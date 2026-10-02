"""Process-lifetime tests for agent-loop hook isolation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = ROOT / ".codex/skills/agent-loop/scripts/process-supervisor.py"


def _load_supervisor() -> Any:
    spec = importlib.util.spec_from_file_location("process_supervisor", SUPERVISOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _detaching_command(pid_file: Path, *, parent_sleep: float) -> str:
    return f"""
import os
from pathlib import Path
import time

pid = os.fork()
if pid == 0:
    os.setsid()
    Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(10)
    os._exit(0)
deadline = time.monotonic() + 2
while not Path({str(pid_file)!r}).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
time.sleep({parent_sleep})
"""


def _assert_recorded_process_dead(pid_file: Path) -> None:
    assert pid_file.is_file()
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not Path(f"/proc/{pid}").exists()


def test_supervisor_self_test() -> None:
    result = subprocess.run(
        [sys.executable, str(SUPERVISOR), "--self-test"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "linux-subreaper-v1"


def test_descendant_enumeration_fails_closed_on_malformed_children(
    tmp_path: Path,
) -> None:
    supervisor = _load_supervisor()
    root_task = tmp_path / "1/task/1"
    child_task = tmp_path / "123/task/123"
    root_task.mkdir(parents=True)
    child_task.mkdir(parents=True)
    (root_task / "children").write_text("123\n", encoding="utf-8")
    (child_task / "children").write_text("malformed\n", encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="could not reliably inspect children of owned process 123"
    ):
        supervisor._descendants(1, tmp_path)


def test_descendant_enumeration_ignores_unrelated_proc_entries(tmp_path: Path) -> None:
    supervisor = _load_supervisor()
    root_task = tmp_path / "1/task/1"
    root_task.mkdir(parents=True)
    (root_task / "children").write_text("\n", encoding="utf-8")
    unrelated = tmp_path / "999"
    unrelated.mkdir()

    assert supervisor._descendants(1, tmp_path) == set()


def test_supervisor_kills_double_forked_session(tmp_path: Path) -> None:
    pid_file = tmp_path / "detached.pid"

    result = subprocess.run(
        [
            sys.executable,
            str(SUPERVISOR),
            "--timeout-seconds",
            "5",
            "--kill-after-seconds",
            "0.2",
            "--",
            sys.executable,
            "-c",
            _detaching_command(pid_file, parent_sleep=0),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    _assert_recorded_process_dead(pid_file)


def test_supervisor_kills_detached_session_on_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "timeout.pid"
    result = subprocess.run(
        [
            sys.executable,
            str(SUPERVISOR),
            "--timeout-seconds",
            "1",
            "--kill-after-seconds",
            "0.2",
            "--",
            sys.executable,
            "-c",
            _detaching_command(pid_file, parent_sleep=10),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 124, result.stderr
    _assert_recorded_process_dead(pid_file)


def test_supervisor_cleans_up_when_terminated(tmp_path: Path) -> None:
    pid_file = tmp_path / "terminated.pid"
    process = subprocess.Popen(
        [
            sys.executable,
            str(SUPERVISOR),
            "--timeout-seconds",
            "30",
            "--kill-after-seconds",
            "0.2",
            "--",
            sys.executable,
            "-c",
            _detaching_command(pid_file, parent_sleep=10),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    process.terminate()
    _, stderr = process.communicate(timeout=10)

    assert process.returncode == 128 + signal.SIGTERM, stderr
    _assert_recorded_process_dead(pid_file)


def test_signal_during_cleanup_does_not_interrupt_reaping(tmp_path: Path) -> None:
    pid_file = tmp_path / "cleanup-signal.pid"
    child = f"""
import os
from pathlib import Path
import signal
import time

parent = os.getpid()
pid = os.fork()
if pid == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    path = Path({str(pid_file)!r})
    path.write_text(str(os.getpid()), encoding="utf-8")
    while os.getppid() == parent:
        time.sleep(0.005)
    os.kill(os.getppid(), signal.SIGTERM)
    time.sleep(10)
    os._exit(0)
deadline = time.monotonic() + 2
while not Path({str(pid_file)!r}).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
"""
    result = subprocess.run(
        [
            sys.executable,
            str(SUPERVISOR),
            "--timeout-seconds",
            "5",
            "--kill-after-seconds",
            "0.2",
            "--",
            sys.executable,
            "-c",
            child,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 128 + signal.SIGTERM, result.stderr
    _assert_recorded_process_dead(pid_file)


def test_supervisor_kills_active_fork_churn(tmp_path: Path) -> None:
    pid_file = tmp_path / "fork-churn.pids"
    child = f"""
import os
from pathlib import Path
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = Path({str(pid_file)!r})
path.write_text(str(os.getpid()) + "\\n", encoding="utf-8")
while True:
    pid = os.fork()
    if pid == 0:
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(str(os.getpid()) + "\\n")
        time.sleep(5)
        os._exit(0)
    time.sleep(0.01)
"""
    result = subprocess.run(
        [
            sys.executable,
            str(SUPERVISOR),
            "--timeout-seconds",
            "1",
            "--kill-after-seconds",
            "0.1",
            "--",
            sys.executable,
            "-c",
            child,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 124, result.stderr
    pids = {int(value) for value in pid_file.read_text(encoding="utf-8").splitlines()}
    assert pids
    assert not [pid for pid in pids if Path(f"/proc/{pid}").exists()]


def _run_main(module: Any, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", ["process-supervisor.py", *argv])
    return int(module.main())


def test_cleanup_failure_makes_a_timeout_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A containment failure must not return the retryable timeout status."""
    module = _load_supervisor()

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("hook descendants survived forced cleanup")

    monkeypatch.setattr(module, "_cleanup", _raise)
    status = _run_main(
        module,
        ["--timeout-seconds", "0.2", "--kill-after-seconds", "0.2", "--", "sleep", "10"],
        monkeypatch,
    )
    assert status == 125


def test_cleanup_failure_does_not_fail_a_successful_hook_as_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure is reported on stderr and surfaced as a plain exit code."""
    module = _load_supervisor()

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("hook descendants survived forced cleanup")

    monkeypatch.setattr(module, "_cleanup", _raise)
    status = _run_main(
        module,
        ["--timeout-seconds", "5", "--kill-after-seconds", "0.2", "--", "true"],
        monkeypatch,
    )
    assert status == 125
    assert "survived forced cleanup" in capsys.readouterr().err


def test_cleanup_failure_overrides_a_hook_failure_with_containment_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surviving descendants outrank the hook's own non-zero status."""
    module = _load_supervisor()

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("hook descendants survived forced cleanup")

    monkeypatch.setattr(module, "_cleanup", _raise)
    status = _run_main(
        module,
        ["--timeout-seconds", "5", "--kill-after-seconds", "0.2", "--", "false"],
        monkeypatch,
    )
    assert status == 125
