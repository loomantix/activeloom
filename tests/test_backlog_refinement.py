"""Behavioral coverage for the backlog-refinement helper scripts."""
from __future__ import annotations

import argparse
import json
import shutil
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


def _load_candidates(tmp_path: Path, marker: str = "") -> ModuleType:
    skill = tmp_path / "backlog-refinement"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "candidates.py"
    shutil.copy2(CANDIDATES, target)
    (skill / "RUBRIC.md").write_text(
        f"# Rubric\n\n<!-- auto-managed-labels: {marker} -->\n",
        encoding="utf-8",
    )
    return _load_script(f"candidates_{tmp_path.name}", target)


def _issue(*labels: str, title: str = "Task") -> dict[str, Any]:
    return {
        "number": 1,
        "title": title,
        "labels": [{"name": label} for label in labels],
        "assignees": [],
    }


def test_candidates_bail_label_wins_over_stale_ready_label(tmp_path: Path) -> None:
    mod = _load_candidates(tmp_path)
    assert mod.classify(_issue("dev: agent", "agent-bail: spec-gap")) == "excluded"


def test_candidates_auto_managed_marker_is_consumer_owned(tmp_path: Path) -> None:
    mod = _load_candidates(tmp_path, "nightly-digest, automated-report")
    assert mod.AUTO_MANAGED_LABELS == ("nightly-digest", "automated-report")
    assert mod.classify(_issue("nightly-digest")) == "skipped"
    assert mod.classify(_issue("unrelated")) == "unrefined"


def test_candidates_absent_marker_disables_skipping(tmp_path: Path) -> None:
    """A RUBRIC.md without the marker must not crash (documented safe default).

    RUBRIC.md is consumer-owned (`create_if_missing`), so a pre-existing consumer
    whose copy predates the marker has no marker at all. That is the "hasn't opted
    in" state and must degrade to no auto-managed skipping, never a hard exit.
    """
    skill = tmp_path / "backlog-refinement"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "candidates.py"
    shutil.copy2(CANDIDATES, target)
    (skill / "RUBRIC.md").write_text("# no marker\n", encoding="utf-8")
    mod = _load_script("candidates_no_marker", target)
    assert mod.AUTO_MANAGED_LABELS == ()
    assert mod.classify(_issue("anything")) == "unrefined"


def test_candidates_falls_back_to_template_when_rubric_md_absent(
    tmp_path: Path,
) -> None:
    """When RUBRIC.md is absent (e.g. global installation), fall back to RUBRIC.md.template."""
    skill = tmp_path / "backlog-refinement"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "candidates.py"
    shutil.copy2(CANDIDATES, target)
    (skill / "RUBRIC.md.template").write_text(
        "# Template Rubric\n\n<!-- auto-managed-labels: template-skip -->\n",
        encoding="utf-8",
    )
    mod = _load_script("candidates_template_fallback", target)
    assert mod.AUTO_MANAGED_LABELS == ("template-skip",)
    assert mod.classify(_issue("template-skip")) == "skipped"
    assert mod.classify(_issue("other")) == "unrefined"


def test_candidates_rejects_duplicate_marker(tmp_path: Path) -> None:
    """More than one marker is ambiguous — fail closed rather than guess."""
    skill = tmp_path / "backlog-refinement"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "candidates.py"
    shutil.copy2(CANDIDATES, target)
    (skill / "RUBRIC.md").write_text(
        "<!-- auto-managed-labels: one -->\n<!-- auto-managed-labels: two -->\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc_info:
        _load_script("invalid_candidates_dup", target)
    assert exc_info.value.code == 1


def test_candidates_required_query_fails_closed_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path)

    def timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("gh", 60)

    monkeypatch.setattr(mod.subprocess, "run", timeout)
    with pytest.raises(SystemExit) as exc_info:
        mod.fetch_open_issues()
    assert exc_info.value.code == 1


def test_candidates_rejects_invalid_json_and_possible_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path)
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


def test_candidates_limit_rejects_negative_values(tmp_path: Path) -> None:
    mod = _load_candidates(tmp_path)
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
    assert bail_mod.is_bucket_a({"_rca": {"bucket": "A"}}, "agent-bail: custom")
    assert not bail_mod.is_bucket_a(
        {"_rca": {"bucket": "B"}}, "agent-bail: stale"
    )
    assert bail_mod.is_bucket_a({"_rca": None}, "agent-bail: stale")


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
