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
CAPACITY = "Selected model is at capacity. Please try a different model."


@pytest.fixture
def capacity_harness(harness: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = harness.module
    original_environment = harness.runner.environment
    original_managed = module.managed
    controls = SimpleNamespace(
        failures=1, cleanup_denied=False, exit_code=1, side_effect=None
    )
    launches: list[dict[str, str | None]] = []

    def environment(self: Any, engine: str) -> dict[str, str]:
        env = original_environment(self, engine)
        settings = self.state.setdefault("review_settings", {})
        settings.setdefault(
            "codex",
            {
                "engine": "codex",
                "model": "gpt-6-astra",
                "effort": "max",
                "source": "user profile",
                "fallback": {"model": "gpt-5.6-sol", "effort": "medium"},
            },
        )
        if engine == "codex":
            selected = self.selected_settings(engine)
            env.update(
                ACTIVELOOM_REVIEW_MODEL=selected["model"],
                ACTIVELOOM_REVIEW_EFFORT=selected["effort"],
            )
        return dict(env)

    def managed(
        argv: list[str], log: Path, env: dict[str, str], timeout: int = 3600
    ) -> None:
        if (
            "AGENT_LOOP_REVIEW_RESULT_FILE" in env
            and env["AGENT_LOOP_REVIEW_ENGINE"] == "codex"
        ):
            launches.append(
                {
                    "model": env.get("ACTIVELOOM_REVIEW_MODEL"),
                    "effort": env.get("ACTIVELOOM_REVIEW_EFFORT"),
                }
            )
            if controls.failures:
                controls.failures -= 1
                module.save(
                    Path(env["ACTIVELOOM_LAUNCH_STATE"]),
                    {
                        "version": 1,
                        "attempt_id": env["ACTIVELOOM_ATTEMPT_ID"],
                        "phase": "execution",
                        "review_started": None,
                    },
                )
                log.write_text(
                    json.dumps({"type": "turn.failed", "error": {"message": CAPACITY}})
                    + "\n"
                )
                if controls.side_effect:
                    controls.side_effect(log.parent)
                if controls.cleanup_denied:
                    raise module.Blocked("process-group cleanup denied")
                raise module.ProcessFailure("capacity failure", controls.exit_code)
        original_managed(argv, log, env, timeout)

    monkeypatch.setattr(harness.runner, "environment", environment)
    monkeypatch.setattr(module, "managed", managed)
    return SimpleNamespace(harness=harness, controls=controls, launches=launches)


@pytest.mark.parametrize(
    "events,expected",
    [
        ([{"type": "error", "message": CAPACITY}], True),
        ([{"type": "turn.failed", "error": {"message": CAPACITY}}], True),
        (
            [
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "output": CAPACITY},
                }
            ],
            False,
        ),
        ([{"type": "error", "message": CAPACITY}, {"type": "turn.completed"}], False),
        ([{"type": "error", "message": "Authentication failed"}], False),
        ([{"type": "error", "message": CAPACITY}, {"type": "item.started"}], False),
        ([{"type": "error", "message": "Rate limit exceeded"}], False),
        (
            [
                {"type": "error", "message": CAPACITY},
                {"type": "turn.failed", "error": {"message": "Network disconnected"}},
            ],
            False,
        ),
    ],
)
def test_capacity_recognition_uses_only_terminal_json_events(
    tmp_path: Path, events: list[dict[str, Any]], expected: bool
) -> None:
    log = tmp_path / "worker.log"
    log.write_text(
        "ERROR: " + CAPACITY + "\n" + "\n".join(json.dumps(event) for event in events)
    )
    assert load("review-chain-runner").capacity_rejected(log) is expected


def test_capacity_fallback_preserves_run_budget_and_uses_medium(
    capacity_harness: Any,
) -> None:
    h = capacity_harness.harness
    runner = h.runner(h.args, h.directory)
    assert runner.run() == "converged"
    assert capacity_harness.launches == [
        {"model": "gpt-6-astra", "effort": "max"},
        {"model": "gpt-5.6-sol", "effort": "medium"},
        {"model": "gpt-5.6-sol", "effort": "medium"},
    ]
    assert len(runner.state["completed"]) == 4
    assert len(runner.state["attempts"]) == 5
    failed, fallback = runner.state["attempts"][:2]
    assert failed["phase"] == "capacity_failed"
    assert failed["round"] == fallback["round"] == 1
    assert (h.directory / failed["folder"] / "worker.log").is_file()
    assert fallback["settings"] == {"model": "gpt-5.6-sol", "effort": "medium"}
    assert (
        "model gpt-5.6-sol, effort medium (capacity fallback)"
        in runner.settings_line("codex")
    )


def test_fallback_capacity_exhaustion_never_loops(capacity_harness: Any) -> None:
    h = capacity_harness.harness
    capacity_harness.controls.failures = 2
    runner = h.runner(h.args, h.directory)
    with pytest.raises(h.module.Blocked, match="fallback is also at capacity"):
        runner.run()
    assert len(capacity_harness.launches) == 2
    assert runner.state["completed"] == []
    h.args.resume = True
    with pytest.raises(h.module.Blocked, match="fallback is also at capacity"):
        h.runner(h.args, h.directory).run()
    assert len(capacity_harness.launches) == 2


@pytest.mark.parametrize(
    "case",
    [
        "cleanup",
        "timeout",
        "result",
        "threads",
        "comments",
        "head",
        "missing-fallback",
        "log",
    ],
)
def test_unsafe_capacity_failures_cannot_launch_fallback(
    capacity_harness: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    h = capacity_harness.harness
    controls = capacity_harness.controls
    runner = h.runner(h.args, h.directory)
    if case == "cleanup":
        controls.cleanup_denied = True
    elif case == "timeout":
        controls.exit_code = 124
    elif case == "result":
        controls.side_effect = lambda folder: (folder / "result.json").write_text("{}")
    elif case in ("threads", "comments"):
        original = getattr(h.runner, case)

        def mutate(folder: Path) -> None:
            monkeypatch.setattr(
                runner, case, lambda path: h.module.save(path, ["changed"])
            )

        controls.side_effect = mutate
        assert original
    elif case == "head":
        controls.side_effect = lambda folder: monkeypatch.setattr(
            runner, "boundary", lambda: "f" * 40
        )
    elif case == "missing-fallback":
        controls.side_effect = lambda folder: runner.state["review_settings"][
            "codex"
        ].pop("fallback")
    else:
        recover = runner.recover_capacity

        def tamper(pending: dict[str, Any]) -> None:
            (h.directory / pending["folder"] / "worker.log").write_text("changed")
            recover(pending)

        monkeypatch.setattr(runner, "recover_capacity", tamper)
    with pytest.raises(h.module.Blocked):
        runner.run()
    assert len(capacity_harness.launches) == 1
    assert runner.state["completed"] == []


def test_capacity_fallback_resumes_an_interrupted_preparation(
    capacity_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = capacity_harness.harness
    runner = h.runner(h.args, h.directory)
    original = h.module.save
    interrupted = False

    def save(path: Path, value: Any) -> None:
        nonlocal interrupted
        if path.name.startswith("recovery-") and not interrupted:
            interrupted = True
            raise OSError("injected fallback snapshot interruption")
        original(path, value)

    monkeypatch.setattr(h.module, "save", save)
    with pytest.raises(OSError, match="injected fallback"):
        runner.run()
    h.args.resume = True
    resumed = h.runner(h.args, h.directory)
    assert resumed.run() == "converged"
    assert len(capacity_harness.launches) == 3
    assert len(resumed.state["attempts"]) == 5


@pytest.mark.parametrize("evidence", ["comments", "threads", "result", "log", "unchanged"])
def test_prepared_capacity_retry_rechecks_evidence_before_launch(
    capacity_harness: Any, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    h = capacity_harness.harness
    runner = h.runner(h.args, h.directory)
    recover = runner.recover_capacity

    def interrupt(pending: dict[str, Any]) -> None:
        recover(pending)
        raise OSError("interrupted after fallback preparation")

    monkeypatch.setattr(runner, "recover_capacity", interrupt)
    with pytest.raises(OSError, match="after fallback preparation"):
        runner.run()
    before = h.module.read(h.directory / "state.json")
    assert before["pending"]["phase"] == "prepared"
    h.args.resume = True
    resumed = h.runner(h.args, h.directory)
    if evidence in ("comments", "threads"):
        monkeypatch.setattr(
            resumed, evidence, lambda path: h.module.save(path, ["changed"])
        )
    elif evidence in ("result", "log"):
        origin = h.directory / before["attempts"][0]["folder"]
        name = "result.json" if evidence == "result" else "worker.log"
        (origin / name).write_text("{}")
    if evidence == "unchanged":
        assert resumed.run() == "converged"
        assert resumed.state["run_id"] == before["run_id"]
    else:
        with pytest.raises(h.module.Blocked, match="evidence changed"):
            resumed.run()
        assert len(capacity_harness.launches) == 1
        assert resumed.state["completed"] == []


@pytest.mark.parametrize("recovery", ["capacity", "preflight"])
@pytest.mark.parametrize("cut", ["stage", "publish"])
def test_recovery_snapshot_survives_process_termination(
    capacity_harness: Any, monkeypatch: pytest.MonkeyPatch, recovery: str, cut: str
) -> None:
    h = capacity_harness.harness
    if recovery == "preflight":
        capacity_harness.controls.failures = 0
        h.controls.preflight = True
        with pytest.raises(h.module.Blocked):
            h.runner(h.args, h.directory).run()
        h.args.resume = h.args.recover_preflight = True
        h.controls.preflight = False
    replace = h.module.os.replace

    def terminate(source: Any, target: Any) -> None:
        path = Path(target)
        if (
            cut == "stage" and path.name.startswith("recovery-")
            or cut == "publish" and path.name.startswith("before-")
            and (path.parent.name == "fallback" or path.parent.name.startswith("retry-"))
        ):
            os._exit(91)
        replace(source, target)

    monkeypatch.setattr(h.module.os, "replace", terminate)
    child = os.fork()
    if child == 0:
        # A real exit bypasses save()'s finally block, unlike an injected error.
        try:
            h.runner(h.args, h.directory).run()
        finally:
            os._exit(92)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 91
    monkeypatch.setattr(h.module.os, "replace", replace)
    before = h.module.read(h.directory / "state.json")
    capacity_harness.controls.failures = 0
    h.args.resume = True
    resumed = h.runner(h.args, h.directory)
    assert resumed.run() == "converged"
    assert resumed.state["run_id"] == before["run_id"]
    assert len(resumed.state["attempts"]) == 5


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

        def comments(self, path: Path) -> None:
            module.save(path, [])

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
        real_managed=real_managed,
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
    with pytest.raises(module.Blocked) as caught:
        module.managed(
            [sys.executable, "-c", "raise SystemExit(7)"],
            tmp_path / "worker.log",
            dict(os.environ),
            5,
        )
    # The probe proves the group is gone, so the worker's own failure surfaces.
    assert str(caught.value).endswith(f"exited 7; inspect {tmp_path / 'worker.log'}")
    assert "process-group cleanup denied" not in str(caught.value)


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
    for name in (
        "review-launch-state.py",
        "review-profile.py",
        "review-profile.defaults.json",
    ):
        shutil.copyfile(SCRIPTS / name, tmp_path / name)
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
            "ACTIVELOOM_REVIEW_MODEL": "inherit",
            "ACTIVELOOM_REVIEW_EFFORT": "high",
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
        assert "--json" in argv
        assert "-m" not in argv and "--ignore-user-config" not in argv
        assert argv[argv.index('model_reasoning_effort="high"') - 1] == "-c"
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
    for name in (
        "review-launch-state.py",
        "review-profile.py",
        "review-profile.defaults.json",
    ):
        shutil.copyfile(SCRIPTS / name, tmp_path / name)
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
            "ACTIVELOOM_REVIEW_MODEL": "example-model",
            "ACTIVELOOM_REVIEW_EFFORT": "max",
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
    # The reviewer launch itself is captured by a sibling test; here the
    # pinned settings must at least resolve without a profile on disk.
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
    authorization = next(i for i, line in enumerate(lines) if "authorize-pass" in line)
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
            raise harness.module.ProcessFailure("synthetic dirty surface", 1)
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


def test_preflight_cleanup_denial_cannot_retry_a_surviving_launcher(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordinary = harness.module.managed
    real_killpg = os.killpg
    groups: set[int] = set()

    def denied_cleanup(pid: int, sig: int) -> None:
        groups.add(pid)
        if sig:
            raise PermissionError(1, "synthetic denied cleanup")
        real_killpg(pid, sig)

    def stalled_preflight(
        argv: list[str], log: Path, env: dict[str, str], timeout: int = 3600
    ) -> None:
        if "AGENT_LOOP_REVIEW_ENGINE" not in env:
            ordinary(argv, log, env, timeout)
            return
        helper = SCRIPTS / "review-launch-state.py"
        program = (
            f"import runpy,time; runpy.run_path({str(helper)!r})"
            "['record']('preflight','pr_boundary'); time.sleep(30)"
        )
        monkeypatch.setattr(harness.module.os, "killpg", denied_cleanup)
        try:
            harness.real_managed([sys.executable, "-c", program], log, env, 1)
        finally:
            monkeypatch.setattr(harness.module.os, "killpg", real_killpg)

    monkeypatch.setattr(harness.module, "managed", stalled_preflight)
    try:
        runner = harness.runner(harness.args, harness.directory)
        with pytest.raises(
            harness.module.Blocked, match="process-group cleanup denied"
        ):
            runner.run()
        assert groups
        for pid in groups:
            real_killpg(pid, 0)
        marker = harness.directory / runner.state["pending"]["folder"] / "launch.json"
        assert harness.module.read(marker)["phase"] == "preflight"
        attempt = runner.state["attempts"][-1]
        assert attempt["exit_status"] is None
        assert attempt["phase"] == "launching"
        assert attempt["review_started"] is None
        harness.args.resume = harness.args.recover_preflight = True
        monkeypatch.setattr(harness.module, "managed", ordinary)
        with pytest.raises(harness.module.Blocked):
            harness.runner(harness.args, harness.directory).run()
        assert not harness.launches
        for pid in groups:
            real_killpg(pid, 0)
    finally:
        for pid in groups:
            try:
                real_killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def test_recovery_rejects_previously_misclassified_unknown_preflight_exit(
    harness: Any,
) -> None:
    harness.controls.preflight = True
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    state = harness.module.read(harness.directory / "state.json")
    # Older recovery controllers could label denied cleanup this way.
    state["attempts"][-1]["exit_status"] = None
    harness.module.save(harness.directory / "state.json", state)
    harness.controls.preflight = False
    harness.args.resume = harness.args.recover_preflight = True
    with pytest.raises(harness.module.Blocked, match="worker exit is unknown"):
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


@pytest.mark.parametrize(
    "corruption", ["log", "log-proof", "controller", "result", "head"]
)
@pytest.mark.parametrize("diagnostic", ["dirty", "moved", "moved-null"])
def test_legacy_reconciliation_rejects_uncertain_evidence(
    harness: Any, monkeypatch: pytest.MonkeyPatch, corruption: str, diagnostic: str
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
    if diagnostic in ("moved", "moved-null"):
        (folder / "worker.log").write_text(
            "fatal: not a git repository: (null)\n"
            if diagnostic == "moved-null"
            else "fatal: not a git repository: /missing/primary/.git/worktrees/reviewer\n"
        )
        log = harness.directory / "original-controller.log"
        log.write_text(
            f"Starting gemini pass 1 at {HEAD}\n"
            "review-chain blocked: bash exited 128; inspect worker.log; "
            f"checkpoint: {harness.directory}\n"
        )
        harness.args.legacy_controller_log = [str(log), harness.module.digest(log)]
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
    elif corruption == "log-proof":
        # The operator hashes the log as found; only its exact bytes can reject it.
        (folder / "worker.log").write_text("review interrupted\n")
        proof = harness.module.digest(folder / "worker.log")
    elif corruption == "controller":
        (runner.control / "run-agy-review.sh").write_text("changed")
    elif corruption == "result":
        (folder / "result.json").write_text("{}")
    else:
        pending["before"] = BASE
    with pytest.raises(harness.module.Blocked):
        runner.reconcile_legacy_preflight(pending, proof)
    assert not runner.state["attempts"]


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "hash",
        "exit",
        "cleanup",
        "engine",
        "round",
        "head",
        "checkpoint",
        "extra-output",
        "review-output",
        "symlink",
        "ordinary-git-error",
    ],
)
@pytest.mark.parametrize("diagnostic", ["reviewer", "null"])
def test_legacy_git_failure_requires_terminal_controller_evidence(
    harness: Any, corruption: str, diagnostic: str
) -> None:
    runner = harness.runner(harness.args, harness.directory)
    pending = {"engine": "gemini", "round": 2, "before": HEAD}
    worker = harness.directory / "worker.log"
    worker.write_text(
        "fatal: not a git repository: (null)\n"
        if diagnostic == "null"
        else "fatal: not a git repository: /missing/primary/.git/worktrees/reviewer\n"
    )
    controller_log = harness.directory / "original-controller.log"
    terminal = (
        f"Starting gemini pass 2 at {HEAD}\n"
        "review-chain blocked: bash exited 128; inspect worker.log; "
        f"checkpoint: {harness.directory}\n"
    )
    changes = {
        "exit": ("bash exited 128", "bash exited 1"),
        "cleanup": ("bash exited 128", "process-group cleanup denied"),
        "engine": ("Starting gemini", "Starting claude"),
        "round": ("pass 2", "pass 3"),
        "head": (HEAD, BASE),
        "checkpoint": (str(harness.directory), str(harness.directory / "other")),
    }
    if corruption in changes:
        terminal = terminal.replace(*changes[corruption])
    if corruption == "extra-output":
        terminal += "Starting another reviewer\n"
    if corruption == "review-output":
        worker.write_text(worker.read_text() + "agy review failed (exit 128)\n")
    if corruption == "ordinary-git-error":
        worker.write_text("fatal: not a git repository: /missing/unrelated.git\n")
    controller_log.write_text(terminal)
    if corruption != "missing":
        harness.args.legacy_controller_log = [
            str(controller_log),
            harness.module.digest(controller_log),
        ]
    if corruption == "hash":
        controller_log.write_text(terminal + "changed\n")
    if corruption == "symlink":
        target = controller_log.with_suffix(".original")
        controller_log.rename(target)
        controller_log.symlink_to(target)
    with pytest.raises(harness.module.Blocked):
        runner.legacy_failure(pending, worker)


@pytest.fixture
def moved_worktree_failure(tmp_path: Path) -> bytes:
    """Capture Git's real failure after a primary clone moves, then repair it."""
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    moved = tmp_path / "moved"

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            capture_output=True,
            check=True,
            env={**os.environ, "LC_ALL": "C"},
        )

    git("init", str(primary))
    git("-C", str(primary), "commit", "--allow-empty", "-m", "synthetic surface")
    git("-C", str(primary), "worktree", "add", "--detach", str(linked))
    head = git("-C", str(linked), "rev-parse", "HEAD").stdout
    primary.rename(moved)
    broken = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--show-toplevel"],
        capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    assert broken.returncode == 128
    assert broken.stderr.startswith(b"fatal: not a git repository: ")
    git("-C", str(moved), "worktree", "repair", str(linked))
    assert git("-C", str(linked), "rev-parse", "HEAD").stdout == head
    assert git("-C", str(linked), "status", "--porcelain").stdout == b""
    return broken.stderr


def test_legacy_moved_worktree_recovery_keeps_completed_passes(
    harness: Any, monkeypatch: pytest.MonkeyPatch, moved_worktree_failure: bytes
) -> None:
    harness.args.chain = "claude,codex,gemini"
    original = harness.runner.launch

    def legacy_failure(self: Any, pending: dict[str, Any]) -> None:
        if pending["engine"] != "gemini":
            original(self, pending)
            return
        pending["phase"] = "launching"
        folder = self.directory / pending["folder"]
        # v1 recorded only these files before executing its launcher.
        (folder / "before-comments.json").unlink()
        pending.pop("before_comments_sha256")
        (folder / "worker.log").write_bytes(moved_worktree_failure)
        self.state["version"] = 1
        self.state.pop("attempts")
        self.persist()
        raise harness.module.Blocked("bash exited 128; inspect worker.log")

    monkeypatch.setattr(harness.runner, "launch", legacy_failure)
    with pytest.raises(harness.module.Blocked, match="bash exited 128"):
        harness.runner(harness.args, harness.directory).run()
    prior = harness.module.read(harness.directory / "state.json")
    assert harness.launches == ["claude", "codex"]
    folder = harness.directory / prior["pending"]["folder"]
    history = (folder / "historical.json").read_bytes()
    threads = (folder / "before-threads.json").read_bytes()
    controller_log = harness.directory / "original-controller.log"
    controller_log.write_text(
        f"Starting gemini pass 1 at {HEAD}\n"
        "review-chain blocked: bash exited 128; inspect worker.log; "
        f"checkpoint: {harness.directory}\n"
    )
    original_command = harness.module.command

    def command(argv: list[str]) -> str:
        if argv[:1] == ["git"] and "-C" in argv:
            if "--show-toplevel" in argv:
                return str(ROOT)
            return "" if "status" in argv else HEAD
        return str(original_command(argv))

    monkeypatch.setattr(harness.module, "command", command)
    monkeypatch.setattr(
        harness.module, "LEGACY_PREFLIGHT_HASHES", prior["control_hashes"]
    )
    monkeypatch.setattr(harness.runner, "launch", original)
    harness.args.resume = harness.args.recover_preflight = True
    harness.args.migrate_controller = HEAD
    harness.args.reconcile_legacy_preflight = harness.module.digest(
        folder / "worker.log"
    )
    harness.args.legacy_controller_log = [
        str(controller_log),
        harness.module.digest(controller_log),
    ]
    resumed = harness.runner(harness.args, harness.directory)
    assert resumed.run() == "converged"
    assert harness.module.read(harness.directory / "state-v1.json") == prior
    for key in ("run_id", "config", "base", "start_head"):
        assert resumed.state[key] == prior[key]
    assert resumed.state["completed"][:2] == prior["completed"]
    assert harness.launches == ["claude", "codex", "gemini"]
    failed, retry = resumed.state["attempts"]
    assert failed["phase"] == "preflight_failed"
    assert failed["exit_status"] == 128
    assert failed["review_started"] is False
    assert failed["failure_reason"] == "surface_provenance"
    assert failed["round"] == retry["round"] == 1
    assert failed["legacy_controller_log_sha256"] == harness.module.digest(
        controller_log
    )
    assert (folder / "worker.log").read_bytes() == moved_worktree_failure
    assert (folder / "historical.json").read_bytes() == history
    assert (folder / "before-threads.json").read_bytes() == threads


def test_managed_gemini_installation_survives_upstream_clone_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("review-chain-runner")
    upstream = tmp_path / "upstream"
    subprocess.run(["git", "init", str(upstream)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(upstream),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "synthetic trusted pin",
        ],
        check=True,
        capture_output=True,
    )
    pin = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    directory = tmp_path / "checkpoint"
    control = directory / "control"
    control.mkdir(parents=True)
    (directory / "installation").mkdir()
    (control / "run-agy-review.sh").write_text(f'agy_surface_sha="{pin}"\n')
    runner = module.Runner(SimpleNamespace(repo="example/repo"), directory)
    runner.state = {
        "installation": {"manifest_sha256": "d" * 64},
        "review_settings": {"gemini": {"model": "test-model", "effort": "high"}},
    }
    original = module.command
    fetches = []

    def command(argv: list[str]) -> str:
        if "fetch" in argv:
            fetches.append(list(argv))
            # Serve the trusted bytes from a local synthetic repository. Keep
            # the installed origin canonical, without using the network.
            argv = [str(upstream) if part == "origin" else part for part in argv]
        return str(original(argv))

    monkeypatch.setattr(module, "command", command)
    environment = runner.environment("gemini")
    checkout = directory / "installation/agy"
    assert (checkout / ".git").is_dir()
    assert original(["git", "-C", str(checkout), "remote", "get-url", "origin"]) == (
        "https://github.com/loomantix/activeloom.git"
    )
    upstream.rename(tmp_path / "moved-upstream")
    assert runner.environment("gemini") == environment
    assert original(["git", "-C", str(checkout), "rev-parse", "HEAD"]) == pin
    assert original(["git", "-C", str(checkout), "status", "--porcelain"]) == ""
    assert len(fetches) == 1


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
        "review_settings": {
            "codex": {
                "engine": "codex",
                "model": "inherit",
                "effort": "high",
                "source": "user profile",
            }
        },
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
@pytest.mark.parametrize("diagnostic", ["dirty", "moved", "moved-null"])
def test_legacy_reconciliation_resumes_after_evidence_write(
    harness: Any, monkeypatch: pytest.MonkeyPatch, cut: str, diagnostic: str
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
    if diagnostic in ("moved", "moved-null"):
        (folder / "worker.log").write_text(
            "fatal: not a git repository: (null)\n"
            if diagnostic == "moved-null"
            else "fatal: not a git repository: /missing/primary/.git/worktrees/reviewer\n"
        )
        log = harness.directory / "original-controller.log"
        log.write_text(
            f"Starting gemini pass 1 at {HEAD}\n"
            "review-chain blocked: bash exited 128; inspect worker.log; "
            f"checkpoint: {harness.directory}\n"
        )
        harness.args.legacy_controller_log = [str(log), harness.module.digest(log)]
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
    if diagnostic in ("moved", "moved-null"):
        recovery = resumed.recovery_command()
        assert "--legacy-controller-log" in recovery
        assert harness.args.legacy_controller_log[1] in recovery
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


def test_review_settings_are_pinned_once_and_named_in_the_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    module = load("review-chain-runner")
    directory = tmp_path / "checkpoint"
    (directory / "control").mkdir(parents=True)
    for name in ("review-profile.py", "review-profile.defaults.json"):
        shutil.copyfile(SCRIPTS / name, directory / "control" / name)
    profile = tmp_path / "review-profile.json"
    defaults = json.loads((SCRIPTS / "review-profile.defaults.json").read_text())
    document = {
        "schema_version": 1,
        "defaults_version": defaults["defaults_version"],
        "confirmed_at": "2026-01-01T00:00:00Z",
        "engines": defaults["engines"],
        "order": defaults["order"],
        "repos": {"example/repo": {"engines": {"claude": {"effort": "high"}}}},
    }
    profile.write_text(json.dumps(document))
    monkeypatch.setenv("ACTIVELOOM_REVIEW_PROFILE", str(profile))
    # A caller cannot pre-seed the pin through its own environment.
    monkeypatch.setenv("ACTIVELOOM_REVIEW_MODEL", "sonnet")
    monkeypatch.setenv("ACTIVELOOM_REVIEW_EFFORT", "low")
    runner = module.Runner(SimpleNamespace(repo="example/repo"), directory)

    assert runner.settings_line("claude") == (
        "Reviewer settings: not recorded by this run.\n"
    )
    pinned = runner.review_settings("claude")
    assert pinned == {
        "engine": "claude",
        "model": "opus",
        "effort": "high",
        "source": "repository override",
    }
    assert json.loads((directory / "state.json").read_text())["review_settings"] == {
        "claude": pinned
    }

    document["repos"]["example/repo"]["engines"]["claude"]["effort"] = "max"
    profile.write_text(json.dumps(document))
    assert runner.review_settings("claude") == pinned
    assert runner.settings_line("claude") == (
        "Reviewer settings: model opus, effort high (repository override).\n"
    )


def test_missing_review_profile_blocks_before_any_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    module = load("review-chain-runner")
    directory = tmp_path / "checkpoint"
    (directory / "control").mkdir(parents=True)
    shutil.copyfile(
        SCRIPTS / "review-profile.py", directory / "control/review-profile.py"
    )
    monkeypatch.setenv("ACTIVELOOM_REVIEW_PROFILE", str(tmp_path / "absent.json"))
    runner = module.Runner(SimpleNamespace(repo="example/repo"), directory)
    with pytest.raises(module.Blocked, match="review-setup"):
        runner.review_settings("codex")
    assert not (directory / "state.json").exists()


def test_worker_environment_exports_the_selected_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("review-chain-runner")
    directory = tmp_path / "checkpoint"
    directory.mkdir()
    monkeypatch.setenv("ACTIVELOOM_REVIEW_MODEL", "stale-model")
    monkeypatch.setenv("ACTIVELOOM_REVIEW_EFFORT", "low")
    runner = module.Runner(SimpleNamespace(repo="example/repo"), directory)
    runner.state.update(
        installation={"manifest_sha256": "c" * 64},
        review_settings={
            "codex": {
                "engine": "codex",
                "model": "gpt-6-astra",
                "effort": "max",
                "source": "user profile",
                "fallback": {"model": "gpt-5.6-sol", "effort": "medium"},
            },
            "claude": {
                "engine": "claude",
                "model": "opus",
                "effort": "medium",
                "source": "user profile",
            },
        },
    )

    def pinned(engine: str) -> tuple[str | None, str | None]:
        env = runner.environment(engine)
        return env.get("ACTIVELOOM_REVIEW_MODEL"), env.get("ACTIVELOOM_REVIEW_EFFORT")

    assert pinned("codex") == ("gpt-6-astra", "max")
    assert pinned("claude") == ("opus", "medium")
    runner.state["fallback_engines"] = ["codex"]
    assert pinned("codex") == ("gpt-5.6-sol", "medium")
    assert pinned("claude") == ("opus", "medium")
