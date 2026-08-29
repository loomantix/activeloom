"""Tests for private, atomic agent-loop run-state checkpoints."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / ".agents/skills/agent-loop/scripts/agent-loop-state.py"
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
        "--review-deadline-epoch",
        "2000000000",
        "--review-max-rounds",
        "4",
    )
    assert created.returncode == 0, created.stderr
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700
    value = json.loads(created.stdout)
    assert value["version"] == 2
    assert value["phase"] == "draft-open"
    assert value["reviewEngine"] is None
    assert value["reviewDeadlineEpoch"] == 2000000000
    assert value["reviewMaxRounds"] == 4
    missing_engine = _run(
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
    assert missing_engine.returncode != 0
    assert "current review engine" in missing_engine.stderr
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
        "--review-engine",
        "gemini",
    )
    assert updated.returncode == 0, updated.stderr
    shown = _run("show", "--file", str(state))
    assert shown.returncode == 0, shown.stderr
    shown_value = json.loads(shown.stdout)
    assert shown_value["round"] == 2
    assert shown_value["reviewEngine"] == "gemini"


def test_batch_state_persists_order_cursor_statuses_and_child_paths(tmp_path: Path) -> None:
    state = tmp_path / "private" / "batch-state.json"
    created = _run(
        "batch-create", "--file", str(state), "--run-id", "batch-1",
        "--repo", "example/repository", "--base-branch", "main",
        "--issues", "7,8",
    )
    assert created.returncode == 0, created.stderr
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    active = _run(
        "batch-update", "--file", str(state), "--issue", "7",
        "--expected-status", "pending", "--status", "active",
        "--child-run-state", str(tmp_path / "child-7.json"),
    )
    assert active.returncode == 0, active.stderr
    finalized = _run(
        "batch-update", "--file", str(state), "--issue", "7",
        "--expected-status", "active", "--status", "finalized",
    )
    assert finalized.returncode == 0, finalized.stderr
    value = json.loads(state.read_text(encoding="utf-8"))
    assert value["allowlist"] == [7, 8]
    assert value["cursor"] == 1
    assert value["issues"][0] == {
        "issue": 7,
        "status": "finalized",
        "childRunState": str((tmp_path / "child-7.json").resolve()),
    }
    assert value["issues"][1]["status"] == "pending"


def test_batch_never_skips_active_uncertain_issue(tmp_path: Path) -> None:
    state = tmp_path / "batch-state.json"
    assert _run(
        "batch-create", "--file", str(state), "--run-id", "batch-1",
        "--repo", "example/repository", "--base-branch", "main", "--issues", "7,8",
    ).returncode == 0
    assert _run(
        "batch-update", "--file", str(state), "--issue", "7",
        "--expected-status", "pending", "--status", "active"
    ).returncode == 0
    skipped = _run(
        "batch-update", "--file", str(state), "--issue", "8",
        "--expected-status", "pending", "--status", "active"
    )
    assert skipped.returncode != 0
    assert "current cursor issue" in skipped.stderr
    bailed = _run(
        "batch-update", "--file", str(state), "--issue", "7",
        "--expected-status", "active", "--status", "bailed"
    )
    assert bailed.returncode == 0, bailed.stderr
    assert json.loads(state.read_text())["cursor"] == 1


def test_batch_pending_issue_can_be_explicitly_bailed(tmp_path: Path) -> None:
    state = tmp_path / "batch-state.json"
    assert _run(
        "batch-create", "--file", str(state), "--run-id", "batch-1",
        "--repo", "example/repository", "--base-branch", "main", "--issues", "7,8",
    ).returncode == 0
    bailed = _run(
        "batch-update", "--file", str(state), "--issue", "7",
        "--expected-status", "pending", "--status", "bailed",
    )
    assert bailed.returncode == 0, bailed.stderr
    value = json.loads(state.read_text(encoding="utf-8"))
    assert value["cursor"] == 1
    assert value["issues"][0]["status"] == "bailed"


def test_batch_expected_status_is_atomic_across_concurrent_updates(
    tmp_path: Path,
) -> None:
    state = tmp_path / "batch-state.json"
    assert _run(
        "batch-create", "--file", str(state), "--run-id", "batch-1",
        "--repo", "example/repository", "--base-branch", "main", "--issues", "7",
    ).returncode == 0
    lock_descriptor = os.open(f"{state}.lock", os.O_RDWR)
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    command = [
        "python3", str(HELPER), "batch-update", "--file", str(state),
        "--issue", "7", "--expected-status", "pending", "--status", "active",
    ]
    processes = [
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    try:
        time.sleep(0.1)
        assert all(process.poll() is None for process in processes)
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)
    results = [process.communicate(timeout=10) for process in processes]
    assert sorted(process.returncode for process in processes) == [0, 1]
    assert any("expected pending, found active" in stderr for _, stderr in results)


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


def test_state_rejects_pre_gemini_protocol_checkpoint(tmp_path: Path) -> None:
    state = tmp_path / "run-state.json"
    value = {
        "version": 1,
        "runId": "run-1",
        "repo": "example/repository",
        "issue": 7,
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
        "reviewEngine": None,
        "geminiResultSha256": None,
        "claudeResultSha256": None,
    }
    state.write_text(json.dumps(value), encoding="utf-8")
    state.chmod(0o600)
    result = _run("show", "--file", str(state))
    assert result.returncode != 0
    assert "unsupported run state version" in result.stderr


def test_state_rejects_json_booleans_for_integer_fields(tmp_path: Path) -> None:
    state = tmp_path / "run-state.json"
    value = {
        "version": 2,
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
        "reviewEngine": None,
        "geminiResultSha256": None,
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
        "--gemini-result-sha256",
        "c" * 64,
        "--claude-result-sha256",
        "d" * 64,
    )
    assert updated.returncode == 0, updated.stderr
    value = json.loads(state.read_text(encoding="utf-8"))
    assert value["geminiResultSha256"] == "c" * 64
    assert value["claudeResultSha256"] == "d" * 64
    assert value["reviewEngine"] is None

    finalizing = _run(
        "update",
        "--file",
        str(state),
        "--phase",
        "finalizing",
    )
    assert finalizing.returncode == 0, finalizing.stderr
    value = json.loads(state.read_text(encoding="utf-8"))
    assert value["geminiResultSha256"] == "c" * 64

    finalized = _run(
        "update",
        "--file",
        str(state),
        "--phase",
        "finalized",
    )
    assert finalized.returncode == 0, finalized.stderr
    value = json.loads(state.read_text(encoding="utf-8"))
    assert value["geminiResultSha256"] == "c" * 64
