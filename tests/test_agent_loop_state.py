"""Tests for private, atomic agent-loop run-state checkpoints."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / ".codex/skills/agent-loop/scripts/agent-loop-state.py"
HEAD = "a" * 40
BASE = "b" * 40
TITLE_HASH = "c" * 64
BODY_HASH = "d" * 64


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(HELPER), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_state_create_and_update_are_private_and_validated(tmp_path: Path) -> None:
    state = tmp_path / "private" / "run-state.json"
    created = _run(
        "create",
        "--file",
        str(state),
        "--run-id",
        "run-1",
        "--repo",
        "example/repository",
        "--issue",
        "7",
        "--issue-title-sha256",
        TITLE_HASH,
        "--issue-body-sha256",
        BODY_HASH,
        "--base-branch",
        "main",
        "--branch",
        "agent-loop/issue-7-run-1",
        "--worktree",
        str(tmp_path / "worktree"),
        "--log-dir",
        str(tmp_path / "logs"),
        "--pr",
        "9",
        "--pr-url",
        "https://example.invalid/pr/9",
        "--base-sha",
        BASE,
        "--head-sha",
        HEAD,
    )
    assert created.returncode == 0, created.stderr
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700
    value = json.loads(created.stdout)
    assert value["phase"] == "draft-open"
    updated = _run(
        "update",
        "--file",
        str(state),
        "--phase",
        "reviewing",
        "--round",
        "2",
        "--base-sha",
        BASE,
        "--head-sha",
        "c" * 40,
    )
    assert updated.returncode == 0, updated.stderr
    shown = _run("show", "--file", str(state))
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["round"] == 2


def test_state_rejects_permissive_or_unknown_content(tmp_path: Path) -> None:
    state = tmp_path / "run-state.json"
    state.write_text("{}\n", encoding="utf-8")
    state.chmod(0o644)
    result = _run("show", "--file", str(state))
    assert result.returncode != 0
    assert "permissions" in result.stderr
    state.chmod(0o600)
    result = _run("show", "--file", str(state))
    assert result.returncode != 0
    assert "missing or unknown" in result.stderr


def test_state_rejects_json_booleans_for_integer_fields(tmp_path: Path) -> None:
    state = tmp_path / "run-state.json"
    value = {
        "version": 1,
        "runId": "run-1",
        "repo": "example/repository",
        "issue": True,
        "issueTitleSha256": TITLE_HASH,
        "issueBodySha256": BODY_HASH,
        "baseBranch": "main",
        "branch": "agent-loop/issue-7-run-1",
        "worktree": str((tmp_path / "worktree").resolve()),
        "logDir": str((tmp_path / "logs").resolve()),
        "prNumber": 9,
        "prUrl": "https://example.invalid/pr/9",
        "baseSha": BASE,
        "headSha": HEAD,
        "phase": "draft-open",
        "round": 1,
        "codexResultSha256": None,
        "claudeResultSha256": None,
    }
    state.write_text(json.dumps(value), encoding="utf-8")
    state.chmod(0o600)
    result = _run("show", "--file", str(state))
    assert result.returncode != 0
    assert "positive integer" in result.stderr


def test_state_create_never_clobbers_an_existing_file(tmp_path: Path) -> None:
    state = tmp_path / "run-state.json"
    state.write_text("preserve me\n", encoding="utf-8")
    state.chmod(0o600)
    result = _run(
        "create",
        "--file",
        str(state),
        "--run-id",
        "run-1",
        "--repo",
        "example/repository",
        "--issue",
        "7",
        "--issue-title-sha256",
        TITLE_HASH,
        "--issue-body-sha256",
        BODY_HASH,
        "--base-branch",
        "main",
        "--branch",
        "agent-loop/issue-7-run-1",
        "--worktree",
        str(tmp_path / "worktree"),
        "--log-dir",
        str(tmp_path / "logs"),
        "--pr",
        "9",
        "--pr-url",
        "https://example.invalid/pr/9",
        "--base-sha",
        BASE,
        "--head-sha",
        HEAD,
    )
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert state.read_text(encoding="utf-8") == "preserve me\n"


def test_converged_state_requires_and_preserves_review_result_hashes(
    tmp_path: Path,
) -> None:
    state = tmp_path / "run-state.json"
    created = _run(
        "create",
        "--file",
        str(state),
        "--run-id",
        "run-1",
        "--repo",
        "example/repository",
        "--issue",
        "7",
        "--issue-title-sha256",
        TITLE_HASH,
        "--issue-body-sha256",
        BODY_HASH,
        "--base-branch",
        "main",
        "--branch",
        "agent-loop/issue-7-run-1",
        "--worktree",
        str(tmp_path / "worktree"),
        "--log-dir",
        str(tmp_path / "logs"),
        "--pr",
        "9",
        "--pr-url",
        "https://example.invalid/pr/9",
        "--base-sha",
        BASE,
        "--head-sha",
        HEAD,
    )
    assert created.returncode == 0, created.stderr
    missing = _run(
        "update",
        "--file",
        str(state),
        "--phase",
        "converged",
    )
    assert missing.returncode != 0
    assert "requires both review result hashes" in missing.stderr

    updated = _run(
        "update",
        "--file",
        str(state),
        "--phase",
        "converged",
        "--codex-result-sha256",
        "c" * 64,
        "--claude-result-sha256",
        "d" * 64,
    )
    assert updated.returncode == 0, updated.stderr
    value = json.loads(state.read_text(encoding="utf-8"))
    assert value["codexResultSha256"] == "c" * 64
    assert value["claudeResultSha256"] == "d" * 64

    finalized = _run(
        "update",
        "--file",
        str(state),
        "--phase",
        "finalized",
    )
    assert finalized.returncode == 0, finalized.stderr
    value = json.loads(state.read_text(encoding="utf-8"))
    assert value["codexResultSha256"] == "c" * 64
