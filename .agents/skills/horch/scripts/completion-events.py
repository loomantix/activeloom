#!/usr/bin/env python3
"""Durable completion-event inbox for the horch orchestrator.

A session the orchestrator started records one event when it finishes (a build's
draft PR, a review run's end, a ready flip). The orchestrator's blocking `wait`
returns as soon as an unacknowledged event is in the inbox, and otherwise after
the 300-second watchdog interval, so a lost event only costs latency.

Contract:

- An event holds identifiers and enums only: repository, issue, PR, full head,
  run ID, kind, outcome, source session name and a UTC timestamp. No free text.
- `record` persists before it wakes anyone. The event is written to a private
  temporary file, fsynced, and hard-linked into `pending/`. Readers sync the
  directory after listing it so only durable entries can wake a waiter.
- The event ID is a digest of the identity fields. Recording the same event
  again, before or after acknowledgement, changes nothing (`duplicate`).
- `ack` moves an event from `pending/` to `acked/`; acknowledging it again
  changes nothing (`already-acknowledged`).
- A `pending/` entry that can never be read as a valid event (bad content, a
  name that is not its ID, not a regular file, a symlink, unreadable, larger
  than 4 KiB) is moved to `quarantine/` and counted once in `invalid`, so it
  wakes a waiter once rather than on every call. A transient read error leaves the entry in place.
- An event grants no authority. It tells the orchestrator where to look; the
  orchestrator still re-reads GitHub, the run state and the pane before acting.

The helper launches no process and makes no network or GitHub calls.

Exit codes: 0 success, 2 invalid input or inbox, 124 `wait` reached its timeout.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = "horch-completion-event:v1"
WATCHDOG_SECONDS = 300
KINDS = {"build", "review-run", "ready-flip", "dep-fix"}
OUTCOMES = {
    "pr-opened",
    "question",
    "failed",
    "converged",
    "exhausted",
    "ended",
    "blocked",
    "ready",
}
IDENTITY = ("kind", "repo", "issue", "pr", "head", "run_id", "outcome")
FIELDS = {"schema", "id", "at", "source", *IDENTITY}
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
HEAD_RE = re.compile(r"[0-9a-f]{40}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
ID_RE = re.compile(r"[0-9a-f]{32}")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MAX_EVENT_BYTES = 4096
# Errors opening a pending entry that retrying cannot fix: a symlink, a directory, no read
# permission, or a socket or device node.
PERMANENT_ENTRY_ERRNOS = {errno.ELOOP, errno.EISDIR, errno.EACCES, errno.ENXIO}


class EventError(ValueError):
    """Invalid event or inbox. The message names a field or a condition, never input text."""


def _positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EventError(f"invalid {field}")
    return value


def _token(value: Any, field: str, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise EventError(f"invalid {field}")
    return value


def event_id(identity: dict[str, Any]) -> str:
    canonical = json.dumps({key: identity.get(key) for key in IDENTITY}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def validate(event: dict[str, Any]) -> dict[str, Any]:
    """Return the event if it matches the identifier-only contract, else raise EventError."""
    if not isinstance(event, dict) or set(event) != FIELDS:
        raise EventError("event fields differ from the contract")
    if event["schema"] != SCHEMA:
        raise EventError("invalid schema")
    if event["kind"] not in KINDS:
        raise EventError("invalid kind")
    if event["outcome"] not in OUTCOMES:
        raise EventError("invalid outcome")
    if not isinstance(event["repo"], str) or not REPO_RE.fullmatch(event["repo"]):
        raise EventError("invalid repo")
    issue = _positive_int(event["issue"], "issue")
    pr = _positive_int(event["pr"], "pr")
    head = _token(event["head"], "head", HEAD_RE)
    _token(event["run_id"], "run_id", TOKEN_RE)
    _token(event["source"], "source", TOKEN_RE)
    if issue is None and pr is None:
        raise EventError("issue or pr required")
    if pr is not None and head is None:
        raise EventError("head required with pr")
    if event["kind"] in {"review-run", "ready-flip", "dep-fix"} and pr is None:
        raise EventError("pr required for this kind")
    if event["kind"] == "review-run" and event["run_id"] is None:
        raise EventError("run_id required for review-run")
    if not isinstance(event["at"], str) or not TS_RE.fullmatch(event["at"]):
        raise EventError("invalid at")
    if event["id"] != event_id(event):
        raise EventError("id does not match identity")
    return event


def build_event(
    *,
    kind: str,
    repo: str,
    outcome: str,
    issue: int | None = None,
    pr: int | None = None,
    head: str | None = None,
    run_id: str | None = None,
    source: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": kind,
        "repo": repo,
        "issue": issue,
        "pr": pr,
        "head": head,
        "run_id": run_id,
        "outcome": outcome,
        "source": source,
        "at": at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    event["id"] = event_id(event)
    return validate(event)


def _private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise EventError("inbox path is a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise EventError("inbox path is not a directory")
    if info.st_uid != os.getuid():
        raise EventError("inbox is owned by another user")
    if info.st_mode & 0o077:
        raise EventError("inbox directory must be private")
    return path


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Inbox:
    def __init__(self, root: Path):
        self.root = _private_dir(root)
        self.pending_dir = _private_dir(root / "pending")
        self.acked_dir = _private_dir(root / "acked")
        self.tmp_dir = _private_dir(root / ".tmp")
        self.quarantine_dir = _private_dir(root / "quarantine")
        _fsync_dir(root)
        _fsync_dir(root.parent)

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _name(self, eid: str) -> str:
        if not ID_RE.fullmatch(eid):
            raise EventError("invalid id")
        return f"{eid}.json"

    def record(self, event: dict[str, Any]) -> str:
        """Persist the event, then expose it to waiters. Returns `recorded` or `duplicate`."""
        validate(event)
        name = self._name(event["id"])
        with self._lock():
            for directory in (self.acked_dir, self.pending_dir):
                if (directory / name).exists():
                    _fsync_dir(directory)
                    return "duplicate"
            fd, tmp = tempfile.mkstemp(dir=self.tmp_dir, prefix="event-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(event, handle, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.link(tmp, self.pending_dir / name)
                _fsync_dir(self.pending_dir)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
        return "recorded"

    def ack(self, eid: str) -> str:
        """Move one event to acked/. Returns `acknowledged` or `already-acknowledged`."""
        name = self._name(eid)
        with self._lock():
            pending = self.pending_dir / name
            acked = self.acked_dir / name
            replay = acked.exists()
            if not replay:
                if not pending.exists():
                    raise EventError("unknown event")
                os.link(pending, acked)
            _fsync_dir(self.acked_dir)
            pending.unlink(missing_ok=True)
            _fsync_dir(self.pending_dir)
        return "already-acknowledged" if replay else "acknowledged"

    def pending(self) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        invalid = 0
        paths = sorted(self.pending_dir.glob("*.json"))
        if paths:
            # A writer may have linked an entry and stopped before syncing it.
            _fsync_dir(self.pending_dir)
        for path in paths:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise EventError("pending entry must be a regular file")
                    data = handle.read(MAX_EVENT_BYTES + 1)
                    if len(data) > MAX_EVENT_BYTES:
                        raise EventError("pending entry too large")
                    event = validate(json.loads(data.decode("utf-8")))
                if path.name != f"{event['id']}.json":
                    raise EventError("name")
                events.append(event)
            except FileNotFoundError:
                continue  # A concurrent acknowledgement removed the entry.
            except OSError as error:
                if error.errno not in PERMANENT_ENTRY_ERRNOS:
                    raise
                self._quarantine(path)
                invalid += 1
            except (ValueError, TypeError, KeyError, RecursionError):
                invalid += 1
                self._quarantine(path)
        events.sort(key=lambda item: (item["at"], item["id"]))
        return {"events": events, "invalid": invalid}

    def _quarantine(self, path: Path) -> None:
        """Move a permanently unreadable pending entry aside, so it is reported once."""
        with self._lock():
            try:
                os.rename(path, self.quarantine_dir / f"{path.name}.{time.time_ns()}")
            except FileNotFoundError:
                return
            _fsync_dir(self.quarantine_dir)
            _fsync_dir(self.pending_dir)

    def wait(self, timeout: float, interval: float) -> dict[str, Any] | None:
        """Block until an event is pending (returned) or the timeout passes (None)."""
        deadline = time.monotonic() + timeout
        while True:
            found = self.pending()
            if found["events"] or found["invalid"]:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(interval, remaining))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable completion-event inbox for horch.")
    parser.add_argument("--inbox", required=True, type=Path, help="absolute inbox directory under the horch state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="persist one completion event")
    record.add_argument("--kind", required=True, choices=sorted(KINDS))
    record.add_argument("--repo", required=True)
    record.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    record.add_argument("--issue", type=int)
    record.add_argument("--pr", type=int)
    record.add_argument("--head")
    record.add_argument("--run-id")
    record.add_argument("--source")
    commands.add_parser("pending", help="list unacknowledged events")
    ack = commands.add_parser("ack", help="acknowledge one event")
    ack.add_argument("--id", required=True)
    wait = commands.add_parser("wait", help="block until an event is pending or the watchdog interval passes")
    wait.add_argument("--timeout", type=float, default=WATCHDOG_SECONDS)
    wait.add_argument("--interval", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.inbox.is_absolute():
            raise EventError("inbox must be absolute")
        inbox = Inbox(args.inbox)
        if args.command == "record":
            event = build_event(
                kind=args.kind,
                repo=args.repo,
                outcome=args.outcome,
                issue=args.issue,
                pr=args.pr,
                head=args.head,
                run_id=args.run_id,
                source=args.source,
            )
            print(json.dumps({"id": event["id"], "status": inbox.record(event)}))
        elif args.command == "ack":
            print(json.dumps({"id": args.id, "status": inbox.ack(args.id)}))
        elif args.command == "pending":
            print(json.dumps(inbox.pending(), sort_keys=True))
        else:
            if not 0 < args.timeout <= WATCHDOG_SECONDS or not 0 < args.interval <= args.timeout:
                raise EventError(f"timeout must be in (0, {WATCHDOG_SECONDS}] and interval in (0, timeout]")
            found = inbox.wait(args.timeout, args.interval)
            if found is None:
                print(json.dumps({"status": "watchdog"}))
                return 124
            print(json.dumps(found, sort_keys=True))
        return 0
    except EventError as error:
        print(json.dumps({"status": "invalid", "reason": str(error)}))
        return 2
    except OSError as error:
        print(json.dumps({"status": "invalid", "reason": f"inbox error {error.errno}"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
