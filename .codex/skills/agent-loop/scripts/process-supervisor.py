#!/usr/bin/env python3
"""Run one hook and ensure none of its descendants survive."""

from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


PR_SET_CHILD_SUBREAPER = 36


class TerminationRequested(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def _become_subreaper() -> None:
    if not Path("/proc/self/stat").is_file():
        raise RuntimeError("hook supervision requires Linux procfs")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _descendants(root_pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    parents: dict[int, int] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # The comm field can contain spaces and parentheses; PPID follows its
            # final closing parenthesis in /proc/<pid>/stat.
            fields = (
                entry.joinpath("stat")
                .read_text(encoding="utf-8")
                .rsplit(")", 1)[1]
                .split()
            )
            parents[int(entry.name)] = int(fields[1])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (IndexError, PermissionError, ValueError) as error:
            raise RuntimeError(
                f"could not reliably inspect process {entry.name}"
            ) from error
    found: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {pid for pid, parent in parents.items() if parent in frontier}
        children -= found
        if not children:
            break
        found.update(children)
        frontier = children
    return found


def _signal_descendants(root_pid: int, sig: signal.Signals) -> bool:
    descendants = _descendants(root_pid)
    for pid in sorted(descendants, reverse=True):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
    return bool(descendants)


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _cleanup(root_pid: int, grace_seconds: float) -> None:
    for sig, budget in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, 1.0)):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if not _signal_descendants(root_pid, sig):
                _reap()
                if not _descendants(root_pid):
                    return
            _reap()
            time.sleep(0.025)
    raise RuntimeError("hook descendants survived forced cleanup")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--kill-after-seconds", type=float, default=15.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    _become_subreaper()
    if args.self_test:
        print("linux-subreaper-v1")
        return 0
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if (
        not command
        or args.timeout_seconds is None
        or args.timeout_seconds <= 0
        or args.kill_after_seconds < 0
    ):
        parser.error("a command and positive timeout are required")

    received_signal: int | None = None
    cleaning_up = False

    def request_termination(signum: int, _frame: object) -> None:
        nonlocal received_signal, cleaning_up
        if received_signal is None:
            received_signal = signum
            if not cleaning_up:
                raise TerminationRequested(signum)

    handled_signals = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    previous_handlers = {
        sig: signal.signal(sig, request_termination) for sig in handled_signals
    }
    child: subprocess.Popen[bytes] | None = None
    timed_out = False
    return_code = 0
    try:
        child = subprocess.Popen(command, start_new_session=True)
        try:
            return_code = child.wait(timeout=args.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=args.kill_after_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
    except TerminationRequested:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    finally:
        cleaning_up = True
        _cleanup(os.getpid(), args.kill_after_seconds)
        _reap()
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)

    if received_signal is not None:
        return 128 + received_signal
    if timed_out:
        return 124
    if return_code < 0:
        return 128 - return_code
    return return_code


if __name__ == "__main__":
    sys.exit(main())
