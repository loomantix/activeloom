"""Behavioral coverage for the backlog-refinement helper scripts."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = REPO_ROOT / ".codex/skills/backlog-refinement/scripts/candidates.py"
BAIL_REPORT = REPO_ROOT / ".codex/skills/backlog-refinement/scripts/bail-report.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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
    # Both files present is the steady state of a bootstrapped consumer: the
    # rendered RUBRIC.md sits beside the synced template. Write a template with
    # a conflicting marker so every test using this fixture also pins that
    # RUBRIC.md wins — inverting the guard would otherwise go unnoticed.
    (skill / "RUBRIC.md.template").write_text(
        "# Template Rubric\n\n<!-- auto-managed-labels: template-should-not-win -->\n",
        encoding="utf-8",
    )
    return _load(target, f"candidates_{tmp_path.name}")


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
    mod = _load(target, "candidates_no_marker")
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
    mod = _load(target, "candidates_template_fallback")
    assert mod.AUTO_MANAGED_LABELS == ("template-skip",)
    assert mod.classify(_issue("template-skip")) == "skipped"
    assert mod.classify(_issue("other")) == "unrefined"


def test_candidates_prefers_rubric_md_over_template_when_both_present(
    tmp_path: Path,
) -> None:
    """The steady state of a bootstrapped consumer: RUBRIC.md must win.

    Inverting the guard would silently replace the repo's configured skip list
    with the template's, which ships empty — auto-managed issues would stop
    being skipped and the script would still exit 0.
    """
    mod = _load_candidates(tmp_path, "repo-configured-skip")
    assert mod.AUTO_MANAGED_LABELS == ("repo-configured-skip",)
    assert mod.classify(_issue("repo-configured-skip")) == "skipped"
    assert mod.classify(_issue("template-should-not-win")) == "unrefined"


def test_candidates_exits_when_neither_rubric_nor_template_exists(
    tmp_path: Path,
) -> None:
    """The fallback must stay fail-closed when there is nothing to fall back to."""
    skill = tmp_path / "backlog-refinement"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "candidates.py"
    shutil.copy2(CANDIDATES, target)
    with pytest.raises(SystemExit) as excinfo:
        _load(target, "candidates_no_rubric_at_all")
    assert excinfo.value.code == 1


def test_candidates_announces_template_fallback_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A degraded run must be distinguishable from a configured one."""
    skill = tmp_path / "backlog-refinement"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "candidates.py"
    shutil.copy2(CANDIDATES, target)
    (skill / "RUBRIC.md.template").write_text(
        "# Template Rubric\n\n<!-- auto-managed-labels: -->\n", encoding="utf-8"
    )
    mod = _load(target, "candidates_fallback_notice")
    assert mod.AUTO_MANAGED_LABELS == ()
    err = capsys.readouterr().err
    assert "RUBRIC.md.template" in err
    assert "NOT applied" in err


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
        _load(target, "invalid_candidates_dup")
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
    return _load(BAIL_REPORT, "bail_report_tests")


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
