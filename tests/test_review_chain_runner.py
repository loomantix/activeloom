"""The runner owns progression; fake workers never choose or start a next pass."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".codex/skills/critique/scripts"
HEAD = "a" * 40
BASE = "b" * 40


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = load("review-chain-runner")
    controller = load("local-review-handoff")
    directory = tmp_path / "checkpoint"
    directory.mkdir()
    authorization = tmp_path / "authorization.txt"
    authorization.write_text(
        "Review this synthetic PR; controller integrity selects Deep trigger 3."
    )
    args = SimpleNamespace(
        repo="example/repo",
        pr=1,
        base=BASE,
        tier="deep",
        author="codex",
        trigger=3,
        chain="codex,claude,codex,claude",
        cycle=None,
        check=[f'{sys.executable} -c "pass"'],
        require_dco=False,
        resume=False,
        authorization_file=str(authorization),
    )
    events: list[dict[str, Any]] = []
    launches: list[str] = []
    controls = SimpleNamespace(
        outcome="clean", exit_code=0, missing=False, fail_attest=False, fail_check=False
    )
    monkeypatch.setattr(
        module, "command", lambda argv: BASE if argv[0] == "git" else "test-actor"
    )
    real_managed = module.managed
    # A real child writes one bounded result. It cannot call the controller or
    # schedule another reviewer. Only Runner.run can produce the whole chain.
    worker = tmp_path / "worker.py"
    worker.write_text("""import json, os, pathlib, sys
p = pathlib.Path(os.environ["AGENT_LOOP_REVIEW_RESULT_FILE"])
status = sys.argv[1]
if status != "missing":
    result = dict(version=3, status=status, engine=os.environ["AGENT_LOOP_REVIEW_ENGINE"],
        round=int(os.environ["AGENT_LOOP_REVIEW_ROUND"]), baseSha=os.environ["AGENT_LOOP_REVIEW_BASE_SHA"],
        beforeSha=os.environ["AGENT_LOOP_PR_HEAD_SHA"], afterSha=os.environ["AGENT_LOOP_PR_HEAD_SHA"],
        classification=None, findingFingerprints=[], finalLaneComplete=status != "blocked")
    if status == "blocked": result["blocker"] = "Synthetic blocked review"
    p.write_text(json.dumps(result))
sys.exit(int(sys.argv[2]))
""")

    def managed(
        argv: list[str], log: Path, env: dict[str, str], timeout: int = 3600
    ) -> None:
        if "AGENT_LOOP_REVIEW_RESULT_FILE" in env:
            launches.append(env["AGENT_LOOP_REVIEW_ENGINE"])
            real_managed(
                [
                    sys.executable,
                    str(worker),
                    "missing" if controls.missing else controls.outcome,
                    str(controls.exit_code),
                ],
                log,
                env,
                10,
            )
        elif controls.fail_check:
            raise module.Blocked("synthetic gate failure")
        else:
            real_managed(argv, log, env, 10)

    monkeypatch.setattr(module, "managed", managed)

    class FakeRunner(module.Runner):  # type: ignore[misc, name-defined]
        def boundary(self) -> str:
            return HEAD

        def dco(self, head: str) -> None:
            pass

        def threads(self, path: Path) -> list[int]:
            module.save(path, [])
            return []

        def helper(self, name: str, *parts: str) -> dict[str, Any]:
            operation = parts[0]
            options = dict(zip(parts[1::2], parts[2::2], strict=True))
            plan = {
                "comment_id": 1,
                "run_id": "d" * 64,
                "base": BASE,
                "start_head": HEAD,
                "tier": args.tier,
                "max_rounds": controller.TIER_CAPS[args.tier],
                "sequence": (args.chain or args.cycle).split(","),
                "plan_mode": "chain" if args.chain else "cycle",
            }
            if operation in ("start-run", "authorize-pass"):
                return {"run_id": plan["run_id"], "verified": True}
            if operation == "next-pass":
                return dict(controller._sequence_decision(events, plan, HEAD))
            if operation == "validate-result":
                # Exercise the real vendored result validator, not a fake pass.
                result = subprocess.run(
                    ["node", str(SCRIPTS / "review-ledger.js"), *parts],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return dict(json.loads(result.stdout))
            if operation == "attest":
                marker = (
                    f"<!-- local-review-pass:v3 engine={options['--engine']} round={options['--round']} "
                    f"base={BASE} head={HEAD} result-sha256={options['--expected-result-sha256']} -->\nReviewed."
                )
                if not any(e["body"] == marker for e in events):
                    events.append({"id": len(events) + 2, "body": marker})
                if controls.fail_attest:
                    controls.fail_attest = False
                    raise module.Blocked(
                        "synthetic interruption after remote attestation"
                    )
            return {"verified": True}

    return SimpleNamespace(
        module=module,
        args=args,
        directory=directory,
        runner=FakeRunner,
        controls=controls,
        events=events,
        launches=launches,
    )


def test_one_invocation_runs_all_fixed_steps(harness: Any) -> None:
    runner = harness.runner(harness.args, harness.directory)
    assert runner.run() == "converged"
    assert harness.launches == ["codex", "claude", "codex", "claude"]
    assert len(runner.state["completed"]) == 4


def test_cycle_stops_on_verified_convergence(harness: Any) -> None:
    harness.args.chain, harness.args.cycle = None, "codex,claude"
    runner = harness.runner(harness.args, harness.directory)
    assert runner.run() == "converged"
    assert harness.launches == ["codex", "claude", "codex"]


@pytest.mark.parametrize("failure", ["missing", "blocked", "exit", "check"])
def test_worker_failure_never_starts_next_engine(harness: Any, failure: str) -> None:
    if failure == "missing":
        harness.controls.missing = True
    elif failure == "blocked":
        harness.controls.outcome = "blocked"
    elif failure == "exit":
        harness.controls.exit_code = 1
    else:
        harness.controls.fail_check = True
    runner = harness.runner(harness.args, harness.directory)
    with pytest.raises(harness.module.Blocked):
        runner.run()
    assert harness.launches == ["codex"]
    assert runner.state["completed"] == []
    assert harness.events == []


def test_resume_reconciles_attestation_without_rerunning_worker(harness: Any) -> None:
    harness.controls.fail_attest = True
    with pytest.raises(harness.module.Blocked, match="after remote attestation"):
        harness.runner(harness.args, harness.directory).run()
    harness.args.resume = True
    resumed = harness.runner(harness.args, harness.directory)
    assert resumed.run() == "converged"
    assert harness.launches == ["codex", "claude", "codex", "claude"]
    assert len(harness.events) == 4


def test_resume_can_rerun_failed_gate_not_the_worker(harness: Any) -> None:
    harness.controls.fail_check = True
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    harness.controls.fail_check = False
    harness.args.resume = True
    assert harness.runner(harness.args, harness.directory).run() == "converged"
    assert len(harness.launches) == 4


def test_resume_rejects_changed_plan_and_tampered_snapshot(harness: Any) -> None:
    harness.controls.fail_check = True
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    harness.args.resume = True
    harness.args.chain = "claude,codex"
    with pytest.raises(harness.module.Blocked, match="same plan"):
        harness.runner(harness.args, harness.directory).run()
    harness.args.chain = "codex,claude,codex,claude"
    (harness.directory / "control/local-review-handoff.py").write_text("tampered")
    with pytest.raises(harness.module.Blocked, match="launcher changed"):
        harness.runner(harness.args, harness.directory).run()
    assert len(harness.launches) == 1


def test_resume_cannot_promote_unknown_worker_exit(harness: Any) -> None:
    harness.controls.exit_code = 1
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    harness.args.resume = True
    with pytest.raises(harness.module.Blocked, match="exit is unknown"):
        harness.runner(harness.args, harness.directory).run()
    assert len(harness.launches) == 1


def test_dco_checks_every_non_merge_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("review-chain-runner")
    args = SimpleNamespace()
    runner = module.Runner(args, tmp_path)
    runner.state = {"config": {"require_dco": True}, "base": BASE}
    monkeypatch.setattr(
        module,
        "command",
        lambda argv: HEAD if "rev-list" in argv else "Unsigned commit",
    )
    with pytest.raises(module.Blocked, match="DCO sign-off missing"):
        runner.dco(HEAD)
    monkeypatch.setattr(
        module,
        "command",
        lambda argv: HEAD
        if "rev-list" in argv
        else "Signed-off-by: Test <test@example.com>",
    )
    runner.dco(HEAD)


def test_managed_timeout_stops_worker(tmp_path: Path) -> None:
    module = load("review-chain-runner")
    with pytest.raises(subprocess.TimeoutExpired):
        module.managed(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path / "worker.log",
            dict(os.environ),
            0.1,
        )


@pytest.mark.parametrize("stale", [False, True])
def test_codex_launcher_pins_boundary_without_changing_model(
    tmp_path: Path, stale: bool
) -> None:
    import shutil

    launcher = tmp_path / "run-codex-review.py"
    shutil.copyfile(SCRIPTS / launcher.name, launcher)
    (tmp_path / "local-review-handoff.py").write_text("print('{}')\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = """import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "gh":
    if args[0] == "pr": print(json.dumps(dict(headRefOid="a"*40, headRefName="fix/example", headRepository=dict(nameWithOwner="example/repo"), author=dict(login="actor"))))
    elif args[0] == "repo": print("example/repo")
    else: print("actor")
elif name == "git":
    if args[0] == "rev-parse": print("a"*40)
    elif args[0] == "ls-remote": print(("e" if os.environ["STALE"] == "1" else "a")*40 + "\\trefs/heads/fix/example")
elif name == "timeout": os.execvp(args[3], args[3:])
else: pathlib.Path(os.environ["CAPTURE"]).write_text(json.dumps(args))
"""
    for name in ("gh", "git", "timeout", "codex"):
        path = bindir / name
        path.write_text(f"#!{sys.executable}\n" + tool)
        path.chmod(0o700)
    capture = tmp_path / "captured.json"
    result = subprocess.run(
        [
            sys.executable,
            str(launcher),
            "--repo",
            "example/repo",
            "--pr",
            "1",
            "--base",
            BASE,
            "--head",
            HEAD,
            "--round",
            "1",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "CAPTURE": str(capture),
            "STALE": "1" if stale else "0",
        },
        capture_output=True,
        text=True,
    )
    if stale:
        assert result.returncode != 0
        assert not capture.exists()
    else:
        assert result.returncode == 0, result.stderr
        argv = json.loads(capture.read_text())
        assert argv[:2] == ["exec", "--ephemeral"]
        assert "--model" not in argv and "--ignore-user-config" not in argv
        assert "one Codex review pass" in argv[-1]
        assert "Do not launch another engine" in argv[-1]
