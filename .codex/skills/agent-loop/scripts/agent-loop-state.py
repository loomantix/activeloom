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
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, NoReturn


STATE_VERSION = 4
LEGACY_STATE_VERSION = 2
# The batch schema is versioned independently of the run state above. Only the
# run-state schema gained `reviewSettings`, so bumping STATE_VERSION for it must
# not invalidate batch checkpoints for a schema that did not change. This number
# is the value every helper on disk writes today -- here and in consumers, whose
# sync tag predates that bump. It is NOT necessarily the batch schema's original
# number: a run-state-only bump moved the shared constant once before, so a root
# may have written a lower value earlier in its history. Move this only when the
# batch schema itself changes.
BATCH_STATE_VERSION = 1
SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PREPUBLICATION_PHASES = {
    "worker-running",
    "worker-complete",
    "integrating",
    "integrated",
    "publishing",
    "pushed",
}
PHASES = PREPUBLICATION_PHASES | {
    "draft-open",
    "reviewing",
    "converged",
    "finalizing",
    "finalized",
}
BATCH_STATUSES = {"pending", "active", "finalized", "bailed"}
SETTINGS_ENGINES = ("claude", "codex", "gemini")
# review-settings.py pin-file keys: the pins of each role and the engines that
# role has switched to its fallback.
SETTINGS_ROLES = (
    ("review_settings", "fallback_engines"),
    ("worker_settings", "worker_fallback_engines"),
)
SETTINGS_KEYS = {"version", "repo", *(key for role in SETTINGS_ROLES for key in role)}


class StateError(RuntimeError):
    """An invalid or unsafe agent-loop state operation."""


def _fail(message: str) -> NoReturn:
    raise StateError(message)


def _read_state(
    path: Path,
    *,
    label: str,
    validator: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        _fail(f"{label} must be an owner-controlled regular file")
    if metadata.st_mode & 0o077:
        _fail(f"{label} permissions must not grant group or other access")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateError(f"{label} must contain valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    validator(value)
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
    legacy_extended = required | {"gitConfigSha256"}
    extended = legacy_extended | {"projectDir", "projectGitConfigSha256"}
    budget = {"reviewDeadlineEpoch", "reviewMaxRounds"}
    if value.get("version") == STATE_VERSION:
        budget = {"reviewBudget", "reviewMaxRounds"}
        required |= budget
    if set(value) - {"reviewSettings"} not in {
        frozenset(required),
        frozenset(legacy_extended),
        frozenset(extended),
        frozenset(required | budget),
        frozenset(legacy_extended | budget),
        frozenset(extended | budget),
    }:
        _fail("run state has missing or unknown fields")
    if "reviewSettings" in value:
        _validate_settings(value["reviewSettings"])
    if type(value["version"]) is not int or value["version"] not in {
        LEGACY_STATE_VERSION,
        STATE_VERSION,
    }:
        _fail("unsupported run state version")
    _budget_validate(value)
    for key in ("runId", "repo", "baseBranch", "branch", "worktree", "logDir"):
        if not isinstance(value[key], str) or not value[key]:
            _fail(f"run state {key} must be a non-empty string")
    for key in ("issue", "round"):
        if type(value[key]) is not int or value[key] < 1:
            _fail(f"run state {key} must be a positive integer")
    if "reviewDeadlineEpoch" in value:
        if (
            type(value["reviewDeadlineEpoch"]) is not int
            or value["reviewDeadlineEpoch"] < 1
        ):
            _fail("run state reviewDeadlineEpoch must be a positive integer")
        if (
            type(value["reviewMaxRounds"]) is not int
            or not 1 <= value["reviewMaxRounds"] <= 4
        ):
            _fail("run state reviewMaxRounds must be between 1 and 4")
    for key in ("baseSha", "headSha"):
        if not isinstance(value[key], str) or not SHA_RE.fullmatch(value[key]):
            _fail(f"run state {key} must be a full lowercase commit SHA")
    if value["phase"] not in PHASES:
        _fail("run state phase is invalid")
    if value["phase"] in PREPUBLICATION_PHASES:
        if (
            value["version"] != STATE_VERSION
            or value["prNumber"] is not None
            or value["prUrl"] is not None
        ):
            _fail("pre-publication state requires an active budget and no PR identity")
        if value["round"] != 1 or any(
            value[key] is not None
            for key in ("codexResultSha256", "claudeResultSha256")
        ):
            _fail("pre-publication state cannot contain review evidence")
    elif (
        type(value["prNumber"]) is not int
        or value["prNumber"] < 1
        or not isinstance(value["prUrl"], str)
        or not value["prUrl"]
    ):
        _fail("published state requires a positive PR number and URL")
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
    if "gitConfigSha256" in value and (
        not isinstance(value["gitConfigSha256"], str)
        or not SHA256_RE.fullmatch(value["gitConfigSha256"])
    ):
        _fail("run state gitConfigSha256 must be a lowercase SHA-256 digest")
    if "projectDir" in value:
        if not isinstance(value["projectDir"], str) or not value["projectDir"]:
            _fail("run state projectDir must be a non-empty string")
        if not Path(value["projectDir"]).is_absolute():
            _fail("run state projectDir must be absolute")
        if not isinstance(
            value["projectGitConfigSha256"], str
        ) or not SHA256_RE.fullmatch(value["projectGitConfigSha256"]):
            _fail("run state projectGitConfigSha256 must be a lowercase SHA-256 digest")
    if value["phase"] in {"converged", "finalizing", "finalized"} and any(
        value[key] is None for key in ("codexResultSha256", "claudeResultSha256")
    ):
        _fail(
            "converged, finalizing, or finalized run state requires both review result hashes"
        )
    worktree = Path(value["worktree"])
    log_dir = Path(value["logDir"])
    if not worktree.is_absolute() or not log_dir.is_absolute():
        _fail("run state paths must be absolute")


def _read(path: Path) -> dict[str, Any]:
    return _read_state(path, label="run state", validator=_validate)


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


def _atomic_write_state(
    path: Path,
    value: dict[str, Any],
    *,
    label: str,
    validator: Callable[[dict[str, Any]], None],
    replace: bool = True,
) -> None:
    validator(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        _fail(f"{label} directory must not be a symlink")
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
                _fail(f"{label} already exists")
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


def _atomic_write(path: Path, value: dict[str, Any], *, replace: bool = True) -> None:
    _atomic_write_state(
        path,
        value,
        label="run state",
        validator=_validate,
        replace=replace,
    )


def _create(args: argparse.Namespace) -> None:
    path = Path(args.file)
    value = {
        "version": LEGACY_STATE_VERSION,
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
        "phase": args.phase,
        "round": 1,
        "reviewEngine": None,
        "codexResultSha256": None,
        "claudeResultSha256": None,
    }
    if args.git_config_sha256 is not None:
        value["gitConfigSha256"] = args.git_config_sha256
    if args.project_dir is not None and args.project_git_config_sha256 is not None:
        value["projectDir"] = str(Path(args.project_dir).resolve())
        value["projectGitConfigSha256"] = args.project_git_config_sha256
    elif args.project_dir is not None or args.project_git_config_sha256 is not None:
        _fail("run project dir and project Git config digest must be provided together")
    if args.review_budget_seconds is not None:
        if args.review_deadline_epoch is not None or args.review_max_rounds is None:
            _fail("active budget requires a round cap and excludes the legacy deadline")
        value["version"] = STATE_VERSION
        value["reviewBudget"] = _budget_new(args.review_budget_seconds)
        value["reviewMaxRounds"] = args.review_max_rounds
    elif args.review_deadline_epoch is not None and args.review_max_rounds is not None:
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
    if (
        args.phase in PREPUBLICATION_PHASES
        and value["phase"] not in PREPUBLICATION_PHASES
    ):
        _fail("published state cannot return to pre-publication")
    if args.pr is not None or args.pr_url is not None:
        if (
            value["phase"] != "pushed"
            or args.phase != "draft-open"
            or args.pr is None
            or args.pr_url is None
        ):
            _fail("PR identity can only be attached after initial push")
        value["prNumber"] = args.pr
        value["prUrl"] = args.pr_url
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
    if "reviewSettings" in value and not _settings_extend(
        value["reviewSettings"], settings
    ):
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


def _validate_batch(value: dict[str, Any]) -> None:
    required = {
        "version",
        "kind",
        "runId",
        "repo",
        "baseBranch",
        "allowlist",
        "cursor",
        "issues",
    }
    extended = required | {"projectDir", "gitConfigSha256"}
    if set(value) not in {frozenset(required), frozenset(extended)}:
        _fail("batch state has missing or unknown fields")
    if value.get("kind") != "batch":
        _fail("batch state kind must be 'batch'")
    if type(value["version"]) is not int or value["version"] != BATCH_STATE_VERSION:
        _fail(
            "unsupported batch state version: found "
            f"{value['version']!r}, this harness writes {BATCH_STATE_VERSION}"
        )
    for key in ("runId", "repo", "baseBranch"):
        if not isinstance(value[key], str) or not value[key]:
            _fail(f"batch state {key} must be a non-empty string")
    if "projectDir" in value:
        if not isinstance(value["projectDir"], str) or not value["projectDir"]:
            _fail("batch state projectDir must be a non-empty string")
        if not Path(value["projectDir"]).is_absolute():
            _fail("batch state projectDir must be absolute")
        if not isinstance(value["gitConfigSha256"], str) or not SHA256_RE.fullmatch(
            value["gitConfigSha256"]
        ):
            _fail("batch state gitConfigSha256 must be a lowercase SHA-256 digest")
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
        if not isinstance(row, dict) or set(row) != {
            "issue",
            "status",
            "childRunState",
        }:
            _fail("batch issue status has an invalid shape")
        if row["issue"] != allowlist[index] or row["status"] not in BATCH_STATUSES:
            _fail("batch issue status does not match the ordered allowlist")
        child = row["childRunState"]
        if child is not None and (
            not isinstance(child, str) or not Path(child).is_absolute()
        ):
            _fail("batch child run-state path must be absolute or null")
        if index < cursor and row["status"] not in {"finalized", "bailed"}:
            _fail("completed batch entries must be finalized or bailed")
        if index > cursor and row["status"] != "pending":
            _fail("future batch entries must remain pending")
    if cursor < len(rows) and rows[cursor]["status"] not in {"pending", "active"}:
        _fail("current batch entry must be pending or active")


def _read_batch(path: Path) -> dict[str, Any]:
    return _read_state(path, label="batch state", validator=_validate_batch)


def _atomic_write_batch(
    path: Path, value: dict[str, Any], *, replace: bool = True
) -> None:
    _atomic_write_state(
        path,
        value,
        label="batch state",
        validator=_validate_batch,
        replace=replace,
    )


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
    if args.project_dir is not None and args.git_config_sha256 is not None:
        value["projectDir"] = str(Path(args.project_dir).resolve())
        value["gitConfigSha256"] = args.git_config_sha256
    elif args.project_dir is not None or args.git_config_sha256 is not None:
        _fail("batch project dir and Git config digest must be provided together")
    path = Path(args.file)
    with _batch_lock(path):
        _atomic_write_batch(path, value, replace=False)
    print(json.dumps(value, sort_keys=True))


def _batch_update(args: argparse.Namespace) -> None:
    path = Path(args.file)
    with _batch_lock(path):
        value = _read_batch(path)
        cursor = value["cursor"]
        if (
            cursor >= len(value["issues"])
            or value["issues"][cursor]["issue"] != args.issue
        ):
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


# agent-loop-budget:begin
# Shared budget protocol; rendered into the three state helpers.
def _budget_validate(value: dict[str, Any]) -> None:
    budget = value.get("reviewBudget")
    if value["version"] == LEGACY_STATE_VERSION:
        if budget is not None:
            _fail("legacy state cannot carry an active budget")
        return
    if not isinstance(budget, dict) or set(budget) != {"limit", "remaining", "attempts", "migration"}:
        _fail("invalid active review budget")
    limit, remaining = budget["limit"], budget["remaining"]
    if type(limit) is not int or limit < 1 or type(remaining) is not int or not 0 <= remaining <= limit:
        _fail("invalid remaining review budget")
    if "reviewDeadlineEpoch" in value or type(value.get("reviewMaxRounds")) is not int or not 1 <= value["reviewMaxRounds"] <= 4:
        _fail("active budget requires a round cap and no absolute deadline")
    attempts = budget["attempts"]
    if not isinstance(attempts, list):
        _fail("invalid budget attempt history")
    charged = 0
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict) or set(attempt) != {"id", "reserved", "charged", "startedNs", "boot", "owner", "status", "reason"}:
            _fail("invalid budget attempt")
        if attempt["id"] != index + 1 or attempt["status"] not in {"active", "settled", "abandoned"}:
            _fail("invalid budget attempt sequence")
        for key in ("reserved", "charged", "startedNs", "owner"):
            if type(attempt[key]) is not int or attempt[key] < 1:
                _fail("invalid budget attempt counter")
        if attempt["charged"] > attempt["reserved"] or not isinstance(attempt["boot"], str) or not attempt["boot"]:
            _fail("invalid budget attempt charge")
        if not isinstance(attempt["reason"], str):
            _fail("invalid budget attempt reason")
        if attempt["status"] != "settled" and attempt["charged"] != attempt["reserved"]:
            _fail("unfinished execution must retain its full reservation")
        charged += attempt["charged"]
    migration = budget["migration"]
    initial = limit
    if migration is not None:
        if not isinstance(migration, dict) or set(migration) != {"sha256", "remaining", "reason", "actor", "epoch", "deadline"}:
            _fail("invalid budget migration audit")
        initial = migration["remaining"]
        if type(initial) is not int or not 0 <= initial <= limit or not SHA256_RE.fullmatch(str(migration["sha256"])):
            _fail("invalid migrated budget")
        if not all(isinstance(migration[k], str) and migration[k].strip() for k in ("reason", "actor")):
            _fail("invalid migration operator evidence")
    if initial - charged != remaining:
        _fail("review budget accounting does not balance")


def _budget_new(seconds: int) -> dict[str, Any]:
    return {"limit": seconds, "remaining": seconds, "attempts": [], "migration": None}


def _budget_boot() -> str:
    # The wrappers require Linux flock/timeout. A boot identity prevents refund
    # across reboots or unrelated monotonic-clock origins.
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


@contextmanager
def _budget_lock(path: Path, inherited: int | None) -> Iterator[None]:
    metadata = os.lstat(path.parent)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        _fail("budget requires an owner-controlled private log directory")
    descriptor = inherited if inherited is not None else os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            _fail("budget lock does not name the run log directory")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("another process already owns this agent-loop run")
        yield
    finally:
        if inherited is None:
            os.close(descriptor)


def _budget_command(args: argparse.Namespace) -> None:
    import hashlib
    import time

    path = Path(args.file)
    with _budget_lock(path, args.lock_fd):
        value = _read(path)
        if str(path.parent.resolve()) != value["logDir"]:
            _fail("run state file is outside its recorded log directory")
        if args.command == "budget-migrate":
            if value["version"] != LEGACY_STATE_VERSION:
                _fail("only a legacy checkpoint can migrate; active budgets cannot be replenished")
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != args.expected_sha256:
                _fail("checkpoint changed since operator inspection")
            if not args.reason.strip() or not 1 <= args.limit_seconds <= 86400 or not 0 <= args.remaining_seconds <= args.limit_seconds:
                _fail("migration requires a reason and 0 <= remaining <= limit <= 86400 seconds")
            cap = value.get("reviewMaxRounds")
            if cap is None:
                cap = args.max_rounds
            elif args.max_rounds is not None and args.max_rounds != cap:
                _fail("migration cannot change the saved round cap")
            if type(cap) is not int or not 1 <= cap <= 4:
                _fail("legacy checkpoint without a round cap requires --max-rounds (1..4)")
            backup = path.with_name(path.name + ".legacy-" + digest + ".json")
            if backup.exists():
                if backup.is_symlink() or backup.read_bytes() != raw:
                    _fail("legacy backup differs")
            else:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            value["version"] = STATE_VERSION
            value["reviewMaxRounds"] = cap
            value["reviewBudget"] = _budget_new(args.limit_seconds)
            value["reviewBudget"]["remaining"] = args.remaining_seconds
            value["reviewBudget"]["migration"] = {
                "sha256": digest, "remaining": args.remaining_seconds,
                "reason": args.reason, "actor": f"uid:{os.getuid()}",
                "epoch": int(time.time()), "deadline": value.pop("reviewDeadlineEpoch", None),
            }
            _atomic_write(path, value)
            print(json.dumps(value["reviewBudget"], sort_keys=True))
            return
        if value["version"] != STATE_VERSION or "reviewBudget" not in value:
            _fail("legacy review budget requires explicit budget-migrate; no time is inferred from deadlines or mtimes")
        budget = value["reviewBudget"]
        pending = [a for a in budget["attempts"] if a["status"] == "active"]
        if args.command == "budget-reconcile":
            if hashlib.sha256(path.read_bytes()).hexdigest() != args.expected_sha256 or not args.reason.strip():
                _fail("reconciliation requires an unchanged checkpoint and an operator reason")
            for attempt in pending:
                if attempt["boot"] == _budget_boot() and Path(f"/proc/{attempt['owner']}").exists():
                    _fail("recorded controller still exists; stop it before reconciliation")
                attempt["status"] = "abandoned"
                attempt["reason"] = f"uid:{os.getuid()} epoch:{int(time.time())} {args.reason}"
            _atomic_write(path, value)
        elif args.command == "budget-finish":
            if len(pending) != 1 or pending[0]["id"] != args.attempt:
                _fail("budget completion does not match the active reservation")
            attempt = pending[0]
            now = time.monotonic_ns()
            if attempt["boot"] != _budget_boot() or now < attempt["startedNs"] or attempt["owner"] != args.owner:
                _fail("budget clock or execution owner changed; full reservation retained")
            elapsed = max(1, (now - attempt["startedNs"] + 999999999) // 1000000000)
            charged = min(attempt["reserved"], elapsed)
            budget["remaining"] += attempt["reserved"] - charged
            attempt.update(status="settled", charged=charged)
            _atomic_write(path, value)
        else:
            if pending:
                _fail("unfinished budget reservation: confirm workers stopped, then budget-reconcile; no refund is available")
            if args.command == "budget-begin":
                if type(args.seconds) is not int or args.seconds < 1 or args.seconds > budget["remaining"]:
                    _fail("review execution budget exhausted")
                attempt = {"id": len(budget["attempts"]) + 1, "reserved": args.seconds,
                           "charged": args.seconds, "startedNs": time.monotonic_ns(),
                           "boot": _budget_boot(), "owner": args.owner, "status": "active", "reason": ""}
                budget["remaining"] -= args.seconds
                budget["attempts"].append(attempt)
                _atomic_write(path, value)
                print(attempt["id"])
                return
        print(budget["remaining"])


def _budget_parser(commands: Any) -> None:
    for name in ("budget-show", "budget-begin", "budget-finish", "budget-migrate", "budget-reconcile"):
        command = commands.add_parser(name)
        command.add_argument("--file", required=True)
        command.add_argument("--lock-fd", type=int)
        if name in {"budget-migrate", "budget-reconcile"}:
            command.add_argument("--expected-sha256", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--confirm-stopped", action="store_true", required=True)
        if name == "budget-migrate":
            command.add_argument("--remaining-seconds", type=int, required=True)
            command.add_argument("--limit-seconds", type=int, required=True)
            command.add_argument("--max-rounds", type=int)
        if name in {"budget-begin", "budget-finish"}:
            command.add_argument("--owner", type=int, required=True)
        if name == "budget-begin":
            command.add_argument("--seconds", type=int, required=True)
        if name == "budget-finish":
            command.add_argument("--attempt", type=int, required=True)
        command.set_defaults(handler=_budget_command)
# agent-loop-budget:end


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-version", action="version", version=str(STATE_VERSION))
    parser.add_argument(
        "--batch-state-version", action="version", version=str(BATCH_STATE_VERSION)
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _budget_parser(commands)
    create = commands.add_parser("create")
    create.add_argument("--file", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--repo", required=True)
    create.add_argument("--issue", required=True, type=int)
    create.add_argument("--issue-title-sha256", required=True)
    create.add_argument("--issue-body-sha256", required=True)
    create.add_argument("--git-config-sha256")
    create.add_argument("--project-dir")
    create.add_argument("--project-git-config-sha256")
    create.add_argument("--base-branch", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--worktree", required=True)
    create.add_argument("--log-dir", required=True)
    create.add_argument(
        "--phase", choices=("draft-open", "worker-running"), default="draft-open"
    )
    create.add_argument("--pr", type=int)
    create.add_argument("--pr-url")
    create.add_argument("--base-sha", required=True)
    create.add_argument("--head-sha", required=True)
    create.add_argument("--review-budget-seconds", type=int)
    create.add_argument("--review-deadline-epoch", type=int)
    create.add_argument("--review-max-rounds", type=int)
    create.add_argument("--review-settings-file")
    create.set_defaults(handler=_create)
    update = commands.add_parser("update")
    update.add_argument("--file", required=True)
    update.add_argument("--phase", required=True, choices=sorted(PHASES))
    update.add_argument("--pr", type=int)
    update.add_argument("--pr-url")
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
    for name, handler in (
        ("settings-save", _settings_save),
        ("settings-restore", _settings_restore),
    ):
        settings = commands.add_parser(name)
        settings.add_argument("--file", required=True)
        settings.add_argument("--pin-file", required=True)
        settings.set_defaults(handler=handler)
    batch_create = commands.add_parser("batch-create")
    batch_create.add_argument("--file", required=True)
    batch_create.add_argument("--run-id", required=True)
    batch_create.add_argument("--repo", required=True)
    batch_create.add_argument("--base-branch", required=True)
    batch_create.add_argument("--project-dir")
    batch_create.add_argument("--git-config-sha256")
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
