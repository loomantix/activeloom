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
        argv: list[str],
        log: Path,
        env: dict[str, str],
        timeout: int = 3600,
        **_: Any,
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


AGY_IDLE_LINES = (
    "I0919 10:00:00.000 root agent idle; waiting up to 5s for 2 background task(s)\n"
    "I0919 10:00:05.000 terminating 2 background task(s) on exit\n"
)


@pytest.fixture
def idle_harness(harness: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Agy ends its print turn with review lanes still in the background."""
    module = harness.module
    harness.args.chain = "codex,gemini,codex,gemini"
    wrapped = module.managed
    controls = SimpleNamespace(
        idle=1, engine="gemini", side_effect=None, with_result=False, exit_code=0
    )
    idle_launches: list[str] = []

    def managed(
        argv: list[str],
        log: Path,
        env: dict[str, str],
        timeout: int = 3600,
        **_: Any,
    ) -> None:
        if (
            "AGENT_LOOP_REVIEW_RESULT_FILE" in env
            and env["AGENT_LOOP_REVIEW_ENGINE"] == controls.engine
            and controls.idle
        ):
            controls.idle -= 1
            idle_launches.append(env["AGENT_LOOP_REVIEW_ENGINE"])
            module.save(
                Path(env["ACTIVELOOM_LAUNCH_STATE"]),
                {
                    "version": 1,
                    "attempt_id": env["ACTIVELOOM_ATTEMPT_ID"],
                    "phase": "execution",
                    "review_started": None,
                },
            )
            if controls.with_result:
                wrapped(argv, log, env, timeout)
            with log.open("a") as stream:
                stream.write("Waiting for lane results.\n" + AGY_IDLE_LINES)
            if controls.side_effect:
                controls.side_effect(log.parent)
            if controls.exit_code:
                raise module.ProcessFailure("agy exited", controls.exit_code)
            return
        wrapped(argv, log, env, timeout)

    monkeypatch.setattr(module, "managed", managed)
    return SimpleNamespace(
        harness=harness, controls=controls, idle_launches=idle_launches
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        (AGY_IDLE_LINES, True),
        (
            "root agent idle; waiting up to 5s for 1 background task(s)\n"
            "other output\n"
            "terminating 1 background task(s) on exit\n",
            True,
        ),
        (AGY_IDLE_LINES.splitlines(keepends=True)[0], False),
        (AGY_IDLE_LINES.splitlines(keepends=True)[1], False),
        ("".join(reversed(AGY_IDLE_LINES.splitlines(keepends=True))), False),
        (
            "root agent idle; waiting up to 5s for 0 background task(s)\n"
            "terminating 0 background task(s) on exit\n",
            False,
        ),
        (
            "The log said root agent idle; waiting up to 5s for 2 background task(s) here.\n"
            "It then said terminating 2 background task(s) on exit, as quoted.\n",
            False,
        ),
    ],
)
def test_agy_idle_exit_recognition_is_specific(
    tmp_path: Path, text: str, expected: bool
) -> None:
    log = tmp_path / "worker.log"
    log.write_text(text)
    assert load("review-chain-runner").agy_idle_exit(log) is expected


def test_agy_idle_exit_retries_the_same_round_once(
    idle_harness: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    h = idle_harness.harness
    runner = h.runner(h.args, h.directory)
    assert runner.run() == "converged"
    assert idle_harness.idle_launches == ["gemini"]
    assert h.launches == ["codex", "gemini", "codex", "gemini"]
    assert len(runner.state["completed"]) == 4
    assert [(p["engine"], p["round"]) for p in runner.state["completed"]] == [
        ("codex", 1),
        ("gemini", 1),
        ("codex", 2),
        ("gemini", 2),
    ]
    assert len(runner.state["attempts"]) == 5
    failed, retry = runner.state["attempts"][1:3]
    assert failed["phase"] == "idle_exit_failed"
    assert failed["failure_reason"] == "agy_idle_exit"
    assert failed["exit_status"] == 0
    assert (failed["engine"], failed["round"]) == (retry["engine"], retry["round"])
    assert retry["folder"] == failed["folder"] + "/idle-retry"
    assert (h.directory / failed["folder"] / "worker.log").is_file()
    assert f"Agy idle exit: retrying gemini pass 1 once at {HEAD}" in (
        capsys.readouterr().out
    )


def test_second_agy_idle_exit_blocks(idle_harness: Any) -> None:
    h = idle_harness.harness
    idle_harness.controls.idle = 2
    runner = h.runner(h.args, h.directory)
    with pytest.raises(h.module.Blocked, match="no further retry"):
        runner.run()
    assert idle_harness.idle_launches == ["gemini", "gemini"]
    assert len(runner.state["completed"]) == 1
    h.args.resume = True
    with pytest.raises(h.module.Blocked, match="no further retry"):
        h.runner(h.args, h.directory).run()
    assert idle_harness.idle_launches == ["gemini", "gemini"]
    assert h.launches == ["codex"]


@pytest.mark.parametrize(
    "case", ["head", "threads", "comments", "result", "partial", "exit"]
)
def test_unsafe_agy_idle_exit_is_not_retried(
    idle_harness: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    h = idle_harness.harness
    controls = idle_harness.controls
    runner = h.runner(h.args, h.directory)
    if case == "head":
        controls.side_effect = lambda folder: monkeypatch.setattr(
            runner, "boundary", lambda: "f" * 40
        )
    elif case in ("threads", "comments"):
        controls.side_effect = lambda folder: monkeypatch.setattr(
            runner, case, lambda path: h.module.save(path, ["changed"])
        )
    elif case == "result":
        controls.side_effect = lambda folder: (folder / "result.json").write_text("{}")
    elif case == "partial":
        controls.side_effect = lambda folder: (
            folder / "result.json.recovery.json"
        ).write_text("{}")
    else:
        controls.exit_code = 1
    # The fake ledger helper reports an invalid result as a failed node call.
    with pytest.raises((h.module.Blocked, subprocess.CalledProcessError)):
        runner.run()
    assert idle_harness.idle_launches == ["gemini"]
    assert h.launches == ["codex"]
    assert len(runner.state["completed"]) == 1
    assert len(runner.state["attempts"]) == 2


@pytest.mark.parametrize(
    "evidence", ["comments", "threads", "result", "partial", "log", "unchanged"]
)
def test_prepared_idle_retry_rechecks_evidence_before_launch(
    idle_harness: Any, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    h = idle_harness.harness
    runner = h.runner(h.args, h.directory)
    recover = runner.recover_idle_exit

    def interrupt(pending: dict[str, Any]) -> None:
        recover(pending)
        raise OSError("interrupted after idle-retry preparation")

    monkeypatch.setattr(runner, "recover_idle_exit", interrupt)
    with pytest.raises(OSError, match="after idle-retry preparation"):
        runner.run()
    before = h.module.read(h.directory / "state.json")
    assert before["pending"]["phase"] == "prepared"
    assert before["pending"]["folder"].endswith("/idle-retry")
    h.args.resume = True
    resumed = h.runner(h.args, h.directory)
    if evidence in ("comments", "threads"):
        monkeypatch.setattr(
            resumed, evidence, lambda path: h.module.save(path, ["changed"])
        )
    elif evidence in ("result", "partial", "log"):
        origin = h.directory / before["attempts"][1]["folder"]
        name = {
            "result": "result.json",
            "partial": "result.json.recovery.json",
            "log": "worker.log",
        }[evidence]
        (origin / name).write_text("{}")
    if evidence == "unchanged":
        assert resumed.run() == "converged"
        assert resumed.state["run_id"] == before["run_id"]
        assert idle_harness.idle_launches == ["gemini"]
    else:
        with pytest.raises(h.module.Blocked, match="evidence changed"):
            resumed.run()
        assert idle_harness.idle_launches == ["gemini"]
        assert len(resumed.state["completed"]) == 1


def test_agy_idle_lines_after_a_written_result_are_ordinary_success(
    idle_harness: Any,
) -> None:
    h = idle_harness.harness
    idle_harness.controls.with_result = True
    runner = h.runner(h.args, h.directory)
    assert runner.run() == "converged"
    assert idle_harness.idle_launches == ["gemini"]
    assert len(runner.state["attempts"]) == 4
    assert all(a["phase"] == "returned" for a in runner.state["attempts"])


def test_idle_text_from_another_engine_is_not_retried(idle_harness: Any) -> None:
    h = idle_harness.harness
    idle_harness.controls.engine = "codex"
    runner = h.runner(h.args, h.directory)
    with pytest.raises(h.module.Blocked, match="returned no result"):
        runner.run()
    assert idle_harness.idle_launches == ["codex"]
    assert runner.state["attempts"][0]["phase"] == "returned"
    assert runner.state["pending"]["phase"] == "returned"


STDIN_BANNER = "Reading additional input from stdin...\n"


@pytest.mark.parametrize(
    "text,expected",
    [
        (STDIN_BANNER, True),
        ("", True),
        (STDIN_BANNER + 'thread.started {"type": "thread.started"} as text\n', True),
        (STDIN_BANNER + json.dumps({"type": "error", "message": "x"}) + "\n", True),
        (STDIN_BANNER + json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n", False),
        (json.dumps({"type": "turn.started"}) + "\n" + json.dumps({"type": "thread.started"}), False),
    ],
)
def test_codex_startup_stall_recognition_needs_the_json_event(
    tmp_path: Path, text: str, expected: bool
) -> None:
    log = tmp_path / "worker.log"
    log.write_text(text)
    assert load("review-chain-runner").codex_startup_stalled(log) is expected


def test_codex_startup_stall_requires_a_regular_log(tmp_path: Path) -> None:
    module = load("review-chain-runner")
    assert module.codex_startup_stalled(tmp_path / "missing.log") is False
    target = tmp_path / "target.log"
    target.write_text(STDIN_BANNER)
    link = tmp_path / "link.log"
    link.symlink_to(target)
    assert module.codex_startup_stalled(link) is False


def test_managed_worker_never_inherits_an_open_stdin_pipe(tmp_path: Path) -> None:
    module = load("review-chain-runner")
    program = (
        "import os, sys\n"
        "data = sys.stdin.read()\n"
        "a, b = os.fstat(0), os.stat(os.devnull)\n"
        "print((a.st_dev, a.st_ino) == (b.st_dev, b.st_ino), repr(data))\n"
    )
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        # The write end stays open: a worker inheriting this stdin never sees EOF.
        module.managed(
            [sys.executable, "-c", program],
            tmp_path / "worker.log",
            dict(os.environ),
            10,
        )
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)
    assert (tmp_path / "worker.log").read_text() == "True ''\n"


def test_managed_stops_a_worker_that_never_starts(tmp_path: Path) -> None:
    module = load("review-chain-runner")
    log = tmp_path / "worker.log"
    marker = tmp_path / "survived"
    program = (
        "import pathlib, sys, time\n"
        f"print({STDIN_BANNER.strip()!r}, flush=True)\n"
        "time.sleep(20)\n"
        f"pathlib.Path({str(marker)!r}).write_text('late')\n"
    )
    with pytest.raises(module.StartupStalled, match="no thread.started event"):
        module.managed(
            [sys.executable, "-c", program],
            log,
            dict(os.environ),
            30,
            startup_event="thread.started",
            startup_seconds=0.5,
        )
    assert not marker.exists()


def test_managed_keeps_a_worker_that_started(tmp_path: Path) -> None:
    module = load("review-chain-runner")
    started = json.dumps({"type": "thread.started", "thread_id": "t"})
    program = f"import time; print({started!r}, flush=True); time.sleep(1.5)"
    module.managed(
        [sys.executable, "-c", program],
        tmp_path / "worker.log",
        dict(os.environ),
        30,
        startup_event="thread.started",
        startup_seconds=0.3,
    )


def test_managed_without_a_startup_event_never_stalls(tmp_path: Path) -> None:
    module = load("review-chain-runner")
    module.managed(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        tmp_path / "worker.log",
        dict(os.environ),
        30,
        startup_seconds=0.1,
    )


def test_codex_launcher_detaches_stdin_before_execution() -> None:
    lines = [
        line.strip()
        for line in (SCRIPTS / "run-codex-review.py").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    marker = lines.index('launch_state("execution")')
    assert lines[marker - 3 : marker] == [
        "devnull = os.open(os.devnull, os.O_RDONLY)",
        "os.dup2(devnull, 0)",
        "os.close(devnull)",
    ]


@pytest.fixture
def stall_harness(harness: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Codex hangs before thread.started and the watchdog stops it."""
    module = harness.module
    wrapped = module.managed
    controls = SimpleNamespace(
        stalls=1, engine="codex", side_effect=None, started=False, cleanup_denied=False
    )
    stalled: list[str | None] = []

    def managed(
        argv: list[str],
        log: Path,
        env: dict[str, str],
        timeout: int = 3600,
        **kwargs: Any,
    ) -> None:
        if (
            "AGENT_LOOP_REVIEW_RESULT_FILE" in env
            and env["AGENT_LOOP_REVIEW_ENGINE"] == controls.engine
            and controls.stalls
        ):
            controls.stalls -= 1
            stalled.append(kwargs.get("startup_event"))
            module.save(
                Path(env["ACTIVELOOM_LAUNCH_STATE"]),
                {
                    "version": 1,
                    "attempt_id": env["ACTIVELOOM_ATTEMPT_ID"],
                    "phase": "execution",
                    "review_started": None,
                },
            )
            text = STDIN_BANNER
            if controls.started:
                text += json.dumps({"type": "thread.started"}) + "\n"
            log.write_text(text)
            if controls.side_effect:
                controls.side_effect(log.parent)
            if controls.cleanup_denied:
                raise module.CleanupBlocked(
                    "codex exit not confirmed; process-group cleanup denied",
                    4242,
                    None,
                    False,
                )
            raise module.StartupStalled("codex emitted no thread.started event")
        wrapped(argv, log, env, timeout)

    monkeypatch.setattr(module, "managed", managed)
    return SimpleNamespace(harness=harness, controls=controls, stalled=stalled)


def test_codex_startup_stall_retries_the_same_round_once(
    stall_harness: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    h = stall_harness.harness
    runner = h.runner(h.args, h.directory)
    assert runner.run() == "converged"
    # The runner armed the watchdog for the Codex launch.
    assert stall_harness.stalled == ["thread.started"]
    assert h.launches == ["codex", "claude", "codex", "claude"]
    assert [(p["engine"], p["round"]) for p in runner.state["completed"]] == [
        ("codex", 1),
        ("claude", 1),
        ("codex", 2),
        ("claude", 2),
    ]
    assert len(runner.state["attempts"]) == 5
    failed, retry = runner.state["attempts"][:2]
    assert failed["phase"] == "startup_stall_failed"
    assert failed["failure_reason"] == "codex_startup_stall"
    assert failed["exit_status"] is None
    assert (failed["engine"], failed["round"]) == (retry["engine"], retry["round"])
    assert retry["folder"] == failed["folder"] + "/stall-retry"
    assert (h.directory / failed["folder"] / "worker.log").is_file()
    assert f"Codex startup stall: retrying codex pass 1 once at {HEAD}" in (
        capsys.readouterr().out
    )


def test_second_codex_startup_stall_blocks(stall_harness: Any) -> None:
    h = stall_harness.harness
    stall_harness.controls.stalls = 2
    runner = h.runner(h.args, h.directory)
    with pytest.raises(h.module.Blocked, match="no further retry"):
        runner.run()
    assert len(stall_harness.stalled) == 2
    assert runner.state["completed"] == []
    h.args.resume = True
    with pytest.raises(h.module.Blocked, match="no further retry"):
        h.runner(h.args, h.directory).run()
    assert len(stall_harness.stalled) == 2
    assert h.launches == []


@pytest.mark.parametrize(
    "case", ["head", "threads", "comments", "result", "partial", "started", "cleanup"]
)
def test_unsafe_codex_startup_stall_is_not_retried(
    stall_harness: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    h = stall_harness.harness
    controls = stall_harness.controls
    runner = h.runner(h.args, h.directory)
    if case == "head":
        controls.side_effect = lambda folder: monkeypatch.setattr(
            runner, "boundary", lambda: "f" * 40
        )
    elif case in ("threads", "comments"):
        controls.side_effect = lambda folder: monkeypatch.setattr(
            runner, case, lambda path: h.module.save(path, ["changed"])
        )
    elif case == "result":
        controls.side_effect = lambda folder: (folder / "result.json").write_text("{}")
    elif case == "partial":
        controls.side_effect = lambda folder: (
            folder / "result.json.recovery.json"
        ).write_text("{}")
    elif case == "started":
        controls.started = True
    else:
        controls.cleanup_denied = True
    with pytest.raises(h.module.Blocked):
        runner.run()
    assert len(stall_harness.stalled) == 1
    assert h.launches == []
    assert runner.state["completed"] == []
    assert len(runner.state["attempts"]) == 1


def test_another_engine_stall_is_not_retried(stall_harness: Any) -> None:
    h = stall_harness.harness
    stall_harness.controls.engine = "claude"
    runner = h.runner(h.args, h.directory)
    with pytest.raises(h.module.Blocked, match="no thread.started"):
        runner.run()
    # Only Codex arms the watchdog; this fake stall stands in for any failure.
    assert stall_harness.stalled == [None]
    assert runner.state["attempts"][1]["phase"] == "execution_failed"


def test_checkpoint_blocked_without_result_stays_blocked_on_resume(
    idle_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = idle_harness.harness
    # A checkpoint written before idle-exit classification existed.
    recognize = h.module.agy_idle_exit
    monkeypatch.setattr(h.module, "agy_idle_exit", lambda log: False)
    with pytest.raises(h.module.Blocked, match="returned no result"):
        h.runner(h.args, h.directory).run()
    saved = h.module.read(h.directory / "state.json")
    assert saved["pending"]["phase"] == "returned"
    monkeypatch.setattr(h.module, "agy_idle_exit", recognize)
    h.args.resume = True
    with pytest.raises(h.module.Blocked, match="returned no result"):
        h.runner(h.args, h.directory).run()
    assert idle_harness.idle_launches == ["gemini"]
    assert h.launches == ["codex"]


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
    check_launches: list[tuple[list[str], dict[str, str]]] = []
    controls = SimpleNamespace(
        outcome="clean",
        exit_code=0,
        missing=False,
        fail_attest=False,
        fail_check=False,
        preflight=False,
        finalization_failure=False,
        fail_recovery=False,
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
        argv: list[str],
        log: Path,
        env: dict[str, str],
        timeout: int = 3600,
        **_: Any,
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
            result_path = Path(env["AGENT_LOOP_REVIEW_RESULT_FILE"])
            try:
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
            finally:
                # The real sidecar is written by the worker before it exits, so
                # it must survive a cleanup failure raised on the way out.
                if controls.finalization_failure and result_path.is_file():
                    candidate = module.read(result_path)
                    module.save(
                        result_path,
                        {
                            **candidate,
                            "status": "blocked",
                            "finalLaneComplete": False,
                            "blocker": "Synthetic finalization failure",
                        },
                    )
                    module.save(
                        Path(str(result_path) + ".recovery.json"),
                        {"candidate": candidate, "blocked": module.digest(result_path)},
                    )
        elif controls.fail_check:
            raise module.Blocked("synthetic gate failure")
        else:
            check_launches.append((argv, env))
            real_managed(argv, log, env, 10)

    monkeypatch.setattr(module, "managed", managed)

    class FakeRunner(module.Runner):  # type: ignore[misc, name-defined]
        def preflight(self) -> None:
            pass

        def repository_root(self) -> Path:
            return Path.cwd().resolve()

        def target_revision(self) -> str:
            return BASE

        def merge_base(self, target: str, head: str) -> str:
            return BASE

        def validation_contract(self, revision: str) -> dict[str, Any] | None:
            return None

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
            if operation == "recover-result":
                if controls.fail_recovery:
                    raise module.Blocked("synthetic finalization verification failure")
                result_path = Path(options["--result-file"])
                receipt_path = Path(str(result_path) + ".recovery.json")
                assert (
                    module.digest(receipt_path) == options["--expected-recovery-sha256"]
                )
                receipt = module.read(receipt_path)
                current = module.read(result_path)
                if (
                    current != receipt["candidate"]
                    and module.digest(result_path) != receipt["blocked"]
                ):
                    raise module.Blocked("saved result changed")
                module.save(result_path, receipt["candidate"])
                return {"verified": True}
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
        check_launches=check_launches,
        real_managed=real_managed,
    )


def test_one_invocation_runs_all_fixed_steps(harness: Any) -> None:
    runner = harness.runner(harness.args, harness.directory)
    assert runner.run() == "converged"
    assert harness.launches == ["codex", "claude", "codex", "claude"]
    assert len(runner.state["completed"]) == 4


def test_completed_result_recovery_keeps_the_run_and_owed_pass(harness: Any) -> None:
    harness.controls.finalization_failure = True
    harness.controls.fail_recovery = True
    with pytest.raises(harness.module.Blocked, match="finalization verification"):
        harness.runner(harness.args, harness.directory).run()
    original = harness.module.read(harness.directory / "state.json")
    assert original["pending"]["phase"] == "returned"
    assert original["pending"]["result_recovery_sha256"]
    assert original["completed"] == []
    assert harness.launches == ["codex"]
    harness.controls.fail_recovery = False
    harness.controls.finalization_failure = False
    harness.args.resume = True
    resumed = harness.runner(harness.args, harness.directory)
    assert resumed.run() == "converged"
    assert resumed.state["run_id"] == original["run_id"]
    assert harness.launches == ["codex", "claude", "codex", "claude"]
    assert len(resumed.state["attempts"]) == 4


@pytest.mark.parametrize(
    "changed", ["receipt", "snapshot", "result", "late-receipt", "unknown-exit"]
)
def test_completed_result_recovery_rejects_unbound_evidence(
    harness: Any, changed: str
) -> None:
    harness.controls.finalization_failure = True
    harness.controls.fail_recovery = True
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    state = harness.module.read(harness.directory / "state.json")
    pending = state["pending"]
    folder = harness.directory / pending["folder"]
    if changed in ("receipt", "snapshot", "result"):
        name = {
            "receipt": "result.json.recovery.json",
            "snapshot": "historical.json",
            "result": "result.json",
        }[changed]
        path = folder / name
        path.write_text(path.read_text() + "\n")
    elif changed == "late-receipt":
        pending.pop("result_recovery_sha256")
    else:
        pending["phase"] = "launching"
    harness.module.save(harness.directory / "state.json", state)
    harness.controls.fail_recovery = False
    harness.args.resume = True
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    assert harness.launches == ["codex"]
    assert not harness.events


def test_completed_result_recovery_replays_after_attestation_interruption(harness: Any) -> None:
    harness.controls.finalization_failure = True
    harness.controls.fail_attest = True
    with pytest.raises(harness.module.Blocked, match="after remote attestation"):
        harness.runner(harness.args, harness.directory).run()
    harness.controls.finalization_failure = False
    harness.args.resume = True
    resumed = harness.runner(harness.args, harness.directory)
    assert resumed.run() == "converged"
    assert harness.launches == ["codex", "claude", "codex", "claude"]
    assert len(resumed.state["completed"]) == 4


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


PROVIDER_500 = (
    "API Error: 500 Internal server error. This is a server-side issue, "
    "usually temporary — try again in a moment.\n"
)


@pytest.fixture
def provider_500_harness(harness: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    original = harness.module.managed
    controls = SimpleNamespace(
        failures=1, side_effect=None, log=PROVIDER_500, exit_status=1,
        marker_phase="execution", engine="claude",
    )
    failures_seen: list[str] = []

    def managed(
        argv: list[str], log: Path, env: dict[str, str], timeout: int = 3600,
        **kwargs: Any,
    ) -> None:
        if (
            "AGENT_LOOP_REVIEW_RESULT_FILE" in env
            and env["AGENT_LOOP_REVIEW_ENGINE"] == controls.engine
            and controls.failures
        ):
            controls.failures -= 1
            failures_seen.append(env["ACTIVELOOM_ATTEMPT_ID"])
            harness.module.save(
                Path(env["ACTIVELOOM_LAUNCH_STATE"]),
                {
                    "version": 1,
                    "attempt_id": env["ACTIVELOOM_ATTEMPT_ID"],
                    "phase": controls.marker_phase,
                    "review_started": None,
                },
            )
            log.write_text(controls.log)
            if controls.side_effect:
                controls.side_effect(log.parent)
            raise harness.module.ProcessFailure(
                f"{controls.engine} exited", controls.exit_status
            )
        original(argv, log, env, timeout, **kwargs)

    monkeypatch.setattr(harness.module, "managed", managed)
    return SimpleNamespace(harness=harness, controls=controls, failures_seen=failures_seen)


@pytest.mark.parametrize(
    "text,expected",
    [
        (PROVIDER_500, True),
        ("bash: warning: setlocale: LC_ALL: unavailable\n" + PROVIDER_500, True),
        (
            "bash: warning: setlocale: LC_ALL: unavailable\n"
            "bash: warning: setlocale: LC_CTYPE: unavailable\n" + PROVIDER_500,
            True,
        ),
        ("reviewer output\n" + PROVIDER_500, False),
        (PROVIDER_500 + "reviewer output\n", False),
        ("API Error: 401 Unauthorized\n", False),
        ("API Error: 500 Internal server error.\n" * 2, False),
    ],
)
def test_claude_provider_500_recognition_requires_sole_diagnostic(
    tmp_path: Path, text: str, expected: bool
) -> None:
    log = tmp_path / "worker.log"
    log.write_text(text)
    assert load("review-chain-runner").claude_provider_500(log) is expected


def test_claude_provider_500_recognition_rejects_large_or_linked_logs(
    tmp_path: Path,
) -> None:
    recognize = load("review-chain-runner").claude_provider_500
    # One line, so only the size cap can reject it.
    oversized = tmp_path / "worker.log"
    oversized.write_text(PROVIDER_500.rstrip("\n") + "x" * 4096 + "\n")
    assert recognize(oversized) is False
    # Content that would otherwise match, so only the symlink check can reject it.
    target = tmp_path / "target.log"
    target.write_text(PROVIDER_500)
    link = tmp_path / "linked.log"
    link.symlink_to(target)
    assert recognize(link) is False
    assert recognize(tmp_path / "missing.log") is False


def test_claude_provider_500_retries_same_pass_without_spending_a_round(
    provider_500_harness: Any,
) -> None:
    h = provider_500_harness.harness
    runner = h.runner(h.args, h.directory)
    assert runner.run() == "converged"
    assert [(p["engine"], p["round"]) for p in runner.state["completed"]] == [
        ("codex", 1), ("claude", 1), ("codex", 2), ("claude", 2),
    ]
    assert len(runner.state["attempts"]) == 5
    failed, retry = runner.state["attempts"][1:3]
    assert failed["phase"] == "provider_500_failed"
    assert failed["failure_reason"] == "claude_provider_500"
    assert (failed["engine"], failed["round"]) == (retry["engine"], retry["round"])
    assert retry["folder"] == failed["folder"] + "/provider-retry"


def test_another_engine_provider_500_is_not_retried(
    provider_500_harness: Any,
) -> None:
    h = provider_500_harness.harness
    provider_500_harness.controls.engine = "codex"
    runner = h.runner(h.args, h.directory)
    with pytest.raises(h.module.Blocked, match="codex exited"):
        runner.run()
    assert len(provider_500_harness.failures_seen) == 1
    assert runner.state["attempts"][0]["phase"] == "execution_failed"
    assert runner.state["completed"] == []


def test_second_claude_provider_500_blocks(provider_500_harness: Any) -> None:
    h = provider_500_harness.harness
    provider_500_harness.controls.failures = 2
    runner = h.runner(h.args, h.directory)
    with pytest.raises(h.module.Blocked, match="no further retry"):
        runner.run()
    assert len(provider_500_harness.failures_seen) == 2
    assert len(runner.state["completed"]) == 1
    h.args.resume = True
    with pytest.raises(h.module.Blocked, match="no further retry"):
        h.runner(h.args, h.directory).run()
    assert len(provider_500_harness.failures_seen) == 2


@pytest.mark.parametrize(
    "case", ["head", "threads", "comments", "result", "partial", "log", "timeout", "marker"]
)
def test_unsafe_claude_provider_500_does_not_retry(
    provider_500_harness: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    h = provider_500_harness.harness
    runner = h.runner(h.args, h.directory)
    if case == "head":
        provider_500_harness.controls.side_effect = lambda folder: monkeypatch.setattr(
            runner, "boundary", lambda: "f" * 40
        )
    elif case in ("threads", "comments"):
        provider_500_harness.controls.side_effect = lambda folder: monkeypatch.setattr(
            runner, case, lambda path: h.module.save(path, ["changed"])
        )
    elif case in ("result", "partial"):
        filename = "result.json" if case == "result" else "result.json.recovery.json"
        provider_500_harness.controls.side_effect = lambda folder: (
            folder / filename
        ).write_text("{}")
    else:
        if case == "log":
            provider_500_harness.controls.log = "reviewer output\n" + PROVIDER_500
        elif case == "timeout":
            provider_500_harness.controls.exit_status = 124
        else:
            provider_500_harness.controls.marker_phase = "ready"
    with pytest.raises(h.module.Blocked):
        runner.run()
    assert len(provider_500_harness.failures_seen) == 1
    assert len(runner.state["completed"]) == 1


@pytest.mark.parametrize("evidence", ["unchanged", "log", "threads", "result"])
def test_prepared_provider_retry_rechecks_evidence_on_resume(
    provider_500_harness: Any, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    h = provider_500_harness.harness
    runner = h.runner(h.args, h.directory)
    recover = runner.recover_provider_500

    def interrupt(pending: dict[str, Any]) -> None:
        recover(pending)
        raise OSError("interrupted after provider-retry preparation")

    monkeypatch.setattr(runner, "recover_provider_500", interrupt)
    with pytest.raises(OSError, match="after provider-retry preparation"):
        runner.run()
    before = h.module.read(h.directory / "state.json")
    assert before["pending"]["phase"] == "prepared"
    h.args.resume = True
    resumed = h.runner(h.args, h.directory)
    origin = h.directory / before["attempts"][1]["folder"]
    if evidence == "log":
        (origin / "worker.log").write_text(PROVIDER_500 + "changed\n")
    elif evidence == "threads":
        monkeypatch.setattr(
            resumed, "threads", lambda path: h.module.save(path, ["changed"])
        )
    elif evidence == "result":
        (origin / "result.json").write_text("{}")
    if evidence == "unchanged":
        assert resumed.run() == "converged"
        assert resumed.state["run_id"] == before["run_id"]
    else:
        with pytest.raises(h.module.Blocked, match="evidence changed"):
            resumed.run()
        assert len(provider_500_harness.failures_seen) == 1


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


def validation_contract(
    module: ModuleType,
    *,
    base_environment: dict[str, str] | None = None,
    policy_revision: str = BASE,
) -> dict[str, Any]:
    document = {
        "schema_version": 1,
        "fallback_gate": "full",
        "gates": {
            "backend": {
                "paths": ["apps/backend/**", "packages/shared/**"],
                "commands": [{"argv": [sys.executable, "-c", "pass"]}],
                "environment": {"NODE_ENV": "development"},
            },
            "full": {
                "commands": [{"argv": [sys.executable, "-c", "pass"]}],
                "environment": {"CI": "true"},
            },
        },
    }
    raw = json.dumps(document).encode()
    return {
        "mode": "contract-v1",
        "path": module.VALIDATION_CONTRACT,
        "policy_revision": policy_revision,
        "manifest_sha256": module.hashlib.sha256(raw).hexdigest(),
        "contract": module.parse_validation_contract(raw),
        "base_environment": base_environment or {"PATH": "/test/bin"},
    }


def test_target_revision_pins_the_live_base_tip_not_a_stale_base_ref_oid(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = "c" * 40
    calls: list[list[str]] = []

    def command(argv: list[str]) -> str:
        calls.append(argv)
        if argv[:2] == ["gh", "pr"]:
            assert "baseRefOid" not in argv
            return "staging"
        if argv[:2] == ["git", "ls-remote"]:
            assert argv[-1] == "refs/heads/staging"
            return f"{live}\trefs/heads/staging"
        if argv[:2] == ["git", "fetch"]:
            return ""
        raise AssertionError(argv)

    monkeypatch.setattr(harness.module, "command", command)
    runner = harness.runner(harness.args, harness.directory)
    assert harness.module.Runner.target_revision(runner) == live
    assert calls[-1] == ["git", "fetch", "--quiet", "--no-tags", "origin", live]


def test_repository_contract_selects_gate_and_scrubs_ambient_environment(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    contract = validation_contract(h.module, policy_revision=HEAD)
    revisions: list[str] = []
    h.args.check = None
    monkeypatch.setenv("VITEST_MAX_WORKERS", "99")
    monkeypatch.setenv("EXAMPLE_SECRET", "must-not-leak")
    monkeypatch.setattr(h.runner, "target_revision", lambda self: HEAD)

    def contract_for_revision(self: Any, revision: str) -> dict[str, Any]:
        revisions.append(revision)
        return contract

    monkeypatch.setattr(
        h.runner,
        "validation_contract",
        contract_for_revision,
    )
    monkeypatch.setattr(
        h.runner, "changed_paths", lambda self, base, head: ["apps/backend/api.py"]
    )

    runner = h.runner(h.args, h.directory)
    assert runner.run() == "converged"
    assert runner.state["config"]["checks"] == []
    assert runner.state["config"]["validation"] == contract
    assert runner.state["config"]["validation_policy_revision"] == HEAD
    assert revisions == [HEAD]
    assert len(h.check_launches) == 4
    for argv, environment in h.check_launches:
        assert argv == [sys.executable, "-c", "pass"]
        assert environment == {"PATH": "/test/bin", "NODE_ENV": "development"}
    for number in range(1, 5):
        receipt = h.module.read(h.directory / f"pass-{number}" / "validated.json")
        assert receipt["mode"] == "contract-v1"
        assert receipt["gates"] == ["backend"]
        assert receipt["manifest_sha256"] == contract["manifest_sha256"]


def test_repository_contract_adds_fallback_for_unmatched_paths(harness: Any) -> None:
    runner = harness.runner(harness.args, harness.directory)
    runner.state = {
        "base": BASE,
        "config": {
            "checks": [],
            "validation": validation_contract(harness.module),
        },
    }
    runner.changed_paths = lambda base, head: [
        "apps/backend/api.py",
        "docs/operator.md",
    ]
    resolved = runner.resolved_validation(HEAD)
    assert resolved["gates"] == ["backend", "full"]
    assert [command["environment"] for command in resolved["commands"]] == [
        {"PATH": "/test/bin", "NODE_ENV": "development"},
        {"PATH": "/test/bin", "CI": "true"},
    ]


def test_always_gate_does_not_claim_fallback_path_coverage(harness: Any) -> None:
    contract = validation_contract(harness.module)
    contract["contract"]["gates"] = {
        "baseline": {
            "paths": [],
            "always": True,
            "commands": [["just", "lint"]],
            "environment": {},
        },
        **contract["contract"]["gates"],
    }
    runner = harness.runner(harness.args, harness.directory)
    runner.state = {
        "base": BASE,
        "config": {"checks": [], "validation": contract},
    }
    runner.changed_paths = lambda base, head: ["docs/operator.md"]

    resolved = runner.resolved_validation(HEAD)
    assert resolved["gates"] == ["baseline", "full"]


def test_repository_contract_rejects_legacy_checks_after_opt_in(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = validation_contract(harness.module)
    monkeypatch.setattr(
        harness.runner, "validation_contract", lambda self, base: contract
    )
    with pytest.raises(harness.module.Blocked, match="remove --check"):
        harness.runner(harness.args, harness.directory).run()
    assert harness.launches == []


def test_new_run_requires_the_pull_request_merge_base(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        harness.runner, "merge_base", lambda self, target, head: HEAD
    )
    with pytest.raises(harness.module.Blocked, match="pull request merge base"):
        harness.runner(harness.args, harness.directory).run()
    assert harness.launches == []


def test_runner_requires_the_repository_worktree_root(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        harness.runner, "repository_root", lambda self: Path.cwd().parent
    )
    with pytest.raises(harness.module.Blocked, match="worktree root"):
        harness.runner(harness.args, harness.directory).run()
    assert harness.launches == []


def test_legacy_repository_still_requires_a_check(harness: Any) -> None:
    harness.args.check = None
    with pytest.raises(harness.module.Blocked, match="pass --check"):
        harness.runner(harness.args, harness.directory).run()
    assert harness.launches == []


def test_repository_contract_rejects_environment_drift_on_resume(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.args.check = None
    harness.controls.fail_check = True
    monkeypatch.setenv("HOME", "/first/home")
    monkeypatch.setattr(
        harness.runner,
        "validation_contract",
        lambda self, base: validation_contract(
            harness.module, base_environment={"HOME": os.environ["HOME"]}
        ),
    )
    monkeypatch.setattr(
        harness.runner, "changed_paths", lambda self, base, head: ["docs/change.md"]
    )
    with pytest.raises(harness.module.Blocked, match="synthetic gate failure"):
        harness.runner(harness.args, harness.directory).run()
    assert harness.launches == ["codex"]

    harness.args.resume = True
    harness.controls.fail_check = False
    monkeypatch.setenv("HOME", "/second/home")
    with pytest.raises(harness.module.Blocked, match="same plan"):
        harness.runner(harness.args, harness.directory).run()
    assert harness.launches == ["codex"]


def test_validation_contract_is_loaded_from_pinned_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("review-chain-runner")
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repository, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    manifest = repository / module.VALIDATION_CONTRACT
    valid = {
        "schema_version": 1,
        "fallback_gate": "full",
        "gates": {"full": {"commands": [{"argv": ["true"]}]}},
    }
    manifest.write_text(json.dumps(valid))
    git("add", module.VALIDATION_CONTRACT)
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    manifest.write_text('{"schema_version": 999}')
    git("add", module.VALIDATION_CONTRACT)
    git("commit", "-qm", "head changes its own contract")
    monkeypatch.chdir(repository)

    runner = module.Runner(SimpleNamespace(), tmp_path / "state")
    contract = runner.validation_contract(base)
    assert contract is not None
    assert contract["contract"] == module.parse_validation_contract(
        json.dumps(valid).encode()
    )
    assert contract["manifest_sha256"] == module.hashlib.sha256(
        json.dumps(valid).encode()
    ).hexdigest()


def test_standalone_contract_validation_supports_adoption_prs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load("review-chain-runner")
    document = {
        "schema_version": 1,
        "fallback_gate": "full",
        "gates": {"full": {"commands": [{"argv": ["true"]}]}},
    }
    path = tmp_path / module.VALIDATION_CONTRACT
    path.write_text(json.dumps(document))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / module.VALIDATION_CONTRACT).write_text(json.dumps(document))
    monkeypatch.chdir(nested)
    assert module.main(["--validate-contract"]) == 2
    assert "worktree root" in capsys.readouterr().err
    monkeypatch.chdir(tmp_path)

    assert module.main(["--validate-contract"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "valid"
    assert output["gates"] == ["full"]
    assert output["sha256"] == module.digest(path)


def test_standalone_contract_validation_rejects_invalid_or_linked_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load("review-chain-runner")
    path = tmp_path / module.VALIDATION_CONTRACT
    path.write_text('{"schema_version": true}')
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    assert module.main(["--validate-contract"]) == 2
    assert "validation contract invalid" in capsys.readouterr().err

    target = tmp_path / "target.json"
    target.write_text(
        '{"schema_version":1,"fallback_gate":"full","gates":'
        '{"full":{"commands":[{"argv":["true"]}]}}}'
    )
    path.unlink()
    path.symlink_to(target)
    assert module.main(["--validate-contract"]) == 2
    assert "expected a regular file" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda value: value.update(schema_version=2), "schema_version"),
        (lambda value: value.update(schema_version=True), "schema_version"),
        (lambda value: value.update(extra=True), "unknown contract field"),
        (
            lambda value: value["gates"]["backend"].update(environment={"API_TOKEN": "x"}),
            "forbidden environment name",
        ),
        (
            lambda value: value["gates"]["backend"].update(commands=[{"argv": "test"}]),
            "argv arrays",
        ),
        (
            lambda value: value["gates"]["backend"].update(paths=[]),
            "paths or always true",
        ),
        (
            lambda value: value["gates"]["backend"].update(always=True),
            "not both",
        ),
    ],
)
def test_validation_contract_rejects_unsafe_or_ambiguous_schema(
    mutate: Any, message: str
) -> None:
    module = load("review-chain-runner")
    document = {
        "schema_version": 1,
        "fallback_gate": "full",
        "gates": {
            "backend": {
                "paths": ["apps/backend/**"],
                "commands": [{"argv": ["just", "test", "backend"]}],
            },
            "full": {"commands": [{"argv": ["just", "test"]}]},
        },
    }
    mutate(document)
    with pytest.raises(module.Blocked, match=message):
        module.parse_validation_contract(json.dumps(document).encode())


def test_validation_contract_rejects_duplicate_json_keys() -> None:
    module = load("review-chain-runner")
    raw = b'{"schema_version":1,"schema_version":1,"fallback_gate":"full","gates":{}}'
    with pytest.raises(module.Blocked, match="duplicate validation contract key"):
        module.parse_validation_contract(raw)


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("apps/backend/api.py", "apps/backend/**", True),
        ("apps/backend/services/api.py", "apps/backend/**", True),
        ("apps/backend/services/api.py", "apps/*", False),
        ("README.md", "**/*.md", True),
        ("docs/guide.md", "**/*.md", True),
        ("docs/guide.rst", "**/*.md", False),
    ],
)
def test_validation_path_matching_is_slash_aware(
    path: str, pattern: str, expected: bool
) -> None:
    assert load("review-chain-runner").validation_path_matches(path, pattern) is expected


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


def test_completed_worker_resumes_after_cleanup_group_disappears(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_killpg = os.killpg

    def denied(pid: int, sig: int) -> None:
        raise PermissionError(1, "synthetic cleanup denial")

    monkeypatch.setattr(harness.module.os, "killpg", denied)
    with pytest.raises(harness.module.Blocked, match="process-group cleanup denied"):
        harness.runner(harness.args, harness.directory).run()
    state = harness.module.read(harness.directory / "state.json")
    assert harness.launches == ["codex"]
    assert state["completed"] == []
    assert state["attempts"][0]["exit_status"] == 0
    stale = state["attempts"][0]["process_group"]

    # The resumed chain launches real workers, so only the reaped group is
    # stubbed; probing its recycled pid for real would depend on the host.
    def probe(pid: int, sig: int) -> None:
        if pid == stale:
            assert sig == 0, "recovery must not send another cleanup signal"
            raise ProcessLookupError(3, "synthetic absent group")
        real_killpg(pid, sig)

    monkeypatch.setattr(harness.module.os, "killpg", probe)
    harness.args.resume = True
    resumed = harness.runner(harness.args, harness.directory)
    assert resumed.run() == "converged"
    assert resumed.state["run_id"] == state["run_id"]
    assert harness.launches == ["codex", "claude", "codex", "claude"]
    assert len(resumed.state["attempts"]) == 4


@pytest.mark.parametrize(
    "obstacle,refusal",
    [
        ("live", "process group still exists"),
        ("denied", "cannot inspect the process group"),
        ("result", "evidence changed after cleanup failure"),
        ("log", "evidence changed after cleanup failure"),
        ("late-receipt", "evidence changed after cleanup failure"),
        ("exit", "requires a recorded successful worker exit"),
        ("exit-false", "requires a recorded successful worker exit"),
        ("identity", "requires a recorded successful worker exit"),
        ("duplicate", "cleanup recovery attempt changed"),
        ("group-missing", "process group is unavailable"),
        ("group-bool", "process group is unavailable"),
    ],
)
def test_cleanup_recovery_requires_absence_and_unchanged_completed_evidence(
    harness: Any, monkeypatch: pytest.MonkeyPatch, obstacle: str, refusal: str
) -> None:
    def denied(pid: int, sig: int) -> None:
        raise PermissionError(1, "synthetic cleanup denial")

    monkeypatch.setattr(harness.module.os, "killpg", denied)
    with pytest.raises(harness.module.Blocked, match="process-group cleanup denied"):
        harness.runner(harness.args, harness.directory).run()
    state = harness.module.read(harness.directory / "state.json")
    folder = harness.directory / state["pending"]["folder"]
    if obstacle in ("result", "log"):
        path = folder / ("result.json" if obstacle == "result" else "worker.log")
        path.write_text(path.read_text() + "\n")
    elif obstacle == "late-receipt":
        (folder / "result.json.recovery.json").write_text("{}")
    elif obstacle == "exit":
        state["attempts"][0]["exit_status"] = None
    elif obstacle == "exit-false":
        state["attempts"][0]["exit_status"] = False
    elif obstacle == "identity":
        state["attempts"][0]["engine"] = "claude"
    elif obstacle == "duplicate":
        state["attempts"].append(dict(state["attempts"][0]))
    elif obstacle == "group-missing":
        state["attempts"][0].pop("process_group")
    elif obstacle == "group-bool":
        state["attempts"][0]["process_group"] = True
    harness.module.save(harness.directory / "state.json", state)

    def probe(pid: int, sig: int) -> None:
        assert sig == 0, "recovery must not send another cleanup signal"
        if obstacle == "denied":
            raise PermissionError(1, "synthetic denied probe")
        if obstacle != "live":
            raise ProcessLookupError(3, "synthetic absent group")

    monkeypatch.setattr(harness.module.os, "killpg", probe)
    harness.args.resume = True
    resumed = harness.runner(harness.args, harness.directory)
    with pytest.raises(harness.module.Blocked, match=refusal):
        resumed.run()
    assert harness.launches == ["codex"]
    assert not harness.events
    # The refusal must come from the recovery guard, leaving the sealed attempt
    # exactly as it was rather than from some later stage after a promotion.
    blocked = harness.module.read(harness.directory / "state.json")
    assert blocked["pending"]["phase"] == "cleanup_blocked"
    assert blocked["attempts"][0]["phase"] == "cleanup_blocked"
    assert "cleanup_reconciled" not in blocked["attempts"][0]


def test_cleanup_denial_survives_an_unreadable_completed_output(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_digest = harness.module.digest

    def unreadable(path: Path) -> str:
        if path.name == "result.json":
            raise OSError(13, "synthetic unreadable result")
        return str(real_digest(path))

    def denied(pid: int, sig: int) -> None:
        raise PermissionError(1, "synthetic cleanup denial")

    monkeypatch.setattr(harness.module.os, "killpg", denied)
    monkeypatch.setattr(harness.module, "digest", unreadable)
    # An I/O error while sealing must not displace the cleanup denial or leave
    # the attempt half-sealed; only Blocked being caught would let it through.
    with pytest.raises(harness.module.Blocked, match="process-group cleanup denied"):
        harness.runner(harness.args, harness.directory).run()
    state = harness.module.read(harness.directory / "state.json")
    assert state["attempts"][0]["exit_status"] == 0
    assert state["attempts"][0]["phase"] != "cleanup_blocked"
    assert state["pending"]["phase"] != "cleanup_blocked"
    assert "cleanup_result_sha256" not in state["pending"]
    assert harness.launches == ["codex"]


@pytest.mark.parametrize("tamper", [False, True])
def test_cleanup_denial_seals_the_finalization_receipt(
    harness: Any, monkeypatch: pytest.MonkeyPatch, tamper: bool
) -> None:
    real_killpg = os.killpg
    harness.controls.finalization_failure = True

    def denied(pid: int, sig: int) -> None:
        raise PermissionError(1, "synthetic cleanup denial")

    monkeypatch.setattr(harness.module.os, "killpg", denied)
    with pytest.raises(harness.module.Blocked, match="process-group cleanup denied"):
        harness.runner(harness.args, harness.directory).run()
    state = harness.module.read(harness.directory / "state.json")
    pending = state["pending"]
    folder = harness.directory / pending["folder"]
    receipt = folder / "result.json.recovery.json"
    # The seal must bind the receipt observed at the worker's exit, not a
    # sidecar that appears while the group is being reconciled.
    assert pending["phase"] == "cleanup_blocked"
    assert pending["result_recovery_sha256"] == harness.module.digest(receipt)
    if tamper:
        receipt.write_text(receipt.read_text() + "\n")
    stale = state["attempts"][0]["process_group"]

    def probe(pid: int, sig: int) -> None:
        if pid == stale:
            assert sig == 0, "recovery must not send another cleanup signal"
            raise ProcessLookupError(3, "synthetic absent group")
        real_killpg(pid, sig)

    monkeypatch.setattr(harness.module.os, "killpg", probe)
    harness.controls.finalization_failure = False
    harness.args.resume = True
    resumed = harness.runner(harness.args, harness.directory)
    if tamper:
        with pytest.raises(
            harness.module.Blocked, match="evidence changed after cleanup failure"
        ):
            resumed.run()
        assert harness.launches == ["codex"]
        assert not harness.events
        return
    assert resumed.run() == "converged"
    assert resumed.state["run_id"] == state["run_id"]
    assert harness.launches == ["codex", "claude", "codex", "claude"]


@pytest.mark.parametrize("exit_code,missing", [(7, False), (0, True)])
def test_cleanup_denial_does_not_make_an_incomplete_review_resumable(
    harness: Any, monkeypatch: pytest.MonkeyPatch, exit_code: int, missing: bool
) -> None:
    real_killpg = os.killpg
    harness.controls.exit_code = exit_code
    harness.controls.missing = missing

    def denied(pid: int, sig: int) -> None:
        raise PermissionError(1, "synthetic cleanup denial")

    monkeypatch.setattr(harness.module.os, "killpg", denied)
    with pytest.raises(harness.module.Blocked, match="process-group cleanup denied"):
        harness.runner(harness.args, harness.directory).run()
    state = harness.module.read(harness.directory / "state.json")
    assert state["attempts"][0]["exit_status"] == exit_code
    # Unsealable output never half-seals the attempt, and never displaces the
    # cleanup denial the operator has to act on.
    assert state["attempts"][0]["phase"] != "cleanup_blocked"
    assert state["pending"]["phase"] != "cleanup_blocked"
    assert "cleanup_result_sha256" not in state["pending"]
    monkeypatch.setattr(harness.module.os, "killpg", real_killpg)
    harness.controls.exit_code = 0
    harness.controls.missing = False
    harness.args.resume = True
    with pytest.raises(harness.module.Blocked):
        harness.runner(harness.args, harness.directory).run()
    assert harness.launches == ["codex"]
    assert not harness.events


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
        argv: list[str],
        log: Path,
        env: dict[str, str],
        timeout: int = 3600,
        **_: Any,
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
        argv: list[str],
        log: Path,
        env: dict[str, str],
        timeout: int = 3600,
        **_: Any,
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
        argv: list[str],
        log: Path,
        env: dict[str, str],
        timeout: int = 3600,
        **_: Any,
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
        argv: list[str],
        log: Path,
        env: dict[str, str],
        timeout: int = 3600,
        **_: Any,
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
        argv: list[str],
        log: Path,
        env: dict[str, str],
        timeout: int = 3600,
        **_: Any,
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
