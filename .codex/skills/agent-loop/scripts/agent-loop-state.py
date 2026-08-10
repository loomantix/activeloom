#!/usr/bin/env python3
"""Atomically create, update, and validate private agent-loop run state."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn


STATE_VERSION = 1
SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PHASES = {"draft-open", "reviewing", "converged", "finalized"}


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
        "codexResultSha256",
        "claudeResultSha256",
    }
    if set(value) != required:
        _fail("run state has missing or unknown fields")
    if value["version"] != STATE_VERSION:
        _fail("unsupported run state version")
    for key in ("runId", "repo", "baseBranch", "branch", "worktree", "logDir", "prUrl"):
        if not isinstance(value[key], str) or not value[key]:
            _fail(f"run state {key} must be a non-empty string")
    for key in ("issue", "prNumber", "round"):
        if not isinstance(value[key], int) or value[key] < 1:
            _fail(f"run state {key} must be a positive integer")
    for key in ("baseSha", "headSha"):
        if not isinstance(value[key], str) or not SHA_RE.fullmatch(value[key]):
            _fail(f"run state {key} must be a full lowercase commit SHA")
    if value["phase"] not in PHASES:
        _fail("run state phase is invalid")
    for key in ("codexResultSha256", "claudeResultSha256"):
        digest = value[key]
        if digest is not None and (
            not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
        ):
            _fail(f"run state {key} must be null or a lowercase SHA-256 digest")
    if value["phase"] in {"converged", "finalized"} and any(
        value[key] is None
        for key in ("codexResultSha256", "claudeResultSha256")
    ):
        _fail("converged or finalized run state requires both review result hashes")
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
        "codexResultSha256": None,
        "claudeResultSha256": None,
    }
    _atomic_write(path, value, replace=False)
    print(json.dumps(value, sort_keys=True))


def _update(args: argparse.Namespace) -> None:
    path = Path(args.file)
    value = _read(path)
    value["phase"] = args.phase
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-version", action="version", version=str(STATE_VERSION))
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--file", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--repo", required=True)
    create.add_argument("--issue", required=True, type=int)
    create.add_argument("--base-branch", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--worktree", required=True)
    create.add_argument("--log-dir", required=True)
    create.add_argument("--pr", required=True, type=int)
    create.add_argument("--pr-url", required=True)
    create.add_argument("--base-sha", required=True)
    create.add_argument("--head-sha", required=True)
    create.set_defaults(handler=_create)
    update = commands.add_parser("update")
    update.add_argument("--file", required=True)
    update.add_argument("--phase", required=True, choices=sorted(PHASES))
    update.add_argument("--round", type=int)
    update.add_argument("--base-sha")
    update.add_argument("--head-sha")
    update.add_argument("--codex-result-sha256")
    update.add_argument("--claude-result-sha256")
    update.set_defaults(handler=_update)
    show = commands.add_parser("show")
    show.add_argument("--file", required=True)
    show.set_defaults(handler=_show)
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
