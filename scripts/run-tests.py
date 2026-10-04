#!/usr/bin/env python3
"""Bound local Python validation without turning a fallback into a full regression."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import FrameType


ROOT = Path(__file__).resolve().parents[1]
LIMITS = {"fast": (120, 2), "focused": (120, 2), "regression": (1200, 4)}


class StopRequested(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def request_stop(signum: int, frame: FrameType | None) -> None:
    raise StopRequested(signum)


def stop(process: subprocess.Popen[bytes]) -> None:
    """Stop the owned process group, including workers after their parent exits."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()
    process.wait()


def run(argv: list[str], timeout: float) -> int:
    with subprocess.Popen(argv, cwd=ROOT, start_new_session=os.name == "posix") as process:
        previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"Test lane exceeded {timeout:g}s; stopping its workers.", file=sys.stderr)
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
            stop(process)
            return 124
        except StopRequested as error:
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
            stop(process)
            return 128 + error.signum
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("lane", nargs="?", choices=tuple(LIMITS), default="fast")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    default_timeout, workers = LIMITS[args.lane]
    timeout = args.timeout_seconds if args.timeout_seconds is not None else default_timeout
    if not math.isfinite(timeout) or timeout <= 0:
        parser.error("--timeout-seconds must be finite and positive")
    argv = [
        sys.executable, "-m", "pytest", *args.pytest_args,
        f"--test-lane={args.lane}", "-n", str(workers), "--dist=worksteal", "--durations=10",
    ]
    print(f"Test lane: {args.lane}; deadline: {timeout:g}s; workers: {workers}", flush=True)
    return run(argv, timeout)


if __name__ == "__main__":
    raise SystemExit(main())
