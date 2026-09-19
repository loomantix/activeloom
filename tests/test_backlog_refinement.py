"""Behavioral coverage for the backlog-refinement helper scripts."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import _load_script


REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = REPO_ROOT / ".claude/skills/backlog-refinement/scripts/candidates.py"
BAIL_REPORT = REPO_ROOT / ".claude/skills/backlog-refinement/scripts/bail-report.py"


PRIORITIES = "priority: critical, priority: high, priority: medium, priority: low"


def _load_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: str = "",
    *,
    rubric: str | None = None,
    location: str = ".backlog/refinement.local.md",
    name: str | None = None,
) -> ModuleType:
    """Load candidates.py with ``tmp_path`` as the repository being refined.

    ``tmp_path`` is not a git checkout, so the script's repo-root lookup falls
    back to the working directory — which the test points at ``tmp_path``.
    ``rubric=None`` writes a local file with the given skip marker and the
    default priority labels; pass text to write it verbatim, or ``location=""``
    to write no rubric at all.
    """
    monkeypatch.chdir(tmp_path)
    if location:
        path = tmp_path / location
        path.parent.mkdir(parents=True, exist_ok=True)
        if rubric is None:
            rubric = (
                f"# Rubric\n\n<!-- auto-managed-labels: {marker} -->\n\n"
                f"<!-- priority-labels: {PRIORITIES} -->\n"
            )
        path.write_text(rubric, encoding="utf-8")
    return _load_script(name or f"candidates_{tmp_path.name}", CANDIDATES)


def _issue(*labels: str, title: str = "Task") -> dict[str, Any]:
    return {
        "number": 1,
        "title": title,
        "labels": [{"name": label} for label in labels],
        "assignees": [],
    }


def test_candidates_bail_label_wins_over_stale_ready_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    assert mod.classify(_issue("dev: agent", "agent-bail: spec-gap")) == "excluded"


def test_candidates_reads_the_repo_local_rubric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch, "nightly-digest, automated-report")
    assert mod.CONFIG.source == str(tmp_path / ".backlog/refinement.local.md")
    assert mod.CONFIG.auto_managed_labels == ("nightly-digest", "automated-report")
    assert mod.CONFIG.priority_labels == tuple(PRIORITIES.split(", "))
    assert mod.classify(_issue("nightly-digest")) == "skipped"
    assert mod.classify(_issue("unrelated")) == "unrefined"


def test_candidates_falls_back_to_a_legacy_harness_rubric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A repo that predates `.backlog/` keeps working and is told to migrate.

    A legacy rubric predates the priority marker, so priority backfill stays off
    until `setup` migrates it rather than guessing at label names.
    """
    mod = _load_candidates(
        tmp_path,
        monkeypatch,
        rubric="# Legacy\n\n<!-- auto-managed-labels: legacy-skip -->\n",
        location=".codex/skills/backlog-refinement/RUBRIC.md",
    )
    assert mod.CONFIG.auto_managed_labels == ("legacy-skip",)
    assert mod.CONFIG.priority_labels == ()
    err = capsys.readouterr().err
    assert "legacy rubric" in err and "setup" in err


def test_candidates_local_rubric_wins_over_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / ".claude/skills/backlog-refinement/RUBRIC.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("<!-- auto-managed-labels: legacy-skip -->\n", encoding="utf-8")
    mod = _load_candidates(tmp_path, monkeypatch, "local-skip")
    assert mod.CONFIG.auto_managed_labels == ("local-skip",)


def test_candidates_without_any_rubric_uses_core_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch, location="")
    assert mod.CONFIG == mod.RubricConfig(None, (), ())
    assert "setup" in capsys.readouterr().err
    assert mod.classify(_issue("anything")) == "unrefined"


def test_candidates_absent_marker_disables_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rubric without the markers must not crash (documented safe default)."""
    mod = _load_candidates(tmp_path, monkeypatch, rubric="# no marker\n")
    assert mod.CONFIG.auto_managed_labels == ()
    assert mod.CONFIG.priority_labels == ()
    assert mod.classify(_issue("anything")) == "unrefined"


def test_candidates_warns_on_an_off_shape_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_candidates(
        tmp_path, monkeypatch, rubric="- <!-- priority-labels: p1, p2 --> trailing\n"
    )
    assert mod.CONFIG.priority_labels == ()
    assert "not on a line of its own" in capsys.readouterr().err


def test_candidates_rejects_duplicate_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More than one marker is ambiguous — fail closed rather than guess."""
    with pytest.raises(SystemExit) as exc_info:
        _load_candidates(
            tmp_path,
            monkeypatch,
            rubric="<!-- auto-managed-labels: one -->\n<!-- auto-managed-labels: two -->\n",
        )
    assert exc_info.value.code == 1


def test_candidates_backfill_flags_missing_priority_and_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    # A grill-class bail missing both.
    assert mod.backfill_gaps(_issue("agent: refined", "agent-bail: open-decision")) == [
        "priority",
        "needs",
    ]
    # A non-grill bail never needs a needs: label.
    assert mod.backfill_gaps(
        _issue("agent: refined", "agent-bail: credential-gate", "priority: low")
    ) == []
    # An existing needs: label and priority satisfy both.
    assert mod.backfill_gaps(
        _issue("agent-bail: epic", "needs: grill", "priority: high")
    ) == []
    # A ready issue still needs a priority.
    assert mod.backfill_gaps(_issue("dev: agent", "agent: refined")) == ["priority"]


def test_candidates_empty_priority_marker_disables_priority_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(
        tmp_path, monkeypatch, rubric="<!-- priority-labels: -->\n"
    )
    assert mod.backfill_gaps(_issue("dev: agent", "agent: refined")) == []


def test_candidates_reports_conflicting_priorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    assert mod.priority_conflicted(_issue("priority: high", "priority: low"))
    assert not mod.priority_conflicted(_issue("priority: high", "unrelated"))


def test_candidates_required_query_fails_closed_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)

    def timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("gh", 60)

    monkeypatch.setattr(mod.subprocess, "run", timeout)
    with pytest.raises(SystemExit) as exc_info:
        mod.fetch_open_issues()
    assert exc_info.value.code == 1


def test_candidates_rejects_invalid_json_and_possible_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "not-json", ""),
    )
    with pytest.raises(SystemExit):
        mod.fetch_open_issues()

    payload = json.dumps([{}] * mod.GH_LIST_LIMIT)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, payload, ""),
    )
    with pytest.raises(SystemExit):
        mod.fetch_open_issues()


def test_candidates_limit_rejects_negative_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    with pytest.raises(argparse.ArgumentTypeError, match="zero or greater"):
        mod.non_negative_int("-1")


@pytest.fixture(scope="module")
def bail_mod() -> ModuleType:
    return _load_script("bail_report_tests", BAIL_REPORT)


def test_bail_since_normalizes_offsets_to_utc(bail_mod: ModuleType) -> None:
    parsed = bail_mod.parse_since("2026-01-01T01:00:00+01:00")
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-01-01T00:00:00+00:00"


def test_bail_bucket_uses_consumer_rca_before_legacy_default(
    bail_mod: ModuleType,
) -> None:
    assert bail_mod.is_bucket_a(
        {"_rca": {"bucket": "A", "category": "agent-bail: custom"}},
        "agent-bail: custom",
    )
    assert not bail_mod.is_bucket_a(
        {"_rca": {"bucket": "B", "category": "agent-bail: stale"}},
        "agent-bail: stale",
    )
    assert bail_mod.is_bucket_a({"_rca": None}, "agent-bail: stale")


def test_bail_bucket_does_not_apply_stub_to_another_category(
    bail_mod: ModuleType,
) -> None:
    issue = {
        "_rca": {"bucket": "A", "category": "agent-bail: custom"},
    }
    assert bail_mod.is_bucket_a(issue, "agent-bail: custom")
    assert not bail_mod.is_bucket_a(issue, "agent-bail: external")


def test_bail_report_fails_closed_on_timeout(
    bail_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("gh", 60)

    monkeypatch.setattr(bail_mod.subprocess, "run", timeout)
    with pytest.raises(SystemExit) as exc_info:
        bail_mod.run_gh(["issue", "list"])
    assert exc_info.value.code == 1


def test_bail_report_rejects_possible_truncation(
    bail_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bail_mod,
        "run_gh",
        lambda args: [
            {"labels": [], "updatedAt": "2026-01-01T00:00:00Z"}
            for _ in range(bail_mod.GH_LIST_LIMIT)
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        bail_mod.fetch_bailed(None)
    assert exc_info.value.code == 1


def test_bail_report_parses_latest_rca_stub(
    bail_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bail_mod,
        "run_gh",
        lambda args: {
            "comments": [
                {"body": "<!-- agent-loop-rca\nbucket: B\ncategory: old\n-->"},
                {
                    "body": "<!-- agent-loop-rca\nbucket: A\n"
                    "category: agent-bail: custom\n-->"
                },
            ]
        },
    )
    assert bail_mod.parse_rca_stub(1) == {
        "bucket": "A",
        "category": "agent-bail: custom",
    }


def test_bail_report_parses_rca_stub_null_safety(
    bail_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bail_mod,
        "run_gh",
        lambda args: {
            "comments": [
                {"body": None},
                {"body": "<!-- agent-loop-rca\nBucket: B\nCategory: stale\n-->"},
            ]
        },
    )
    assert bail_mod.parse_rca_stub(1) == {
        "bucket": "B",
        "category": "stale",
    }


def test_bail_report_is_bucket_a_normalization(bail_mod: ModuleType) -> None:
    # 1. Custom category without agent-bail: prefix in RCA stub matches label
    issue_custom = {"_rca": {"bucket": "A", "category": "custom-rule"}}
    assert bail_mod.is_bucket_a(issue_custom, "agent-bail: custom-rule") is True

    # 2. Explicit bucket B override on default bucket A category
    issue_override = {"_rca": {"bucket": "B", "category": "stale"}}
    assert bail_mod.is_bucket_a(issue_override, "agent-bail: stale") is False

    # 3. Fallback when no RCA stub exists
    issue_none = {"_rca": None}
    assert bail_mod.is_bucket_a(issue_none, "agent-bail: stale") is True
    assert bail_mod.is_bucket_a(issue_none, "agent-bail: unlisted") is False


def test_candidates_backfill_gaps_requires_grill_interview_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    # Issue with unrelated needs: triage label still lacks interview label
    issue_unrelated = {
        "labels": [{"name": "agent-bail: spec-gap"}, {"name": "needs: triage"}]
    }
    assert "needs" in mod.backfill_gaps(issue_unrelated)

    # Issue with needs: grill satisfies the interview requirement
    issue_grill = {
        "labels": [{"name": "agent-bail: spec-gap"}, {"name": "needs: grill"}]
    }
    assert "needs" not in mod.backfill_gaps(issue_grill)
