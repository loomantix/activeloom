"""Process-lifetime tests for agent-loop hook isolation."""

from __future__ import annotations

from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = ROOT / ".codex/skills/agent-loop/scripts/process-supervisor.py"


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
            "0.2",
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
            "0.2",
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
