"""Process-lifetime tests for agent-loop hook isolation."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = ROOT / ".codex/skills/agent-loop/scripts/process-supervisor.py"


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
    marker = tmp_path / "detached-child-survived"
    child = (
        "import os,time; from pathlib import Path; "
        "pid=os.fork(); "
        f"(os.setsid(), time.sleep(0.4), Path({str(marker)!r}).write_text('bad')) "
        "if pid == 0 else None"
    )

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
    time.sleep(0.5)

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
