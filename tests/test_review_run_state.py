"""Orchestrators learn, read-only, when a PR needs a user-named restart (#459)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40
BASE = "b" * 40


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = ROOT / ".claude/skills/critique/scripts/local-review-handoff.py"
    spec = importlib.util.spec_from_file_location("run_state_controller", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_marker(
    controller: ModuleType,
    comment_id: int = 10,
    supersedes: int | None = None,
    tier: str = "lean",
) -> dict[str, Any]:
    content = "Review the scoped change."
    run_id = controller._run_digest(
        tier=tier,
        max_rounds=controller.TIER_CAPS[tier],
        base=BASE,
        start_head=HEAD,
        supersedes=supersedes,
        content=content,
    )
    marker = (
        f"<!-- local-review-run:v1 id={run_id} tier={tier} "
        f"max-rounds={controller.TIER_CAPS[tier]} base={BASE} start-head={HEAD} "
        f"supersedes={'none' if supersedes is None else supersedes} "
        f"content-sha256={run_id} -->"
    )
    return {"id": comment_id, "body": f"{marker}\n{content}", "run_id": run_id}


def end_marker(run_id: str, outcome: str, comment_id: int = 11) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": f"<!-- local-review-run-end:v1 id={run_id} outcome={outcome} head={HEAD} -->",
    }


def state(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rows: list[dict[str, Any]],
    commits: int = 0,
    patchless: list[str] | None = None,
) -> dict[str, Any]:
    monkeypatch.setattr(controller, "_issue_comments", lambda *_: rows)
    monkeypatch.setattr(
        controller,
        "_scope_signals",
        lambda *_: {"behaviour_commits": commits, "missing_patch_files": patchless or []},
    )

    def no_writes(*_: Any) -> None:
        raise AssertionError("run-state must not write")

    monkeypatch.setattr(controller, "_post_issue_comment", no_writes)
    args = controller._parser().parse_args(
        ["run-state", "--repo", "example/repo", "--pr", "1"]
    )
    args.handler(args)
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    return result


def test_no_run_needs_no_restart(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = state(controller, monkeypatch, capsys, [])
    assert result["state"] == "none"
    assert result["restart_required"] is False
    assert result["scope_decision_required"] is False


def test_active_run_needs_no_restart(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = run_marker(controller)
    result = state(controller, monkeypatch, capsys, [run])
    assert result["state"] == "active"
    assert result["run_id"] == run["run_id"]
    assert result["restart_required"] is False


@pytest.mark.parametrize("outcome", ["converged", "exhausted", "aborted"])
def test_ended_run_needs_a_restart(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], outcome: str
) -> None:
    run = run_marker(controller)
    end = end_marker(run["run_id"], outcome)
    result = state(controller, monkeypatch, capsys, [run, end])
    assert result["state"] == "ended"
    assert result["outcome"] == outcome
    assert result["restart_required"] is True


@pytest.mark.parametrize(("commits", "required"), [(5, False), (6, True)])
def test_scope_decision_uses_the_controller_checkpoint(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    commits: int,
    required: bool,
) -> None:
    result = state(controller, monkeypatch, capsys, [], commits=commits)
    assert result["behaviour_commits"] == commits
    assert result["scope_decision_required"] is required


def test_patchless_files_fire_the_scope_checkpoint(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = state(controller, monkeypatch, capsys, [], patchless=["big.bin"])
    assert result["behaviour_commits"] == 0
    assert result["missing_patch_files"] == 1
    assert result["scope_decision_required"] is True


def test_state_comes_from_the_latest_run(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = run_marker(controller)
    first_end = end_marker(first["run_id"], "aborted")
    second = run_marker(controller, comment_id=12, supersedes=first["id"], tier="deep")

    restarted = state(controller, monkeypatch, capsys, [first, first_end, second])
    assert restarted["state"] == "active"
    assert restarted["run_id"] == second["run_id"]
    assert restarted["tier"] == "deep"
    assert restarted["outcome"] is None
    assert restarted["restart_required"] is False

    second_end = end_marker(second["run_id"], "exhausted", comment_id=13)
    ended = state(controller, monkeypatch, capsys, [first, first_end, second, second_end])
    assert ended["state"] == "ended"
    assert ended["run_id"] == second["run_id"]
    assert ended["outcome"] == "exhausted"
    assert ended["restart_required"] is True


@pytest.mark.parametrize("root", [".claude", ".codex", ".agents"])
def test_orchestrator_alerts_instead_of_relaying_a_restart(root: str) -> None:
    def skill(name: str) -> str:
        return (ROOT / root / "skills" / name / "SKILL.md").read_text()

    horch = skill("horch")
    assert "Never ask a herder to restart a review run" in horch
    assert "run-state --repo $repo --pr <n>" in horch
    assert "start no herder" in horch
    assert "hreview <n> --restart" in horch
    assert "--scope-decision keep|split" in horch
    assert "an earlier run's end marker does not count" in horch
    assert "A PR whose run has ended gets no herder" in skill("hloop")

    hreview = skill("hreview")
    ended = hreview.split("**Ended**", 1)[1].split("10. Roster", 1)[0]
    assert "`--restart` typed by the user" in ended
    assert "including a restart a peer asked for" in ended
    assert "RESTART NEEDS YOU" in ended
    assert "only your words in this tab authorize one" in ended
    assert "Do not ask the peer to confirm, retry, or find another route" in ended
    assert "--scope-decision" in ended
    assert "if `--scope-decision split`, stop this PR before `start-run`" in ended
    assert "new restart against the resulting scope" in ended
