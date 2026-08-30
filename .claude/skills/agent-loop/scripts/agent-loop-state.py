#!/usr/bin/env python3
"""Atomically create, update, and validate private agent-loop run state."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, NoReturn


STATE_VERSION = 1
SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PHASES = {"draft-open", "reviewing", "converged", "finalizing", "finalized"}
BATCH_STATUSES = {"pending", "active", "finalized", "bailed"}


class StateError(RuntimeError):
    """An invalid or unsafe agent-loop state operation."""


def _fail(message: str) -> NoReturn:
    raise StateError(message)


def _read(path: Path) -> dict[str, Any]:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        _fail("run state must be an owner-controlled regular file")
    if metadata.st_mode & 0o077:
        _fail("run state permissions must not grant group or other access")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateError("run state must contain valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        _fail("run state must be a JSON object")
    _validate(value)
    return value


def _validate(value: dict[str, Any]) -> None:
    required = {
        "version",
        "runId",
        "repo",
        "issue",
        "issueTitleSha256",
        "issueBodySha256",
        "baseBranch",
        "branch",
        "worktree",
        "logDir",
        "prNumber",
        "prUrl",
        "baseSha",
        "headSha",
        "phase",
        "round",
        "reviewEngine",
        "codexResultSha256",
        "claudeResultSha256",
    }
    budget = {"reviewDeadlineEpoch", "reviewMaxRounds"}
    if set(value) not in (required, required | budget):
        _fail("run state has missing or unknown fields")
    if type(value["version"]) is not int or value["version"] != STATE_VERSION:
        _fail("unsupported run state version")
    for key in ("runId", "repo", "baseBranch", "branch", "worktree", "logDir", "prUrl"):
        if not isinstance(value[key], str) or not value[key]:
            _fail(f"run state {key} must be a non-empty string")
    for key in ("issue", "prNumber", "round"):
        if type(value[key]) is not int or value[key] < 1:
            _fail(f"run state {key} must be a positive integer")
    if "reviewDeadlineEpoch" in value:
        if type(value["reviewDeadlineEpoch"]) is not int or value["reviewDeadlineEpoch"] < 1:
            _fail("run state reviewDeadlineEpoch must be a positive integer")
        if type(value["reviewMaxRounds"]) is not int or not 1 <= value["reviewMaxRounds"] <= 4:
            _fail("run state reviewMaxRounds must be between 1 and 4")
    for key in ("baseSha", "headSha"):
        if not isinstance(value[key], str) or not SHA_RE.fullmatch(value[key]):
            _fail(f"run state {key} must be a full lowercase commit SHA")
    if value["phase"] not in PHASES:
        _fail("run state phase is invalid")
    review_engine = value["reviewEngine"]
    if value["phase"] == "reviewing":
        if review_engine not in {"codex", "claude"}:
            _fail("reviewing run state requires a current review engine")
    elif review_engine is not None:
        _fail("only reviewing run state may name a current review engine")
    for key in ("codexResultSha256", "claudeResultSha256"):
        digest = value[key]
        if digest is not None and (
            not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
        ):
            _fail(f"run state {key} must be null or a lowercase SHA-256 digest")
    for key in ("issueTitleSha256", "issueBodySha256"):
        if not isinstance(value[key], str) or not SHA256_RE.fullmatch(value[key]):
            _fail(f"run state {key} must be a lowercase SHA-256 digest")
    if value["phase"] in {"converged", "finalizing", "finalized"} and any(
        value[key] is None
        for key in ("codexResultSha256", "claudeResultSha256")
    ):
        _fail("converged, finalizing, or finalized run state requires both review result hashes")
    worktree = Path(value["worktree"])
    log_dir = Path(value["logDir"])
    if not worktree.is_absolute() or not log_dir.is_absolute():
        _fail("run state paths must be absolute")


def _atomic_write(
    path: Path, value: dict[str, Any], *, replace: bool = True
) -> None:
    _validate(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        _fail("run state directory must not be a symlink")
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                _fail("run state already exists")
            os.unlink(temporary)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _create(args: argparse.Namespace) -> None:
    path = Path(args.file)
    value = {
        "version": STATE_VERSION,
        "runId": args.run_id,
        "repo": args.repo,
        "issue": args.issue,
        "issueTitleSha256": args.issue_title_sha256,
        "issueBodySha256": args.issue_body_sha256,
        "baseBranch": args.base_branch,
        "branch": args.branch,
        "worktree": str(Path(args.worktree).resolve()),
        "logDir": str(Path(args.log_dir).resolve()),
        "prNumber": args.pr,
        "prUrl": args.pr_url,
        "baseSha": args.base_sha,
        "headSha": args.head_sha,
        "phase": "draft-open",
        "round": 1,
        "reviewEngine": None,
        "codexResultSha256": None,
        "claudeResultSha256": None,
    }
    if args.review_deadline_epoch is not None and args.review_max_rounds is not None:
        value["reviewDeadlineEpoch"] = args.review_deadline_epoch
        value["reviewMaxRounds"] = args.review_max_rounds
    elif args.review_deadline_epoch is not None or args.review_max_rounds is not None:
        _fail("review deadline and maximum rounds must be provided together")
    _atomic_write(path, value, replace=False)
    print(json.dumps(value, sort_keys=True))


def _update(args: argparse.Namespace) -> None:
    path = Path(args.file)
    value = _read(path)
    value["phase"] = args.phase
    value["reviewEngine"] = args.review_engine if args.phase == "reviewing" else None
    if args.round is not None:
        value["round"] = args.round
    if args.base_sha is not None:
        value["baseSha"] = args.base_sha
    if args.head_sha is not None:
        value["headSha"] = args.head_sha
    if args.phase in {"draft-open", "reviewing"}:
        value["codexResultSha256"] = None
        value["claudeResultSha256"] = None
    elif args.phase == "converged":
        if args.codex_result_sha256 is None or args.claude_result_sha256 is None:
            _fail("converged state requires both review result hashes")
        value["codexResultSha256"] = args.codex_result_sha256
        value["claudeResultSha256"] = args.claude_result_sha256
    _atomic_write(path, value)
    print(json.dumps(value, sort_keys=True))


def _show(args: argparse.Namespace) -> None:
    print(json.dumps(_read(Path(args.file)), sort_keys=True))


def _validate_batch(value: dict[str, Any]) -> None:
    required = {"version", "kind", "runId", "repo", "baseBranch", "allowlist", "cursor", "issues"}
    if set(value) != required or value.get("version") != STATE_VERSION or value.get("kind") != "batch":
        _fail("batch state has missing, unknown, or unsupported fields")
    for key in ("runId", "repo", "baseBranch"):
        if not isinstance(value[key], str) or not value[key]:
            _fail(f"batch state {key} must be a non-empty string")
    allowlist = value["allowlist"]
    if (
        not isinstance(allowlist, list)
        or not allowlist
        or any(type(issue) is not int or issue < 1 for issue in allowlist)
        or len(set(allowlist)) != len(allowlist)
    ):
        _fail("batch allowlist must contain unique positive issue numbers")
    cursor = value["cursor"]
    if type(cursor) is not int or not 0 <= cursor <= len(allowlist):
        _fail("batch cursor is invalid")
    rows = value["issues"]
    if not isinstance(rows, list) or len(rows) != len(allowlist):
        _fail("batch issue statuses do not match the allowlist")
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"issue", "status", "childRunState"}:
            _fail("batch issue status has an invalid shape")
        if row["issue"] != allowlist[index] or row["status"] not in BATCH_STATUSES:
            _fail("batch issue status does not match the ordered allowlist")
        child = row["childRunState"]
        if child is not None and (not isinstance(child, str) or not Path(child).is_absolute()):
            _fail("batch child run-state path must be absolute or null")
        if index < cursor and row["status"] not in {"finalized", "bailed"}:
            _fail("completed batch entries must be finalized or bailed")
        if index > cursor and row["status"] != "pending":
            _fail("future batch entries must remain pending")
    if cursor < len(rows) and rows[cursor]["status"] not in {"pending", "active"}:
        _fail("current batch entry must be pending or active")


def _read_batch(path: Path) -> dict[str, Any]:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        _fail("batch state must be an owner-controlled private regular file")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateError("batch state must contain valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        _fail("batch state must be a JSON object")
    _validate_batch(value)
    return value


def _atomic_write_batch(path: Path, value: dict[str, Any], *, replace: bool = True) -> None:
    _validate_batch(value)
    # Reuse the same fsync/private atomic writer after temporarily validating as
    # batch state instead of child state.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        _fail("batch state directory must not be a symlink")
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                _fail("batch state already exists")
            os.unlink(temporary)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _validate_batch_lock(path: Path, descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    path_metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or (metadata.st_dev, metadata.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
    ):
        _fail("batch lock must be an owner-controlled private regular file")


@contextmanager
def _batch_lock(path: Path) -> Iterator[None]:
    """Serialize batch reads and compare-and-set updates on a stable inode."""
    lock_path = Path(f"{path}.lock")
    inherited = os.environ.get("AGENT_LOOP_BATCH_LOCK_FD")
    if inherited is not None:
        try:
            descriptor = int(inherited)
        except ValueError as error:
            raise StateError("inherited batch lock descriptor is invalid") from error
        _validate_batch_lock(lock_path, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
        return

    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_path.parent.is_symlink():
        _fail("batch lock directory must not be a symlink")
    os.chmod(lock_path.parent, 0o700)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        _validate_batch_lock(lock_path, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _batch_create(args: argparse.Namespace) -> None:
    allowlist = [int(value) for value in args.issues.split(",")]
    value = {
        "version": STATE_VERSION,
        "kind": "batch",
        "runId": args.run_id,
        "repo": args.repo,
        "baseBranch": args.base_branch,
        "allowlist": allowlist,
        "cursor": 0,
        "issues": [
            {"issue": issue, "status": "pending", "childRunState": None}
            for issue in allowlist
        ],
    }
    path = Path(args.file)
    with _batch_lock(path):
        _atomic_write_batch(path, value, replace=False)
    print(json.dumps(value, sort_keys=True))


def _batch_update(args: argparse.Namespace) -> None:
    path = Path(args.file)
    with _batch_lock(path):
        value = _read_batch(path)
        cursor = value["cursor"]
        if cursor >= len(value["issues"]) or value["issues"][cursor]["issue"] != args.issue:
            _fail("batch update may target only the current cursor issue")
        row = value["issues"][cursor]
        if row["status"] != args.expected_status:
            _fail(
                "batch update status changed: "
                f"expected {args.expected_status}, found {row['status']}"
            )
        allowed_transitions = {
            "pending": {"active", "bailed"},
            "active": {"active", "finalized", "bailed"},
        }
        if args.status not in allowed_transitions.get(args.expected_status, set()):
            _fail("batch issue has an invalid status transition")
        row["status"] = args.status
        if args.child_run_state is not None:
            row["childRunState"] = str(Path(args.child_run_state).resolve())
        if args.status in {"finalized", "bailed"}:
            if args.status == "finalized" and row["childRunState"] is None:
                _fail("finalized batch issue requires a child run-state path")
            value["cursor"] = cursor + 1
        _atomic_write_batch(path, value)
    print(json.dumps(value, sort_keys=True))


def _batch_show(args: argparse.Namespace) -> None:
    path = Path(args.file)
    with _batch_lock(path):
        value = _read_batch(path)
    print(json.dumps(value, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-version", action="version", version=str(STATE_VERSION))
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--file", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--repo", required=True)
    create.add_argument("--issue", required=True, type=int)
    create.add_argument("--issue-title-sha256", required=True)
    create.add_argument("--issue-body-sha256", required=True)
    create.add_argument("--base-branch", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--worktree", required=True)
    create.add_argument("--log-dir", required=True)
    create.add_argument("--pr", required=True, type=int)
    create.add_argument("--pr-url", required=True)
    create.add_argument("--base-sha", required=True)
    create.add_argument("--head-sha", required=True)
    create.add_argument("--review-deadline-epoch", type=int)
    create.add_argument("--review-max-rounds", type=int)
    create.set_defaults(handler=_create)
    update = commands.add_parser("update")
    update.add_argument("--file", required=True)
    update.add_argument("--phase", required=True, choices=sorted(PHASES))
    update.add_argument("--round", type=int)
    update.add_argument("--review-engine", choices=("codex", "claude"))
    update.add_argument("--base-sha")
    update.add_argument("--head-sha")
    update.add_argument("--codex-result-sha256")
    update.add_argument("--claude-result-sha256")
    update.set_defaults(handler=_update)
    show = commands.add_parser("show")
    show.add_argument("--file", required=True)
    show.set_defaults(handler=_show)
    batch_create = commands.add_parser("batch-create")
    batch_create.add_argument("--file", required=True)
    batch_create.add_argument("--run-id", required=True)
    batch_create.add_argument("--repo", required=True)
    batch_create.add_argument("--base-branch", required=True)
    batch_create.add_argument("--issues", required=True)
    batch_create.set_defaults(handler=_batch_create)
    batch_update = commands.add_parser("batch-update")
    batch_update.add_argument("--file", required=True)
    batch_update.add_argument("--issue", required=True, type=int)
    batch_update.add_argument(
        "--expected-status", required=True, choices=sorted(BATCH_STATUSES)
    )
    batch_update.add_argument("--status", required=True, choices=sorted(BATCH_STATUSES))
    batch_update.add_argument("--child-run-state")
    batch_update.set_defaults(handler=_batch_update)
    batch_show = commands.add_parser("batch-show")
    batch_show.add_argument("--file", required=True)
    batch_show.set_defaults(handler=_batch_show)
    return parser


def main() -> int:
    args = _parser().parse_args()
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, StateError) as error:
        print(f"agent-loop-state: {error}", file=sys.stderr)
        raise SystemExit(1) from error
