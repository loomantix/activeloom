#!/usr/bin/env python3
"""Atomically create, update, and validate private agent-loop run state.

A run state also carries the run's pinned reviewer and worker settings in the
review-settings pin-file format, so a resumed run launches the same models.
"""

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


STATE_VERSION = 2
# The batch schema is versioned independently of the run state above. Only the
# run-state schema gained `reviewSettings`, so bumping STATE_VERSION for it must
# not invalidate batch checkpoints for a schema that did not change. This number
# is the value this root's batch schema has carried from the start; move it only
# when the batch schema itself changes.
BATCH_STATE_VERSION = 1
SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PHASES = {"draft-open", "reviewing", "converged", "finalizing", "finalized"}
BATCH_STATUSES = {"pending", "active", "finalized", "bailed", "parked"}
TERMINAL_BATCH_STATUSES = frozenset({"finalized", "bailed", "parked"})
BATCH_ROW_REQUIRED = {"issue", "status", "childRunState"}
BATCH_ROW_OPTIONAL = {"classification", "stopCategory", "stackedOn"}
CLASSIFICATION_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
STOP_CATEGORY_RE = re.compile(r"[a-z][a-z0-9-]{0,31}(?:/[a-z][a-z0-9-]{0,31})?")
SETTINGS_ENGINES = ("claude", "codex", "gemini")
# review-settings.py pin-file keys: the pins of each role and the engines that
# role has switched to its fallback.
SETTINGS_ROLES = (
    ("review_settings", "fallback_engines"),
    ("worker_settings", "worker_fallback_engines"),
)
SETTINGS_KEYS = {"version", "repo", *(key for role in SETTINGS_ROLES for key in role)}
CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."


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
    fields = set(value) - {"reviewSettings"}
    if fields not in (required, required | budget):
        _fail("run state has missing or unknown fields")
    if "reviewSettings" in value:
        _validate_settings(value["reviewSettings"])
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


def _valid_pair(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(key), str) and value[key] for key in ("model", "effort")
    )


def _validate_settings(value: Any) -> None:
    """The pin-file shape; review-settings.py validates the values it launches."""
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or set(value) - SETTINGS_KEYS
        or not (value.get("repo") is None or isinstance(value["repo"], str))
    ):
        _fail("run state reviewSettings has an unsupported format")
    for pins_key, switched_key in SETTINGS_ROLES:
        pins = value.get(pins_key, {})
        switched = value.get(switched_key, [])
        if (
            not isinstance(pins, dict)
            or not isinstance(switched, list)
            or any(engine not in SETTINGS_ENGINES for engine in pins)
            or not all(_valid_pair(settings) for settings in pins.values())
            or any(engine not in pins for engine in switched)
            or len(set(switched)) != len(switched)
        ):
            _fail("run state reviewSettings has an unsupported format")
        for engine in switched:
            if not _valid_pair(pins[engine].get("fallback")):
                _fail("run state reviewSettings switched to a missing fallback")


def _settings_extend(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Whether `new` keeps every pin and switch of `old` and only adds to them."""
    if old.get("repo") != new.get("repo"):
        return False
    for pins_key, switched_key in SETTINGS_ROLES:
        old_pins, new_pins = old.get(pins_key, {}), new.get(pins_key, {})
        if any(new_pins.get(engine) != pins for engine, pins in old_pins.items()):
            return False
        if not set(old.get(switched_key, [])) <= set(new.get(switched_key, [])):
            return False
    return True


def _read_pin_file(path: Path) -> dict[str, Any]:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        _fail("pin file must be an owner-controlled regular file")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateError("pin file must contain valid UTF-8 JSON") from error
    _validate_settings(value)
    return value


def _write_pin_file(path: Path, value: dict[str, Any]) -> None:
    if path.parent.is_symlink():
        _fail("pin file directory must not be a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


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
    if args.review_settings_file is not None:
        value["reviewSettings"] = _read_pin_file(Path(args.review_settings_file))
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


def _settings_save(args: argparse.Namespace) -> None:
    """Record the pin file in the run state; pins and switches only grow."""
    path = Path(args.file)
    value = _read(path)
    settings = _read_pin_file(Path(args.pin_file))
    if "reviewSettings" in value and not _settings_extend(value["reviewSettings"], settings):
        _fail("pin file changes settings this run already pinned")
    value["reviewSettings"] = settings
    _atomic_write(path, value)


def _settings_restore(args: argparse.Namespace) -> None:
    """Write the run state's pins to the pin file a resumed run launches from.

    A pin file that extends the recorded pins holds a fallback switch made just
    before an interruption, so it is kept and recorded instead.
    """
    path = Path(args.file)
    value = _read(path)
    recorded = value.get("reviewSettings")
    if recorded is None:
        return
    pin_file = Path(args.pin_file)
    if pin_file.exists() or pin_file.is_symlink():
        try:
            current = _read_pin_file(pin_file)
        except StateError:
            current = None
        if current is not None and _settings_extend(recorded, current):
            if current != recorded:
                value["reviewSettings"] = current
                _atomic_write(path, value)
            return
    _write_pin_file(pin_file, recorded)


def _capacity_rejected(args: argparse.Namespace) -> None:
    """Exit 0 when the log's last terminal JSON event is a model-capacity rejection.

    The same recognizer as the review-chain runner: only Codex JSON events count,
    never command output or plain diagnostics.
    """
    path = Path(args.log)
    rejected = False
    if not path.is_symlink() and path.is_file():
        with path.open(errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind in ("error", "turn.failed"):
                    error = event.get("error") if kind == "turn.failed" else event
                    rejected = isinstance(error, dict) and error.get("message") == CAPACITY_MESSAGE
                else:
                    rejected = False
    if not rejected:
        raise SystemExit(1)


def _validate_batch(value: dict[str, Any]) -> None:
    required = {"version", "kind", "runId", "repo", "baseBranch", "allowlist", "cursor", "issues"}
    if set(value) != required or value.get("kind") != "batch":
        _fail("batch state has missing or unknown fields")
    if type(value["version"]) is not int or value["version"] != BATCH_STATE_VERSION:
        _fail("unsupported batch state version")
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
        if (
            not isinstance(row, dict)
            or not BATCH_ROW_REQUIRED <= set(row)
            or set(row) - BATCH_ROW_REQUIRED - BATCH_ROW_OPTIONAL
        ):
            _fail("batch issue status has an invalid shape")
        if row["issue"] != allowlist[index] or row["status"] not in BATCH_STATUSES:
            _fail("batch issue status does not match the ordered allowlist")
        if "classification" in row and (
            row["status"] != "bailed"
            or not isinstance(row["classification"], str)
            or not CLASSIFICATION_RE.fullmatch(row["classification"])
        ):
            _fail("only a bailed batch issue may carry a bail classification")
        if row["status"] == "parked":
            category = row.get("stopCategory")
            if not isinstance(category, str) or not STOP_CATEGORY_RE.fullmatch(category):
                _fail("a parked batch issue requires a stop category")
        elif "stopCategory" in row:
            _fail("only a parked batch issue may carry a stop category")
        if "stackedOn" in row:
            parent = row["stackedOn"]
            parent_row = rows[allowlist.index(parent)] if parent in allowlist else None
            if (
                type(parent) is not int
                or parent not in allowlist[:index]
                or row["status"] == "pending"
                or parent_row is None
                or parent_row["status"] != "finalized"
                or parent_row["childRunState"] is None
            ):
                _fail(
                    "a stacked batch issue must name an earlier batch issue, have started, "
                    "and use a finalized parent with a child review checkpoint"
                )
        child = row["childRunState"]
        if child is not None and (not isinstance(child, str) or not Path(child).is_absolute()):
            _fail("batch child run-state path must be absolute or null")
        if index < cursor and row["status"] not in TERMINAL_BATCH_STATUSES:
            _fail("completed batch entries must be finalized, bailed, or parked")
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
        "version": BATCH_STATE_VERSION,
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
        index = next(
            (position for position, entry in enumerate(value["issues"]) if entry["issue"] == args.issue),
            None,
        )
        # A parked entry sits behind the cursor. It can still be closed out
        # once an operator resumes it, without moving the cursor.
        parked_entry = (
            index is not None and index < cursor and value["issues"][index]["status"] == "parked"
        )
        if index is None or (index != cursor and not parked_entry):
            _fail("batch update may target only the current cursor issue or a parked issue")
        row = value["issues"][index]
        if row["status"] != args.expected_status:
            _fail(
                "batch update status changed: "
                f"expected {args.expected_status}, found {row['status']}"
            )
        allowed_transitions = {
            "pending": {"active", "bailed", "parked"},
            "active": {"active", "finalized", "bailed", "parked"},
            "parked": {"finalized", "bailed"},
        }
        if args.status not in allowed_transitions.get(args.expected_status, set()):
            _fail("batch issue has an invalid status transition")
        row["status"] = args.status
        if args.status != "parked":
            row.pop("stopCategory", None)
        if args.stop_category is not None:
            if args.status != "parked":
                _fail("a stop category applies only to a parked batch issue")
            row["stopCategory"] = args.stop_category
        if args.stacked_on is not None:
            row["stackedOn"] = args.stacked_on
        if args.classification is not None:
            if args.status != "bailed":
                _fail("a bail classification applies only to a bailed batch issue")
            row["classification"] = args.classification
        if args.child_run_state is not None:
            row["childRunState"] = str(Path(args.child_run_state).resolve())
        if args.status in TERMINAL_BATCH_STATUSES:
            if args.status == "finalized" and row["childRunState"] is None:
                _fail("finalized batch issue requires a child run-state path")
            if not parked_entry:
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
    parser.add_argument(
        "--batch-state-version", action="version", version=str(BATCH_STATE_VERSION)
    )
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
    create.add_argument("--review-settings-file")
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
    for name, handler in (("settings-save", _settings_save), ("settings-restore", _settings_restore)):
        settings = commands.add_parser(name)
        settings.add_argument("--file", required=True)
        settings.add_argument("--pin-file", required=True)
        settings.set_defaults(handler=handler)
    capacity = commands.add_parser("capacity-rejected")
    capacity.add_argument("--log", required=True)
    capacity.set_defaults(handler=_capacity_rejected)
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
    batch_update.add_argument("--classification")
    batch_update.add_argument("--stop-category")
    batch_update.add_argument("--stacked-on", type=int)
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
