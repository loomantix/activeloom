"""The runner owns progression; fake workers never choose or start a next pass."""

from __future__ import annotations

from collections.abc import Iterator
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
        if name.startswith(("AGENT_LOOP_", "ACTIVELOOM_")):
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
        outcome="clean",
        exit_code=0,
        missing=False,
        fail_attest=False,
        fail_check=False,
        preflight=False,
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
            if controls.preflight:
                module.save(
                    Path(env["ACTIVELOOM_LAUNCH_STATE"]),
                    {
                        "version": 1,
                        "attempt_id": env["ACTIVELOOM_ATTEMPT_ID"],
                        "phase": "preflight",
                        "review_started": False,
                        "failure_reason": "dirty_surface",
                    },
                )
                log.write_text("synthetic preflight rejection\n")
                raise module.ProcessFailure("synthetic preflight failure", 1)
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
        def preflight(self) -> None:
            pass

        def environment(self, engine: str) -> dict[str, str]:
            return {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("AGENT_LOOP_", "ACTIVELOOM_"))
            }

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
    harness.args.recover_preflight = True
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
    shutil.copyfile(
        SCRIPTS / "review-launch-state.py", tmp_path / "review-launch-state.py"
    )
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
            **{
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("AGENT_LOOP_", "ACTIVELOOM_"))
            },
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
        assert (
            f"review pass on PR #1 in example/repo, round 1, pinned base {BASE}, "
            f"exact head {HEAD}. "
        ) in argv[-1]
        assert "absolute paths" not in argv[-1]
        assert "Do not launch another engine" in argv[-1]


def test_codex_launcher_records_execution_and_forwards_run_id(
    tmp_path: Path,
) -> None:
    import shutil

    launcher = tmp_path / "run-codex-review.py"
    shutil.copyfile(SCRIPTS / launcher.name, launcher)
    shutil.copyfile(
        SCRIPTS / "review-launch-state.py", tmp_path / "review-launch-state.py"
    )
    handoff = tmp_path / "handoff.json"
    (tmp_path / "local-review-handoff.py").write_text(
        "import json, os, sys\n"
        "open(os.environ['HANDOFF'], 'w').write(json.dumps(sys.argv[1:]))\n"
    )
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
    elif args[0] == "ls-remote": print("a"*40 + "\\trefs/heads/fix/example")
elif name == "timeout": os.execvp(args[3], args[3:])
else: pathlib.Path(os.environ["CAPTURE"]).write_text(pathlib.Path(os.environ["ACTIVELOOM_LAUNCH_STATE"]).read_text())
"""
    for name in ("gh", "git", "timeout", "codex"):
        path = bindir / name
        path.write_text(f"#!{sys.executable}\n" + tool)
        path.chmod(0o700)
    capture = tmp_path / "observed-by-reviewer.json"
    result = subprocess.run(
        [
            sys.executable,
            str(launcher),
            *("--repo", "example/repo", "--pr", "1", "--base", BASE),
            *("--head", HEAD, "--round", "1"),
        ],
        cwd=tmp_path,
        env={
            **{
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("AGENT_LOOP_", "ACTIVELOOM_"))
            },
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "CAPTURE": str(capture),
            "HANDOFF": str(handoff),
            "ACTIVELOOM_LAUNCH_STATE": str(tmp_path / "launch.json"),
            "ACTIVELOOM_ATTEMPT_ID": "attempt-1",
            "ACTIVELOOM_RUN_ID": "f" * 64,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    # The reviewer process itself must already see execution evidence, so any
    # failure it reports can never be read back as preflight-only.
    observed = json.loads(capture.read_text())
    assert observed["attempt_id"] == "attempt-1"
    assert observed["phase"] == "execution"
    assert observed["review_started"] is None
    argv = json.loads(handoff.read_text())
    assert argv[0] == "authorize-pass"
    assert argv[argv.index("--run-id") + 1] == "f" * 64


@pytest.mark.parametrize(
    ("launcher", "starts_reviewer"),
    [
        ("run-claude-review.sh", "exec timeout "),
        ("run-agy-review.sh", 'run_agy_managed "$result_file" '),
        ("run-codex-review.py", "os.execv("),
    ],
)
def test_runner_launchers_mark_execution_before_the_reviewer_starts(
    launcher: str, starts_reviewer: str
) -> None:
    lines = [
        line.strip()
        for line in (SCRIPTS / launcher).read_text().splitlines()
        if line.strip()
    ]
    marker = (
        'launch_state("execution")'
        if launcher.endswith(".py")
        else "launch_state execution"
    )
    assert lines.count(marker) == 1
    assert lines[lines.index(marker) + 1].startswith(starts_reviewer)
    authorization = next(
        i for i, line in enumerate(lines) if "authorize-pass" in line
    )
    block = "\n".join(lines[authorization : authorization + 18])
    assert (
        "ACTIVELOOM_RUN_ID" in block
        if launcher.endswith(".py")
        else '"${run_id_args[@]}"' in block
    )
    if not launcher.endswith(".py"):
        assert 'run_id_args=(--run-id "$ACTIVELOOM_RUN_ID")' in lines


def test_launch_state_helper_copies_are_identical() -> None:
    assert (SCRIPTS / "review-launch-state.py").read_bytes() == (
        ROOT / ".claude/skills/critique/scripts/review-launch-state.py"
    ).read_bytes()


def test_preflight_recovery_keeps_completed_passes_and_budget(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.args.chain = "claude,codex,gemini"
    original = harness.module.managed
    rejected = False

    def managed(
        argv: list[str], log: Path, env: dict[str, str], timeout: int = 3600
    ) -> None:
        nonlocal rejected
        if env.get("AGENT_LOOP_REVIEW_ENGINE") == "gemini" and not rejected:
            rejected = True
            log.write_text("agy relay surface checkout must be clean\n")
            if env.get("ACTIVELOOM_LAUNCH_STATE"):
                harness.module.save(
                    Path(env["ACTIVELOOM_LAUNCH_STATE"]),
                    {
                        "version": 1,
                        "attempt_id": env["ACTIVELOOM_ATTEMPT_ID"],
                        "phase": "preflight",
                        "review_started": False,
                        "failure_reason": "dirty_surface",
                    },
                )
            raise harness.module.Blocked("synthetic dirty surface")
        original(argv, log, env, timeout)

    monkeypatch.setattr(harness.module, "managed", managed)
    with pytest.raises(harness.module.Blocked, match="dirty surface"):
        harness.runner(harness.args, harness.directory).run()
    state = harness.module.read(harness.directory / "state.json")
    assert harness.launches == ["claude", "codex"]
    assert state["pending"]["engine"] == "gemini"
    assert state["pending"]["round"] == 1
    harness.args.resume = True
    harness.args.recover_preflight = True
    resumed = harness.runner(harness.args, harness.directory)
    assert resumed.run() == "converged"
    assert resumed.state["run_id"] == state["run_id"]
    assert resumed.state["config"] == state["config"]
    assert resumed.state["completed"][:2] == state["completed"]
    assert harness.launches == ["claude", "codex", "gemini"]
    assert len(resumed.state["attempts"]) == 4
    assert resumed.state["attempts"][2]["review_started"] is False
    assert resumed.state["attempts"][2]["failure_reason"] == "dirty_surface"


@pytest.mark.parametrize("broken_engine", ["codex", "claude", "gemini"])
def test_all_engines_preflight_before_any_review(
    harness: Any, monkeypatch: pytest.MonkeyPatch, broken_engine: str
) -> None:
    harness.args.chain = "claude,codex,gemini"
    checked: list[str] = []
    monkeypatch.setattr(harness.runner, "prepare_installation", lambda self: None)
    monkeypatch.setattr(harness.runner, "preflight", harness.module.Runner.preflight)

    def managed(
        argv: list[str], log: Path, env: dict[str, str], timeout: int = 3600
    ) -> None:
        assert argv[-1] == "--preflight-only"
        engine = next(
            e
            for e, name in harness.module.LAUNCHERS.items()
            if name == Path(argv[1]).name
        )
        checked.append(engine)
        if engine == broken_engine:
            raise harness.module.Blocked("synthetic missing tool")

    monkeypatch.setattr(harness.module, "managed", managed)
    runner = harness.runner(harness.args, harness.directory)
    with pytest.raises(harness.module.Blocked, match="selected-engine preflight"):
        runner.run()
    assert checked == ["claude", "codex", "gemini"]
    assert runner.state["run_id"] is None
    assert runner.state["attempts"] == []
    assert harness.events == harness.launches == []


@pytest.mark.parametrize("phase", ["execution", "ready", "preflight"])
def test_resume_never_retries_an_unrecorded_exit(
    harness: Any, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    original = harness.module.managed

    def interrupted(
        argv: list[str], log: Path, env: dict[str, str], timeout: int = 3600
    ) -> None:
        if "AGENT_LOOP_REVIEW_ENGINE" not in env:
            original(argv, log, env, timeout)
            return
        harness.module.save(
            Path(env["ACTIVELOOM_LAUNCH_STATE"]),
            {
                "version": 1,
                "attempt_id": env["ACTIVELOOM_ATTEMPT_ID"],
                "phase": phase,
                "review_started": False if phase == "preflight" else None,
            },
        )
        # An uncatchable controller interruption has no returned exit to record.
        raise SystemExit(137)

    monkeypatch.setattr(harness.module, "managed", interrupted)
    with pytest.raises(SystemExit):
        harness.runner(harness.args, harness.directory).run()
    harness.args.resume = harness.args.recover_preflight = True
    monkeypatch.setattr(harness.module, "managed", original)
    with pytest.raises(harness.module.Blocked, match="returned no result"):
        harness.runner(harness.args, harness.directory).run()
    assert not harness.launches


@pytest.mark.parametrize(
    ("phase", "classified"), [("execution", "execution_failed"), ("ready", "launching")]
)
def test_caught_failure_after_preflight_is_never_recoverable(
    harness: Any, monkeypatch: pytest.MonkeyPatch, phase: str, classified: str
) -> None:
    original = harness.module.managed

    def failed(
        argv: list[str], log: Path, env: dict[str, str], timeout: int = 3600
    ) -> None:
        if "AGENT_LOOP_REVIEW_ENGINE" not in env:
            original(argv, log, env, timeout)
            return
        harness.module.save(
            Path(env["ACTIVELOOM_LAUNCH_STATE"]),
            {
                "version": 1,
                "attempt_id": env["ACTIVELOOM_ATTEMPT_ID"],
                "phase": phase,
                "review_started": None,
                "failure_reason": None,
            },
        )
        raise harness.module.ProcessFailure("synthetic reviewer failure", 1)

    monkeypatch.setattr(harness.module, "managed", failed)
    runner = harness.runner(harness.args, harness.directory)
    with pytest.raises(harness.module.Blocked):
        runner.run()
    assert runner.state["pending"]["phase"] == classified
    assert runner.state["attempts"][-1]["review_started"] is not False
    harness.args.resume = harness.args.recover_preflight = True
    monkeypatch.setattr(harness.module, "managed", original)
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    assert not harness.launches


def test_launch_environment_failure_leaves_the_pass_resumable(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = harness.runner(harness.args, harness.directory)

    def broken(engine: str) -> dict[str, str]:
        raise harness.module.Blocked("synthetic environment failure")

    monkeypatch.setattr(runner, "environment", broken)
    with pytest.raises(harness.module.Blocked, match="synthetic environment failure"):
        runner.run()
    assert runner.state.get("attempts", []) == []
    assert runner.state["pending"]["phase"] == "prepared"
    assert not harness.launches


def test_migration_preserves_the_v1_snapshot_and_budget(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.controls.fail_check = True
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    state_path = harness.directory / "state.json"
    prior = harness.module.read(state_path)
    prior["version"] = 1
    prior.pop("attempts")
    harness.module.save(state_path, prior)
    original = harness.module.command

    def command(argv: list[str]) -> str:
        if argv[:1] == ["git"] and "-C" in argv:
            if "--show-toplevel" in argv:
                return str(ROOT)
            return "" if "status" in argv else HEAD
        return str(original(argv))

    monkeypatch.setattr(harness.module, "command", command)
    harness.args.resume = True
    harness.args.migrate_controller = HEAD
    harness.controls.fail_check = False
    runner = harness.runner(harness.args, harness.directory)
    runner.initialize()
    assert harness.module.read(harness.directory / "state-v1.json") == prior
    for key in (
        "run_id",
        "config",
        "pending",
        "completed",
        "base",
        "head",
        "start_head",
    ):
        assert runner.state[key] == prior[key]
    for name, sha in prior["control_hashes"].items():
        assert harness.module.digest(harness.directory / "control" / name) == sha
    assert runner.run() == "converged"
    assert len(harness.launches) == 4


@pytest.mark.parametrize("corruption", ["log", "controller", "result", "head"])
def test_legacy_reconciliation_rejects_uncertain_evidence(
    harness: Any, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    runner = harness.runner(harness.args, harness.directory)
    runner.initialize()
    pending = {
        "phase": "launching",
        "engine": "gemini",
        "round": 1,
        "before": HEAD,
        "folder": "pass-3",
    }
    folder = harness.directory / "pass-3"
    folder.mkdir()
    (folder / "worker.log").write_text("agy relay surface checkout must be clean\n")
    for name in ("historical.json", "before-threads.json"):
        harness.module.save(folder / name, [])
    backup = harness.directory / "state-v1.json"
    harness.module.save(backup, runner.state)
    runner.state["migrations"] = [
        {
            "prior_state": backup.name,
            "prior_state_sha256": harness.module.digest(backup),
            "prior_control": "control",
            "prior_control_hashes": runner.state["control_hashes"],
        }
    ]
    monkeypatch.setattr(
        harness.module, "LEGACY_PREFLIGHT_HASHES", dict(runner.state["control_hashes"])
    )
    proof = harness.module.digest(folder / "worker.log")
    if corruption == "log":
        (folder / "worker.log").write_text("review interrupted\n")
    elif corruption == "controller":
        (runner.control / "run-agy-review.sh").write_text("changed")
    elif corruption == "result":
        (folder / "result.json").write_text("{}")
    else:
        pending["before"] = BASE
    with pytest.raises(harness.module.Blocked):
        runner.reconcile_legacy_preflight(pending, proof)
    assert not runner.state["attempts"]


def test_installation_repair_preserves_dirty_bytes_and_the_original_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("review-chain-runner")
    source = tmp_path / "consumer"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    workflow = source / ".codex/REVIEW_WORKFLOW.md"
    workflow.parent.mkdir()
    workflow.write_text("original review instructions\n")
    for relative in (
        "references/local-review-ledger.md",
        "skills/critique/scripts/review-ledger.js",
        "skills/critique/scripts/review-ledger.version",
        "skills/critique/scripts/review-ledger.integrity",
        "skills/critique/scripts/package.json",
        "skills/deepcritique/SKILL.md",
        "skills/critique/SKILL.md",
        "skills/refactorpass/SKILL.md",
    ):
        path = source / ".codex" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic installation input\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "synthetic review surface",
        ],
        check=True,
        capture_output=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    monkeypatch.chdir(source)
    directory = tmp_path / "checkpoint"
    directory.mkdir()
    runner = module.Runner(
        SimpleNamespace(repo="example/repo", repair_installation=False), directory
    )
    runner.state = {
        "head": revision,
        "installation_revision": revision,
        "config": {"plan": "codex"},
    }
    runner.prepare_installation()
    installed = directory / "installation/native/.codex/REVIEW_WORKFLOW.md"
    assert installed.read_bytes() == workflow.read_bytes()
    environment = runner.environment("codex")
    for key, value in environment.items():
        if key.startswith("ACTIVELOOM_"):
            monkeypatch.setenv(key, value)
    verifier = load("review-launch-state")
    verifier.verify_installation()
    installed.write_text("local change that must be preserved\n")
    with pytest.raises(ValueError, match="installation changed"):
        verifier.verify_installation()
    workflow.write_text("later consumer changes must not redefine the pin\n")
    runner.state["head"] = HEAD
    runner.args.repair_installation = True
    runner.prepare_installation()
    assert installed.read_text() == "original review instructions\n"
    assert runner.state["installation"]["revision"] == revision
    preserved = directory / runner.state["installation_history"][0]["directory"]
    assert (
        preserved / "native/.codex/REVIEW_WORKFLOW.md"
    ).read_text() == "local change that must be preserved\n"
    for key, value in runner.environment("codex").items():
        if key.startswith("ACTIVELOOM_"):
            monkeypatch.setenv(key, value)
    verifier.verify_installation()


def test_execution_marker_cannot_be_replaced_by_nested_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("review-launch-state")
    marker = tmp_path / "launch.json"
    monkeypatch.setenv("ACTIVELOOM_LAUNCH_STATE", str(marker))
    monkeypatch.setenv("ACTIVELOOM_ATTEMPT_ID", "owned-attempt")
    module.record("preflight", "missing_tool")
    module.record("execution")
    execution = marker.read_bytes()
    with pytest.raises(ValueError, match="cannot return to preflight"):
        module.record("preflight", "dirty_surface")
    assert marker.read_bytes() == execution


@pytest.mark.parametrize("cut", ["snapshot", "checkpoint"])
def test_recovery_resumes_after_retry_directory_checkpoint_interruption(
    harness: Any, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    harness.controls.preflight = True
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    before = harness.module.read(harness.directory / "state.json")
    harness.args.resume = harness.args.recover_preflight = True
    harness.controls.preflight = False
    original = harness.module.save
    original_replace = harness.module.os.replace

    def interrupted(path: Path, value: Any) -> None:
        if (
            cut == "checkpoint"
            and path.name == "state.json"
            and (value.get("pending") or {}).get("phase") == "prepared"
        ):
            raise OSError("injected retry checkpoint interruption")
        original(path, value)

    def interrupted_replace(source: Any, target: Any) -> None:
        if cut == "snapshot" and Path(source).name == "history.pending":
            raise OSError("injected retry snapshot interruption")
        original_replace(source, target)

    monkeypatch.setattr(harness.module, "save", interrupted)
    monkeypatch.setattr(harness.module.os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="injected retry"):
        harness.runner(harness.args, harness.directory).run()
    monkeypatch.setattr(harness.module, "save", original)
    monkeypatch.setattr(harness.module.os, "replace", original_replace)
    resumed = harness.runner(harness.args, harness.directory)
    assert resumed.run() == "converged"
    assert resumed.state["run_id"] == before["run_id"]
    assert resumed.state["config"] == before["config"]
    assert harness.launches == ["codex", "claude", "codex", "claude"]
    assert len(resumed.state["attempts"]) == 5


@pytest.mark.parametrize("cut", ["snapshot", "checkpoint"])
def test_legacy_reconciliation_resumes_after_evidence_write(
    harness: Any, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    runner = harness.runner(harness.args, harness.directory)
    runner.initialize()
    pending = {
        "phase": "launching",
        "engine": "gemini",
        "round": 1,
        "before": HEAD,
        "folder": "pass-3",
    }
    runner.state["pending"] = pending
    folder = harness.directory / "pass-3"
    folder.mkdir()
    (folder / "worker.log").write_text("agy relay surface checkout must be clean\n")
    for name in ("historical.json", "before-threads.json"):
        harness.module.save(folder / name, [])
    backup = harness.directory / "state-v1.json"
    harness.module.save(backup, runner.state)
    runner.state["migrations"] = [
        {
            "prior_state": backup.name,
            "prior_state_sha256": harness.module.digest(backup),
            "prior_control": "control",
            "prior_control_hashes": runner.state["control_hashes"],
        }
    ]
    monkeypatch.setattr(
        harness.module, "LEGACY_PREFLIGHT_HASHES", dict(runner.state["control_hashes"])
    )
    runner.persist()
    original = runner.persist
    original_replace = harness.module.os.replace

    def interrupted() -> None:
        if (
            cut == "checkpoint"
            and runner.state["pending"]["phase"] == "preflight_failed"
        ):
            raise OSError("injected legacy checkpoint interruption")
        original()

    def interrupted_replace(source: Any, target: Any) -> None:
        if cut == "snapshot" and Path(target).name.startswith("legacy-launch-"):
            raise OSError("injected legacy snapshot interruption")
        original_replace(source, target)

    monkeypatch.setattr(runner, "persist", interrupted)
    monkeypatch.setattr(harness.module.os, "replace", interrupted_replace)
    proof = harness.module.digest(folder / "worker.log")
    with pytest.raises(OSError, match="injected legacy"):
        runner.reconcile_legacy_preflight(pending, proof)
    evidence = (folder / "launch.json").read_bytes() if cut == "checkpoint" else None
    monkeypatch.setattr(harness.module.os, "replace", original_replace)
    resumed = harness.runner(harness.args, harness.directory)
    resumed.reconcile_legacy_preflight(resumed.state["pending"], proof)
    assert resumed.state["pending"]["phase"] == "preflight_failed"
    assert len(resumed.state["attempts"]) == 1
    if evidence is not None:
        assert (folder / "launch.json").read_bytes() == evidence
    assert harness.module.digest(folder / "worker.log") == proof


def test_prepared_retry_rejects_replacement_run_before_launch(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.controls.preflight = True
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    harness.args.resume = harness.args.recover_preflight = True
    harness.controls.preflight = False
    runner = harness.runner(harness.args, harness.directory)

    def interrupted(pending: dict[str, Any]) -> None:
        raise OSError("interrupted after preparation")

    monkeypatch.setattr(runner, "launch", interrupted)
    with pytest.raises(OSError, match="after preparation"):
        runner.run()
    resumed = harness.runner(harness.args, harness.directory)
    original = resumed.helper

    def replacement(name: str, *parts: str) -> dict[str, Any]:
        result: dict[str, Any] = original(name, *parts)
        if parts[0] in ("next-pass", "authorize-pass"):
            result["run_id"] = "e" * 64
        return result

    monkeypatch.setattr(resumed, "helper", replacement)
    with pytest.raises(harness.module.Blocked, match="active run changed"):
        resumed.run()
    assert harness.launches == []
    assert harness.events == []


def test_missing_selected_harness_has_recovery_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("review-chain-runner")
    directory = tmp_path / "checkpoint"
    directory.mkdir()
    runner = module.Runner.__new__(module.Runner)
    runner.directory = directory
    runner.state = {"installation_revision": HEAD, "config": {"plan": "claude"}}
    runner.args = SimpleNamespace(repair_installation=False)

    def unavailable(*args: Any, **kwargs: Any) -> bytes:
        raise subprocess.CalledProcessError(128, ["git", "archive"])

    monkeypatch.setattr(module.subprocess, "check_output", unavailable)
    with pytest.raises(
        module.Blocked, match=r"\.claude.*--resume --repair-installation"
    ):
        runner.prepare_installation()
