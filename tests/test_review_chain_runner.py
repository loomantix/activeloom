"""The runner owns progression; fake workers never choose or start a next pass."""

from __future__ import annotations

from collections.abc import Iterator
import importlib.util
import json
import os
import signal
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


@pytest.fixture(params=[False, True], ids=["standalone", "inside-review-worker"])
def enclosing_review(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    result = tmp_path / "enclosing-result.json"
    sentinel = "Original enclosing review evidence\n"
    result.write_text(sentinel)
    if request.param:
        for name, value in {
            "AGENT_LOOP_REVIEW_RESULT_FILE": str(result),
            "AGENT_LOOP_REVIEW_ENGINE": "claude",
            "AGENT_LOOP_REVIEW_ROUND": "1",
            "AGENT_LOOP_REVIEW_BASE_SHA": BASE,
            "AGENT_LOOP_PR_HEAD_SHA": HEAD,
        }.items():
            monkeypatch.setenv(name, value)
    yield
    assert result.read_text() == sentinel


@pytest.fixture
def harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enclosing_review: None
) -> Any:
    # This is a synthetic top-level controller, even when pytest itself runs
    # inside a real reviewer. Inherited worker state otherwise makes fake
    # validation commands look like launches and can overwrite the real result.
    for name in list(os.environ):
        if name.startswith("AGENT_LOOP_"):
            monkeypatch.delenv(name)
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


def test_tier_publication_reaches_real_ledger_dispatch(
    harness: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shutil

    node = shutil.which("node")
    assert node
    original = harness.runner.helper
    checked: list[str] = []

    def helper(self: Any, name: str, *parts: str) -> dict[str, Any]:
        if parts[0] == "post-pr-comment":
            # Let the real parser/dispatcher validate the call, but make gh
            # unavailable so this test cannot perform any external mutation.
            result = subprocess.run(
                [node, str(SCRIPTS / "review-ledger.js"), *parts],
                env={**os.environ, "PATH": str(tmp_path)},
                capture_output=True,
                text=True,
            )
            assert result.returncode != 0
            assert "post-pr-comment requires" not in result.stderr
            assert "GitHub operation failed" in result.stderr
            checked.append(parts[parts.index("--head") + 1])
        return dict(original(self, name, *parts))

    monkeypatch.setattr(harness.runner, "helper", helper)
    assert harness.runner(harness.args, harness.directory).run() == "converged"
    assert checked == [HEAD]


@pytest.mark.parametrize("failure", ["authorization", "base", "copy"])
def test_initialization_failure_can_be_retried(
    harness: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    authorization = Path(harness.args.authorization_file)
    content = authorization.read_text()
    original_command = harness.module.command
    original_copy = harness.module.shutil.copyfile
    if failure == "authorization":
        authorization.unlink()
    elif failure == "base":

        def missing_base(argv: list[str]) -> str:
            if argv[:3] == ["git", "rev-parse", "--verify"]:
                raise harness.module.Blocked("synthetic missing base")
            return str(original_command(argv))

        monkeypatch.setattr(harness.module, "command", missing_base)
    else:

        def interrupted_copy(source: Path, target: Path) -> None:
            raise OSError("synthetic interrupted copy")

        monkeypatch.setattr(harness.module.shutil, "copyfile", interrupted_copy)
    with pytest.raises((OSError, harness.module.Blocked)):
        harness.runner(harness.args, harness.directory).run()
    assert not (harness.directory / "state.json").exists()
    assert not harness.launches
    authorization.write_text(content)
    monkeypatch.setattr(harness.module, "command", original_command)
    monkeypatch.setattr(harness.module.shutil, "copyfile", original_copy)
    assert harness.runner(harness.args, harness.directory).run() == "converged"
    assert len(harness.launches) == 4


def test_resume_retries_unlaunched_thread_snapshot(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = harness.runner.threads

    def unavailable(self: Any, path: Path) -> list[int]:
        raise harness.module.Blocked("synthetic network failure")

    monkeypatch.setattr(harness.runner, "threads", unavailable)
    with pytest.raises(harness.module.Blocked, match="network failure"):
        harness.runner(harness.args, harness.directory).run()
    state = harness.module.read(harness.directory / "state.json")
    assert state["pending"] is None
    assert not harness.launches
    monkeypatch.setattr(harness.runner, "threads", original)
    harness.args.resume = True
    resumed = harness.runner(harness.args, harness.directory)
    assert resumed.run() == "converged"
    assert resumed.state["run_id"] == state["run_id"]
    assert len(harness.launches) == 4


@pytest.mark.parametrize("directory", ["control", "pass-1"])
def test_preparation_refuses_symlink_directories(
    harness: Any, tmp_path: Path, directory: str
) -> None:
    target = tmp_path / "external"
    target.mkdir()
    (harness.directory / directory).symlink_to(target, target_is_directory=True)
    with pytest.raises(harness.module.Blocked, match="cannot be a symlink"):
        harness.runner(harness.args, harness.directory).run()
    assert not list(target.iterdir())
    assert not harness.launches


def test_uncheckpointed_worker_evidence_is_preserved(harness: Any) -> None:
    folder = harness.directory / "pass-1"
    folder.mkdir()
    evidence = folder / "worker.log"
    evidence.write_text("Existing worker evidence")
    with pytest.raises(harness.module.Blocked, match="uncheckpointed pass evidence"):
        harness.runner(harness.args, harness.directory).run()
    assert evidence.read_text() == "Existing worker evidence"
    assert not harness.launches


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


@pytest.mark.parametrize("denied_signal", [signal.SIGTERM, signal.SIGKILL])
def test_chain_advances_after_cleanup_denial_for_absent_group(
    harness: Any, monkeypatch: pytest.MonkeyPatch, denied_signal: int
) -> None:
    real_killpg = os.killpg
    probes: list[int] = []

    def killpg(pid: int, sig: int) -> None:
        if sig == denied_signal:
            raise PermissionError(1, "Operation not permitted")
        if sig == 0:
            probes.append(pid)
        real_killpg(pid, sig)

    monkeypatch.setattr(harness.module.os, "killpg", killpg)
    assert harness.runner(harness.args, harness.directory).run() == "converged"
    assert harness.launches == ["codex", "claude", "codex", "claude"]
    assert probes


@pytest.mark.parametrize("probe_denied", [False, True])
def test_managed_cleanup_denial_blocks_when_group_absence_is_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_denied: bool
) -> None:
    module = load("review-chain-runner")

    def killpg(pid: int, sig: int) -> None:
        if sig != 0 or probe_denied:
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(module.os, "killpg", killpg)
    with pytest.raises(module.Blocked, match="process-group cleanup denied"):
        module.managed(
            [sys.executable, "-c", "pass"],
            tmp_path / "worker.log",
            dict(os.environ),
            5,
        )


def test_managed_cleanup_denial_does_not_hide_failed_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("review-chain-runner")
    real_killpg = os.killpg

    def killpg(pid: int, sig: int) -> None:
        if sig:
            raise PermissionError(1, "Operation not permitted")
        real_killpg(pid, sig)

    monkeypatch.setattr(module.os, "killpg", killpg)
    with pytest.raises(module.Blocked, match="exited 7"):
        module.managed(
            [sys.executable, "-c", "raise SystemExit(7)"],
            tmp_path / "worker.log",
            dict(os.environ),
            5,
        )


def test_managed_cleanup_denial_keeps_timeout_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("review-chain-runner")
    real_killpg = os.killpg
    groups: list[int] = []

    def killpg(pid: int, sig: int) -> None:
        groups.append(pid)
        if sig:
            raise PermissionError(1, "Operation not permitted")
        real_killpg(pid, sig)

    monkeypatch.setattr(module.os, "killpg", killpg)
    try:
        with pytest.raises(module.Blocked) as caught:
            module.managed(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                tmp_path / "worker.log",
                dict(os.environ),
                1,
            )
    finally:
        for pid in set(groups):
            try:
                real_killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    message = str(caught.value)
    assert "exit not confirmed" in message
    assert "process-group cleanup denied" in message
    assert "worker timed out after 1s" in message
    assert "sleep" not in message


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
