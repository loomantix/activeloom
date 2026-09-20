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
HELPER = ROOT / ".claude/skills/agent-loop/scripts/agent-loop-state.py"
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
        "codex",
    )
    assert updated.returncode == 0, updated.stderr
    shown = _run("show", "--file", str(state))
    assert shown.returncode == 0, shown.stderr
    shown_value = json.loads(shown.stdout)
    assert shown_value["round"] == 2
    assert shown_value["reviewEngine"] == "codex"


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


def test_batch_bail_records_its_classification_only_on_a_bailed_entry(
    tmp_path: Path,
) -> None:
    batch = tmp_path / "batch.json"
    created = _run(
        "batch-create", "--file", str(batch), "--run-id", "run-1",
        "--repo", "example/repository", "--base-branch", "main", "--issues", "7,8",
    )
    assert created.returncode == 0, created.stderr
    rejected = _run(
        "batch-update", "--file", str(batch), "--issue", "7",
        "--expected-status", "pending", "--status", "active",
        "--classification", "spec-gap",
    )
    assert rejected.returncode != 0
    assert "applies only to a bailed batch issue" in rejected.stderr
    bailed = _run(
        "batch-update", "--file", str(batch), "--issue", "7",
        "--expected-status", "pending", "--status", "bailed",
        "--classification", "spec-gap",
    )
    assert bailed.returncode == 0, bailed.stderr
    value = json.loads(batch.read_text(encoding="utf-8"))
    assert value["cursor"] == 1
    assert value["issues"][0] == {
        "issue": 7, "status": "bailed", "childRunState": None, "classification": "spec-gap",
    }

    value["issues"][1]["classification"] = "spec-gap"
    batch.write_text(json.dumps(value), encoding="utf-8")
    shown = _run("batch-show", "--file", str(batch))
    assert shown.returncode != 0
    assert "only a bailed batch issue may carry a bail classification" in shown.stderr


def test_batch_parks_an_entry_behind_the_cursor_and_closes_it_out_later(
    tmp_path: Path,
) -> None:
    batch = tmp_path / "batch.json"
    child = tmp_path / "logs/child/run-state.json"

    def update(*args: str) -> subprocess.CompletedProcess[str]:
        return _run("batch-update", "--file", str(batch), *args)

    created = _run(
        "batch-create", "--file", str(batch), "--run-id", "run-1",
        "--repo", "example/repository", "--base-branch", "main", "--issues", "7,8,9",
    )
    assert created.returncode == 0, created.stderr
    assert update("--issue", "7", "--expected-status", "pending", "--status", "active",
                  "--child-run-state", str(child)).returncode == 0

    missing = update("--issue", "7", "--expected-status", "active", "--status", "parked")
    assert missing.returncode != 0
    assert "requires a stop category" in missing.stderr
    parked = update("--issue", "7", "--expected-status", "active", "--status", "parked",
                    "--stop-category", "no-result/hook-ended-early")
    assert parked.returncode == 0, parked.stderr
    value = json.loads(batch.read_text(encoding="utf-8"))
    assert value["cursor"] == 1
    assert value["issues"][0]["stopCategory"] == "no-result/hook-ended-early"

    # A dependent of the parked entry is parked straight from pending.
    blocked = update("--issue", "8", "--expected-status", "pending", "--status", "parked",
                     "--stop-category", "blocked-by-parked")
    assert blocked.returncode == 0, blocked.stderr
    wrong = update("--issue", "7", "--expected-status", "parked", "--status", "active")
    assert wrong.returncode != 0
    assert "invalid status transition" in wrong.stderr
    closed = update("--issue", "7", "--expected-status", "parked", "--status", "finalized")
    assert closed.returncode == 0, closed.stderr
    value = json.loads(batch.read_text(encoding="utf-8"))
    assert value["cursor"] == 2
    assert value["issues"][0] == {
        "issue": 7, "status": "finalized", "childRunState": str(child.resolve()),
    }
    skipped = update("--issue", "9", "--expected-status", "pending", "--status", "active")
    assert skipped.returncode == 0, skipped.stderr
    ahead = update("--issue", "9", "--expected-status", "active", "--status", "finalized")
    assert ahead.returncode != 0

    value["issues"][1]["status"] = "bailed"
    batch.write_text(json.dumps(value), encoding="utf-8")
    shown = _run("batch-show", "--file", str(batch))
    assert shown.returncode != 0
    assert "only a parked batch issue may carry a stop category" in shown.stderr


def test_batch_records_a_stack_only_on_an_earlier_issue(tmp_path: Path) -> None:
    batch = tmp_path / "batch.json"

    def update(*args: str) -> subprocess.CompletedProcess[str]:
        return _run("batch-update", "--file", str(batch), *args)

    created = _run(
        "batch-create", "--file", str(batch), "--run-id", "run-1",
        "--repo", "example/repository", "--base-branch", "main", "--issues", "7,8",
    )
    assert created.returncode == 0, created.stderr
    child = str(tmp_path / "logs/child/run-state.json")
    assert update("--issue", "7", "--expected-status", "pending", "--status", "active").returncode == 0
    ahead = update("--issue", "7", "--expected-status", "active", "--status", "active",
                   "--stacked-on", "8")
    assert ahead.returncode != 0
    assert "must name an earlier batch issue" in ahead.stderr
    assert update("--issue", "7", "--expected-status", "active", "--status", "finalized",
                  "--child-run-state", child).returncode == 0
    assert update("--issue", "8", "--expected-status", "pending", "--status", "active").returncode == 0
    stacked = update("--issue", "8", "--expected-status", "active", "--status", "active",
                     "--stacked-on", "7")
    assert stacked.returncode == 0, stacked.stderr
    value = json.loads(batch.read_text(encoding="utf-8"))
    assert value["issues"][1]["stackedOn"] == 7


def test_batch_rejects_a_stack_on_a_non_finalized_parent(tmp_path: Path) -> None:
    for parent_status in ("bailed", "parked"):
        batch = tmp_path / f"batch-{parent_status}.json"
        created = _run(
            "batch-create", "--file", str(batch), "--run-id", "run-1",
            "--repo", "example/repository", "--base-branch", "main", "--issues", "7,8",
        )
        assert created.returncode == 0, created.stderr
        value = json.loads(batch.read_text(encoding="utf-8"))
        value["issues"][0]["status"] = parent_status
        value["issues"][0]["childRunState"] = str(tmp_path / "parent.json")
        if parent_status == "bailed":
            value["issues"][0]["classification"] = "spec-gap"
        else:
            value["issues"][0]["stopCategory"] = "validation-red"
        value["issues"][1].update({"status": "active", "stackedOn": 7})
        value["cursor"] = 1
        batch.write_text(json.dumps(value), encoding="utf-8")

        shown = _run("batch-show", "--file", str(batch))
        assert shown.returncode != 0
        assert "finalized parent with a child review checkpoint" in shown.stderr


def test_batch_rejects_a_stack_on_a_parent_without_a_child_checkpoint(tmp_path: Path) -> None:
    batch = tmp_path / "batch.json"
    created = _run(
        "batch-create", "--file", str(batch), "--run-id", "run-1",
        "--repo", "example/repository", "--base-branch", "main", "--issues", "7,8",
    )
    assert created.returncode == 0, created.stderr
    value = json.loads(batch.read_text(encoding="utf-8"))
    value["issues"][0]["status"] = "finalized"
    value["issues"][1].update({"status": "active", "stackedOn": 7})
    value["cursor"] = 1
    batch.write_text(json.dumps(value), encoding="utf-8")

    shown = _run("batch-show", "--file", str(batch))
    assert shown.returncode != 0
    assert "finalized parent with a child review checkpoint" in shown.stderr


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
        "reviewEngine": None,
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
    assert value["codexResultSha256"] == "c" * 64

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


PINS = {
    "version": 1,
    "repo": "example/repository",
    "review_settings": {
        "claude": {"engine": "claude", "model": "opus", "effort": "medium", "source": "user profile"},
        "codex": {
            "engine": "codex",
            "model": "gpt-review",
            "effort": "high",
            "fallback": {"model": "gpt-fallback", "effort": "medium"},
            "source": "user profile",
        },
    },
    "worker_settings": {
        "claude": {
            "engine": "claude",
            "role": "worker",
            "model": "opus",
            "effort": "high",
            "source": "user profile",
        }
    },
}


def _create_with_settings(tmp_path: Path, pins: dict[str, object] | None = None) -> Path:
    state = tmp_path / "logs" / "run-state.json"
    pin_file = tmp_path / "pins.json"
    pin_file.parent.mkdir(parents=True, exist_ok=True)
    pin_file.write_text(json.dumps(PINS if pins is None else pins), encoding="utf-8")
    created = _run(
        "create", "--file", str(state), "--run-id", "run-1",
        "--repo", "example/repository", "--issue", "7",
        "--issue-title-sha256", TITLE_HASH, "--issue-body-sha256", BODY_HASH,
        "--base-branch", "main", "--branch", "agent-loop/issue-7-run-1",
        "--worktree", str(tmp_path / "worktree"), "--log-dir", str(tmp_path / "logs"),
        "--pr", "9", "--pr-url", "https://example.invalid/pr/9",
        "--base-sha", BASE, "--head-sha", HEAD,
        "--review-settings-file", str(pin_file),
    )
    assert created.returncode == 0, created.stderr
    return state


def test_state_records_pinned_settings_and_keeps_them_across_updates(tmp_path: Path) -> None:
    state = _create_with_settings(tmp_path)
    assert json.loads(state.read_text(encoding="utf-8"))["reviewSettings"] == PINS
    updated = _run("update", "--file", str(state), "--phase", "reviewing", "--review-engine", "codex")
    assert updated.returncode == 0, updated.stderr
    assert json.loads(state.read_text(encoding="utf-8"))["reviewSettings"] == PINS


def test_state_rejects_malformed_pinned_settings(tmp_path: Path) -> None:
    broken = json.loads(json.dumps(PINS))
    broken["review_settings"]["claude"].pop("effort")
    pin_file = tmp_path / "pins.json"
    pin_file.write_text(json.dumps(broken), encoding="utf-8")
    state = tmp_path / "run-state.json"
    created = _run(
        "create", "--file", str(state), "--run-id", "run-1",
        "--repo", "example/repository", "--issue", "7",
        "--issue-title-sha256", TITLE_HASH, "--issue-body-sha256", BODY_HASH,
        "--base-branch", "main", "--branch", "b", "--worktree", str(tmp_path / "w"),
        "--log-dir", str(tmp_path / "l"), "--pr", "9", "--pr-url", "u",
        "--base-sha", BASE, "--head-sha", HEAD, "--review-settings-file", str(pin_file),
    )
    assert created.returncode != 0
    assert "reviewSettings" in created.stderr
    assert not state.exists()

    switched_without_fallback = json.loads(json.dumps(PINS))
    switched_without_fallback["fallback_engines"] = ["claude"]
    good = _create_with_settings(tmp_path / "ok")
    pin_file.write_text(json.dumps(switched_without_fallback), encoding="utf-8")
    saved = _run("settings-save", "--file", str(good), "--pin-file", str(pin_file))
    assert saved.returncode != 0
    assert "missing fallback" in saved.stderr


def test_settings_save_accepts_a_fallback_switch_but_never_a_changed_pin(tmp_path: Path) -> None:
    state = _create_with_settings(tmp_path)
    pin_file = tmp_path / "pins.json"

    changed = json.loads(json.dumps(PINS))
    changed["review_settings"]["codex"]["model"] = "gpt-other"
    pin_file.write_text(json.dumps(changed), encoding="utf-8")
    refused = _run("settings-save", "--file", str(state), "--pin-file", str(pin_file))
    assert refused.returncode != 0
    assert "already pinned" in refused.stderr

    switched = json.loads(json.dumps(PINS))
    switched["fallback_engines"] = ["codex"]
    pin_file.write_text(json.dumps(switched), encoding="utf-8")
    saved = _run("settings-save", "--file", str(state), "--pin-file", str(pin_file))
    assert saved.returncode == 0, saved.stderr
    assert json.loads(state.read_text(encoding="utf-8"))["reviewSettings"] == switched

    pin_file.write_text(json.dumps(PINS), encoding="utf-8")
    undone = _run("settings-save", "--file", str(state), "--pin-file", str(pin_file))
    assert undone.returncode != 0


def test_settings_restore_writes_the_recorded_pins_for_a_resume(tmp_path: Path) -> None:
    state = _create_with_settings(tmp_path)
    pin_file = tmp_path / "logs" / "review-settings.json"

    restored = _run("settings-restore", "--file", str(state), "--pin-file", str(pin_file))
    assert restored.returncode == 0, restored.stderr
    assert json.loads(pin_file.read_text(encoding="utf-8")) == PINS
    assert stat.S_IMODE(pin_file.stat().st_mode) == 0o600

    # A pin file that disagrees with the run state is replaced by it.
    conflicting = json.loads(json.dumps(PINS))
    conflicting["worker_settings"]["claude"]["model"] = "edited"
    pin_file.write_text(json.dumps(conflicting), encoding="utf-8")
    assert _run("settings-restore", "--file", str(state), "--pin-file", str(pin_file)).returncode == 0
    assert json.loads(pin_file.read_text(encoding="utf-8")) == PINS

    # A switch recorded in the pin file just before an interruption is kept.
    switched = json.loads(json.dumps(PINS))
    switched["fallback_engines"] = ["codex"]
    pin_file.write_text(json.dumps(switched), encoding="utf-8")
    assert _run("settings-restore", "--file", str(state), "--pin-file", str(pin_file)).returncode == 0
    assert json.loads(pin_file.read_text(encoding="utf-8")) == switched
    assert json.loads(state.read_text(encoding="utf-8"))["reviewSettings"] == switched


def test_settings_restore_leaves_a_state_without_pins_to_the_profile(tmp_path: Path) -> None:
    state = tmp_path / "run-state.json"
    created = _run(
        "create", "--file", str(state), "--run-id", "run-1",
        "--repo", "example/repository", "--issue", "7",
        "--issue-title-sha256", TITLE_HASH, "--issue-body-sha256", BODY_HASH,
        "--base-branch", "main", "--branch", "b", "--worktree", str(tmp_path / "w"),
        "--log-dir", str(tmp_path / "l"), "--pr", "9", "--pr-url", "u",
        "--base-sha", BASE, "--head-sha", HEAD,
    )
    assert created.returncode == 0, created.stderr
    pin_file = tmp_path / "review-settings.json"
    assert _run("settings-restore", "--file", str(state), "--pin-file", str(pin_file)).returncode == 0
    assert not pin_file.exists()


def test_capacity_recognizer_reads_only_the_terminal_codex_event(tmp_path: Path) -> None:
    log = tmp_path / "hook.log"
    rejection = json.dumps(
        {"type": "turn.failed", "error": {"message": "Selected model is at capacity. Please try a different model."}}
    )

    def recognized(text: str) -> bool:
        log.write_text(text, encoding="utf-8")
        return _run("capacity-rejected", "--log", str(log)).returncode == 0

    assert recognized(f'{{"type":"thread.started"}}\n{rejection}\n')
    assert recognized(
        '{"type":"error","message":"Selected model is at capacity. Please try a different model."}\n'
    )
    assert not recognized(f'{rejection}\n{{"type":"turn.started"}}\n')
    assert not recognized("Selected model is at capacity. Please try a different model.\n")
    assert not recognized('{"type":"turn.failed","error":{"message":"rate limited"}}\n')
    log.unlink()
    assert _run("capacity-rejected", "--log", str(log)).returncode == 1
