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


# --- rubric settings shared by the scripts ---------------------------------

RUBRIC = REPO_ROOT / ".claude/skills/backlog-refinement/scripts/rubric.py"
PRECHECK = REPO_ROOT / ".claude/skills/backlog-refinement/scripts/precheck.py"
APPLY_PLAN = REPO_ROOT / ".claude/skills/backlog-refinement/scripts/apply-plan.py"

FULL_RUBRIC = (
    "## Settings\n\n"
    "- **Integration branch:** `staging`\n"
    "- **Rewrite mode:** `suggest` <!-- edit | suggest -->\n\n"
    f"<!-- priority-labels: {PRIORITIES} -->\n\n"
    "<!-- auto-managed-labels: -->\n\n"
    "<!-- priority-title-prefixes: [P0], [P1], [P2], [P3] -->\n\n"
    "<!-- stale-action: close -->\n"
)


def _load_rubric_config(tmp_path: Path, text: str) -> Any:
    path = tmp_path / ".backlog/refinement.local.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    mod = _load_script(f"rubric_{tmp_path.name}", RUBRIC)
    return mod, mod.load_config(str(tmp_path))


def test_rubric_reads_every_setting(tmp_path: Path) -> None:
    mod, config = _load_rubric_config(tmp_path, FULL_RUBRIC)
    assert config.integration_branch == "staging"
    assert config.rewrite_mode == "suggest"
    assert config.stale_action == "close"
    assert config.title_prefixes == ("[P0]", "[P1]", "[P2]", "[P3]")
    assert mod.title_priority("[P1][Ops] Rotate keys", config) == "priority: high"
    assert mod.title_priority("Rotate keys [P1]", config) is None


def test_rubric_defaults_are_conservative(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, config = _load_rubric_config(
        tmp_path,
        "- **Integration branch:** `TODO(backlog): the branch loop PRs target`\n"
        f"<!-- priority-labels: {PRIORITIES} -->\n",
    )
    assert config.integration_branch is None
    assert config.stale_action == "recommend"
    assert config.rewrite_mode == "edit"
    assert "no Rewrite mode setting" in capsys.readouterr().err
    assert config.title_prefixes == ()


def test_rubric_ignores_prefixes_that_do_not_match_the_labels(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, config = _load_rubric_config(
        tmp_path,
        f"<!-- priority-labels: {PRIORITIES} -->\n<!-- priority-title-prefixes: [P0], [P1] -->\n",
    )
    assert config.title_prefixes == ()
    assert "one prefix per label" in capsys.readouterr().err


def test_rubric_rejects_an_unknown_stale_action(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _load_rubric_config(tmp_path, "<!-- stale-action: delete -->\n")


def test_candidates_recognises_bracketed_epic_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    assert mod.classify(_issue(title="[Epic] Track A")) == "epic"
    assert mod.classify(_issue(title="Epic: Track B")) == "epic"


def test_candidates_decomposed_epic_owes_no_needs_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    epic = _issue("agent: refined", "agent-bail: epic", "priority: high")
    assert mod.needs_sub_issue_check(epic)
    assert mod.backfill_gaps(epic) == ["needs"]
    assert mod.backfill_gaps(epic, decomposed=True) == []
    # Decomposition excuses only the epic bail, never an open decision.
    decision = _issue("agent-bail: open-decision", "priority: high")
    assert not mod.needs_sub_issue_check(decision)
    assert mod.backfill_gaps(decision, decomposed=True) == ["needs"]


def test_candidates_suggests_priority_from_a_title_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch, rubric=FULL_RUBRIC)
    assert mod.suggested_priority(_issue(title="[P0][Security] Enable scanning")) == "priority: critical"
    # An existing label stands; the prefix suggests nothing.
    assert mod.suggested_priority(_issue("priority: low", title="[P0] Thing")) is None


def test_candidates_ranks_grill_queue_by_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    assert mod.priority_rank(_issue("priority: critical")) == 0
    assert mod.priority_rank(_issue("priority: low")) == 3
    assert mod.priority_rank(_issue()) == 4


# --- precheck ----------------------------------------------------------------


@pytest.fixture(scope="module")
def precheck_mod() -> ModuleType:
    return _load_script("precheck_tests", PRECHECK)


def test_precheck_extracts_same_repo_refs_only(precheck_mod: ModuleType) -> None:
    text = "See #12, #7 and #12 again; not owner/repo#99, not #5abc, and not itself #40."
    assert precheck_mod.extract_refs(text, 40) == [7, 12]


def test_precheck_extracts_blockers_from_blocked_by_lines(precheck_mod: ModuleType) -> None:
    text = "Related: #3\n- Blocked by #12 and #14\nBlocks #20"
    assert precheck_mod.extract_blockers(text, 1) == [12, 14]


def test_precheck_extracts_path_anchors(precheck_mod: ModuleType) -> None:
    text = (
        "`apps/api/src/a.ts:10` then `apps/api/src/a.ts:40-52`, "
        "`README.md`, `pnpm test`, and `docs/x.md`"
    )
    assert precheck_mod.extract_anchors(text) == [
        {"path": "apps/api/src/a.ts", "line": 52},
        {"path": "docs/x.md", "line": None},
    ]


def test_precheck_finds_merged_prs_that_never_closed_the_issue(precheck_mod: ModuleType) -> None:
    prs = [
        {"number": 1, "title": "fix: thing (#123)", "body": "", "closingIssuesReferences": []},
        {"number": 2, "title": "fix", "body": "Closes #123", "closingIssuesReferences": [{"number": 123}]},
        {"number": 3, "title": "fix #1234", "body": None, "closingIssuesReferences": []},
    ]
    assert [p["number"] for p in precheck_mod.unclosed_pr_mentions(prs, 123)] == [1]


def test_precheck_resolves_package_relative_paths(precheck_mod: ModuleType) -> None:
    tree = ["apps/api/src/auth/guard.ts", "apps/web/src/auth/guard.ts", "apps/api/src/main.ts", "README.md"]
    assert precheck_mod.resolve_path("README.md", tree) == ["README.md"]
    assert precheck_mod.resolve_path("./README.md", tree) == ["README.md"]
    assert precheck_mod.resolve_path("src/main.ts", tree) == ["apps/api/src/main.ts"]
    assert precheck_mod.resolve_path("./src/main.ts", tree) == ["apps/api/src/main.ts"]
    assert precheck_mod.resolve_path("auth/guard.ts", tree) == [
        "apps/api/src/auth/guard.ts", "apps/web/src/auth/guard.ts",
    ]
    assert precheck_mod.resolve_path("gone/file.ts", tree) == []


def test_precheck_keeps_resolved_references_past_a_not_found(
    precheck_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gh exits 1 on a partial NOT_FOUND but still prints the resolved nodes.
    out = json.dumps({
        "data": {"repository": {"n5": {"__typename": "Issue", "state": "CLOSED", "title": "t"}, "n333333": None}},
        "errors": [{"type": "NOT_FOUND"}],
    })
    monkeypatch.setattr(
        precheck_mod.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout=out, stderr="gh: not found"),
    )
    assert precheck_mod.reference_states("o", "r", [5, 333333]) == {
        "5": {"type": "Issue", "state": "CLOSED", "title": "t"},
    }

    monkeypatch.setattr(
        precheck_mod.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="HTTP 401"),
    )
    with pytest.raises(SystemExit):
        precheck_mod.reference_states("o", "r", [5])


def _facts(**overrides: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "sub_issues": {"total": 0, "open": [], "closed": []},
        "unclosed_pr_mentions": [],
        "blockers": [],
        "references": {},
        "anchors": [],
        "assignees": [],
        "title_priority": None,
    }
    facts.update(overrides)
    return facts


def test_precheck_hints_name_the_stale_shapes(precheck_mod: ModuleType) -> None:
    assert precheck_mod.hints_for(_facts()) == []
    hints = precheck_mod.hints_for(_facts(
        sub_issues={"total": 2, "open": [], "closed": [5, 6]},
        unclosed_pr_mentions=[{"number": 9, "title": "t", "merged_at": None}],
        blockers=[5],
        references={"5": {"type": "Issue", "state": "CLOSED", "title": "x"}},
        anchors=[
            {"path": "a/b.py", "line": None, "exists": False, "line_count": None},
            {"path": "a/c.py", "line": 90, "exists": True, "line_count": 40},
        ],
        assignees=["someone"],
        title_priority="priority: high",
    ))
    text = "\n".join(hints)
    for expected in ("All 2 sub-issues are closed", "#9", "blocker is closed", "`a/b.py` does not exist",
                     "`a/c.py:90` is past", "@someone", "priority: high"):
        assert expected in text


# --- apply-plan --------------------------------------------------------------


@pytest.fixture(scope="module")
def apply_mod() -> ModuleType:
    return _load_script("apply_plan_tests", APPLY_PLAN)


REPO_LABELS = {
    "dev: agent", "agent: refined", "agent-bail: stale", "agent-bail: epic",
    "agent-bail: open-decision", "agent-bail: sensitive-domain", "needs: grill",
    "needs: product-grill", "status: blocked", "release: ship", *PRIORITIES.split(", "),
}
PRIORITY_TUPLE = tuple(PRIORITIES.split(", "))


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "number": 7,
        "verdict": "exclude",
        "add_labels": ["agent-bail: open-decision", "needs: grill", "priority: medium"],
        "remove_labels": [],
        "comment": "Backlog refinement: excluded.",
        "body": None,
        "close_reason": None,
    }
    entry.update(overrides)
    return entry


def test_apply_plan_accepts_a_well_formed_plan(apply_mod: ModuleType) -> None:
    plan = {"issues": [
        _entry(),
        _entry(number=8, verdict="ready", add_labels=["dev: agent", "priority: low"], body="## Goal\n"),
        _entry(number=9, verdict="stale", add_labels=["agent-bail: stale"], close_reason="completed"),
        _entry(number=10, verdict="refined-only", add_labels=["priority: high"]),
    ]}
    assert apply_mod.validate(plan, REPO_LABELS, PRIORITY_TUPLE) == []


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (_entry(remove_labels=["none (no label present)"]), "does not exist"),
        (_entry(add_labels=None), "must be lists of label names"),
        (_entry(remove_labels=None), "must be lists of label names"),
        (_entry(verdict="ready", add_labels=["dev: agent"]), "needs the rewritten body"),
        (_entry(add_labels=["agent-bail: epic", "agent-bail: open-decision"]), "exactly one agent-bail"),
        (_entry(verdict="stale", add_labels=["agent-bail: stale"]), "close_reason"),
        (_entry(verdict="refined-only", add_labels=["needs: grill"]), "only accompanies"),
        (_entry(body="text"), "only for a ready verdict"),
        (_entry(comment="  "), "comment is required"),
        (_entry(add_labels=["agent-bail: epic", "priority: low", "priority: high"]), "more than one priority"),
        (_entry(add_labels=["agent-bail: epic", "dev: agent"]), "only a ready verdict"),
        (_entry(add_labels=["agent-bail: epic", "release: ship"]), "not one refinement sets"),
        (_entry(remove_labels=["release: ship"]), "not one refinement sets"),
        (_entry(verdict="ready", add_labels=["dev: agent", "status: blocked"], body="b"), "ready cannot carry"),
    ],
)
def test_apply_plan_refuses_unsafe_entries(apply_mod: ModuleType, entry: dict[str, Any], message: str) -> None:
    errors = apply_mod.validate({"issues": [entry]}, REPO_LABELS, PRIORITY_TUPLE)
    assert any(message in e for e in errors), errors


def test_apply_plan_refuses_a_repo_without_the_refined_label(apply_mod: ModuleType) -> None:
    errors = apply_mod.validate({"issues": [_entry()]}, REPO_LABELS - {"agent: refined"}, PRIORITY_TUPLE)
    assert any("'agent: refined' does not exist" in e for e in errors), errors


def test_apply_plan_refuses_duplicate_issues(apply_mod: ModuleType) -> None:
    errors = apply_mod.validate({"issues": [_entry(), _entry()]}, REPO_LABELS, PRIORITY_TUPLE)
    assert any("more than once" in e for e in errors)


def test_apply_plan_label_hygiene(apply_mod: ModuleType) -> None:
    ready = _entry(verdict="ready", add_labels=["dev: agent"], body="b")
    add, remove = apply_mod.label_changes(ready, {"agent-bail: spec-gap", "needs: grill", "status: blocked", "x"})
    assert add == ["dev: agent", "agent: refined"]
    assert remove == ["agent-bail: spec-gap", "needs: grill", "status: blocked"]

    # A new bail replaces the earlier one, and the ready label goes.
    add, remove = apply_mod.label_changes(_entry(), {"dev: agent", "agent-bail: epic", "agent: refined"})
    assert "agent: refined" not in add
    assert remove == ["agent-bail: epic", "dev: agent"]

    stale = _entry(verdict="stale", add_labels=["agent-bail: stale"], close_reason="completed")
    assert apply_mod.label_changes(stale, {"status: blocked"})[1] == ["status: blocked"]

    # A new bail without a needs: label drops the earlier assessment's needs:.
    remove = apply_mod.label_changes(stale, {"agent-bail: spec-gap", "needs: grill"})[1]
    assert remove == ["agent-bail: spec-gap", "needs: grill"]


def test_apply_plan_never_stacks_a_second_priority(apply_mod: ModuleType) -> None:
    add, _ = apply_mod.label_changes(_entry(), {"priority: low"}, PRIORITY_TUPLE)
    assert "priority: medium" not in add
    # Replacing the existing priority on purpose still works.
    add, remove = apply_mod.label_changes(
        _entry(remove_labels=["priority: low"]), {"priority: low"}, PRIORITY_TUPLE
    )
    assert "priority: medium" in add and "priority: low" in remove


class FakeGh:
    """Records gh calls and answers `issue view` from a fixed issue."""

    def __init__(self, state: str = "OPEN", labels: tuple[str, ...] = (), comments: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        self.issue = {
            "state": state,
            "labels": [{"name": n} for n in labels],
            "comments": [{"body": c} for c in comments],
        }

    def __call__(self, args: list[str], *, stdin: str | None = None) -> str:
        self.calls.append(args)
        return json.dumps(self.issue) if args[:2] == ["issue", "view"] else ""

    def verbs(self) -> list[str]:
        return [" ".join(c[:2]) for c in self.calls]


def _config(apply_mod: ModuleType, **overrides: Any) -> Any:
    return apply_mod.RubricConfig(None, (), PRIORITY_TUPLE)._replace(**overrides)


def test_apply_plan_closes_stale_only_when_the_repo_opts_in(
    apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = _entry(verdict="stale", add_labels=["agent-bail: stale"], close_reason="not planned")
    fake = FakeGh()
    monkeypatch.setattr(apply_mod, "gh", fake)
    assert "recommended" in apply_mod.apply_entry(stale, _config(apply_mod))
    assert "issue close" not in fake.verbs()

    fake = FakeGh()
    monkeypatch.setattr(apply_mod, "gh", fake)
    assert "closed as not planned" in apply_mod.apply_entry(stale, _config(apply_mod, stale_action="close"))
    assert fake.verbs()[-1] == "issue close"
    assert fake.calls[-1][-1] == "not planned"


def test_apply_plan_suggest_mode_posts_the_body_as_a_comment(
    apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGh()
    monkeypatch.setattr(apply_mod, "gh", fake)
    ready = _entry(verdict="ready", add_labels=["dev: agent"], body="## Goal\n")
    apply_mod.apply_entry(ready, _config(apply_mod, rewrite_mode="suggest"))
    assert not any("--body-file" in c and c[1] == "edit" for c in fake.calls)
    assert fake.verbs().count("issue comment") == 2


def test_apply_plan_edit_mode_replaces_the_body(
    apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGh()
    written: list[str] = []

    def gh(args: list[str], *, stdin: str | None = None) -> str:
        if args[:2] == ["issue", "edit"] and "--body-file" in args:
            written.append(Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8"))
        return fake(args, stdin=stdin)

    monkeypatch.setattr(apply_mod, "gh", gh)
    ready = _entry(verdict="ready", add_labels=["dev: agent"], body="## Goal\n\nShip it.\n\n")
    assert "body rewritten" in apply_mod.apply_entry(ready, _config(apply_mod))
    assert written == ["## Goal\n\nShip it.\n"]
    assert fake.verbs().count("issue comment") == 1


def test_apply_plan_skips_posted_comments_and_closed_issues(
    apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGh(comments=("Backlog refinement: excluded.",))
    monkeypatch.setattr(apply_mod, "gh", fake)
    apply_mod.apply_entry(_entry(), _config(apply_mod))
    assert "issue comment" not in fake.verbs()

    fake = FakeGh(state="CLOSED")
    monkeypatch.setattr(apply_mod, "gh", fake)
    assert apply_mod.apply_entry(_entry(), _config(apply_mod)).startswith("skipped")
    assert fake.verbs() == ["issue view"]


def test_apply_plan_records_progress(apply_mod: ModuleType, tmp_path: Path) -> None:
    path = str(tmp_path / "plan.json.applied.json")
    assert apply_mod.load_progress(path) == set()
    apply_mod.save_progress(path, {3, 1})
    assert apply_mod.load_progress(path) == {1, 3}


def _run_main(
    apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    plan: dict[str, Any], *flags: str,
) -> tuple[FakeGh, str]:
    fake = FakeGh()
    labels = json.dumps([{"name": n} for n in REPO_LABELS])

    def gh(args: list[str], *, stdin: str | None = None) -> str:
        return labels if args[:2] == ["label", "list"] else fake(args, stdin=stdin)

    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(apply_mod, "gh", gh)
    monkeypatch.setattr(apply_mod, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(apply_mod, "load_config", lambda root: _config(apply_mod))
    monkeypatch.setattr("sys.argv", ["apply-plan.py", str(path), *flags])
    return fake, str(path)


def test_apply_plan_main_mutates_only_a_valid_plan_with_apply(
    apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake, _ = _run_main(apply_mod, monkeypatch, tmp_path, {"issues": [_entry(add_labels=None)]}, "--apply")
    with pytest.raises(SystemExit):
        apply_mod.main()
    assert fake.calls == []

    plan = {"issues": [_entry(), _entry(number=8)]}
    fake, _ = _run_main(apply_mod, monkeypatch, tmp_path, plan)
    assert apply_mod.main() == 0
    assert fake.calls == []

    fake, path = _run_main(apply_mod, monkeypatch, tmp_path, plan, "--apply")
    apply_mod.save_progress(path + ".applied.json", {7})
    assert apply_mod.main() == 0
    assert {c[2] for c in fake.calls} == {"8"}
    assert apply_mod.load_progress(path + ".applied.json") == {7, 8}


# --- interview handback -------------------------------------------------------


def _decision(outcome: str = "settled", next_step: str = "refine", kind: str = "grill") -> str:
    return f"Confirmed decisions and their evidence.\n<!-- grill-decision kind: {kind}; outcome: {outcome}; next: {next_step} -->"


@pytest.mark.parametrize("text,kind,question", [
    ("Question for /grill: Which source?", "grill", "Which source?"),
    ("Question for /product-grill: Who needs this?", "product-grill", "Who needs this?"),
    ("**Question for the grill:** Which source?", "grill", "Which source?"),
    ("## Question for /product-grill\n\nWho needs this?", "product-grill", "Who needs this?"),
    ("Backlog refinement: excluded.\nWhich source?", "grill", "Which source?"),
])
def test_handback_question_shapes(text: str, kind: str, question: str) -> None:
    mod = _load_script("handback_rubric", RUBRIC)
    assert mod.parse_question(text, legacy_tail=True) == {"kind": kind, "question": question}


def test_handback_missing_question_and_quoted_examples() -> None:
    mod = _load_script("handback_rubric", RUBRIC)
    assert mod.parse_question("No interview question recorded.") is None
    assert mod.parse_question("> Question for /grill: Quoted?\n```\nQuestion for /grill: Example?\n```") is None
    assert mod.parse_decision(_decision()) == {"kind": "grill", "outcome": "settled", "next": "refine"}
    for invalid in ("```\n" + _decision() + "\n```", _decision() + "\ntrailing", _decision().replace("settled", "ready"),
                    _decision() + "\n" + _decision(), "> " + _decision().replace("\n", "\n> ")):
        assert mod.parse_decision(invalid) is None


@pytest.mark.parametrize("date,newer", [
    ("2026-01-02T12:00:01Z", True),
    ("2026-01-02T12:00:00Z", False),
    ("2026-01-01T12:00:00Z", False),
    ("2026-01-02T08:00:01-04:00", True),
    ("invalid", False),
    (None, False),
])
def test_handback_decision_ordering(date: str | None, newer: bool) -> None:
    mod = _load_script("handback_rubric", RUBRIC)
    comments = [
        {"body": _decision(), "createdAt": date},
        {"body": "Backlog refinement: excluded.\nQuestion for /grill: Which source?", "createdAt": "2026-01-02T12:00:00Z"},
    ]
    result = mod.grill_context("", comments)
    assert result['newer_decision'] is newer
    assert result['question']['question'] == "Which source?"
    assert result['decision']['outcome'] == 'settled'


def test_handback_new_refinement_supersedes_decision() -> None:
    mod = _load_script("handback_rubric", RUBRIC)
    result = mod.grill_context("## Question for the grill\nBody question?", [
        {"body": _decision(), "createdAt": "2026-01-02T12:00:00Z"},
        {"body": "Backlog refinement: a different blocker remains.", "createdAt": "2026-01-03T12:00:00Z"},
    ])
    assert not result['newer_decision']
    assert result['question']['question'] == 'Body question?'


@pytest.mark.parametrize('setting', ['ask', 'auto'])
def test_handback_local_setting(tmp_path: Path, setting: str) -> None:
    _, config = _load_rubric_config(tmp_path, FULL_RUBRIC + f'\n<!-- grill-handback: {setting} -->\n')
    assert config.grill_handback == setting
    _, default = _load_rubric_config(tmp_path, FULL_RUBRIC)
    assert default.grill_handback == 'ask'


@pytest.mark.parametrize('value', ['yes', 'ask, auto', ''])
def test_handback_invalid_setting_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(SystemExit):
        _load_rubric_config(tmp_path, FULL_RUBRIC + f'\n<!-- grill-handback: {value} -->\n')


def test_handback_grill_queue_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load_candidates(tmp_path, monkeypatch)
    issue = _issue('needs: grill', 'agent-bail: open-decision', 'priority: high')
    monkeypatch.setattr(mod, 'fetch_open_issues', lambda: [issue])
    monkeypatch.setattr('sys.argv', ['candidates.py', '--grill', '--json'])
    detail = {'body': '', 'comments': [
        {'body': 'Backlog refinement: excluded.\nQuestion for /grill: Which source?', 'createdAt': '2026-01-01T00:00:00Z'},
        {'body': _decision(), 'createdAt': '2026-01-02T00:00:00Z'},
    ]}
    calls = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(detail), '')

    monkeypatch.setattr(mod.subprocess, 'run', run)
    assert mod.main() == 0
    row = json.loads(capsys.readouterr().out)['grill_queues']['needs: grill'][0]
    assert row['question']['question'] == 'Which source?'
    assert row['newer_decision'] and row['decision']['next'] == 'refine'
    assert calls == [['gh', 'issue', 'view', '1', '--json', 'body,comments']]


def _handback_entry(outcome: str) -> dict[str, Any]:
    if outcome == 'ready':
        return _entry(verdict='ready', add_labels=['dev: agent'], body='## Goal\nConfirmed goal.\n\n## Acceptance criteria\n- Verified.', decision_comment=_decision())
    if outcome == 'permanent':
        return _entry(add_labels=['agent-bail: sensitive-domain'], decision_comment=_decision())
    if outcome in ('grill', 'product-grill'):
        return _entry(add_labels=['agent-bail: open-decision', f'needs: {outcome}'],
                      decision_comment=_decision(next_step=outcome, kind='product-grill' if outcome == 'grill' else 'grill'),
                      comment=f'Backlog refinement: another question.\nQuestion for /{outcome}: What remains?')
    if outcome == 'provisional':
        return _entry(decision_comment=_decision('provisional', 'grill'), comment='Backlog refinement: blocked on evidence.')
    if outcome == 'dont-build':
        return _entry(verdict='dont-build', add_labels=[], decision_comment=_decision('dont-build', 'none'), close_reason='not planned')
    child = _entry(verdict='ready', add_labels=['dev: agent'], body='## Goal\nBounded child.')
    child.pop('number')
    return _entry(add_labels=['agent-bail: epic'], decision_comment=_decision('split'),
                  children=[{'key': 'bounded-task', 'title': 'A bounded task', 'body': 'Initial child scope', 'assessment': child}])


@pytest.mark.parametrize('outcome', ['ready', 'permanent', 'grill', 'product-grill', 'provisional', 'dont-build', 'split'])
def test_handback_valid_outcomes(apply_mod: ModuleType, outcome: str) -> None:
    assert apply_mod.validate({'issues': [_handback_entry(outcome)]}, REPO_LABELS, PRIORITY_TUPLE) == []


@pytest.mark.parametrize('outcome', ['ready', 'permanent', 'grill', 'product-grill', 'provisional', 'dont-build'])
def test_handback_applies_outcomes(apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    entry = _handback_entry(outcome)
    old_needs = 'needs: product-grill' if outcome == 'grill' else 'needs: grill'
    fake = FakeGh(labels=('agent-bail: open-decision', old_needs), comments=(entry['decision_comment'],))
    monkeypatch.setattr(apply_mod, 'gh', fake)
    apply_mod.apply_entry(entry, _config(apply_mod), close_confirmed=outcome == 'dont-build')
    edits = [call for call in fake.calls if call[:2] == ['issue', 'edit']]
    add, remove = apply_mod.label_changes(entry, {'agent-bail: open-decision', old_needs})
    for label in add:
        assert any('--add-label' in call and label in call[call.index('--add-label')+1] for call in edits)
    for label in remove:
        assert any('--remove-label' in call and label in call[call.index('--remove-label')+1] for call in edits)
    assert ('issue close' in fake.verbs()) == (outcome == 'dont-build')
    if outcome == 'provisional':
        assert old_needs not in remove
    if outcome == 'permanent':
        assert old_needs in remove


@pytest.mark.parametrize('change', [
    {'decision_comment': 'No marker'},
    {'decision_comment': _decision('provisional'), 'verdict': 'ready', 'add_labels': ['dev: agent'], 'body': 'b'},
    {'decision_comment': _decision(next_step='product-grill')},
    {'decision_comment': _decision('split'), 'children': []},
    {'decision_comment': _decision('dont-build', 'none')},
    {'children': [{'key': '../unsafe', 'title': 'x', 'body': 'x', 'assessment': {}}]},
])
def test_handback_invalid_outcomes_refused(apply_mod: ModuleType, change: dict[str, Any]) -> None:
    assert apply_mod.validate({'issues': [_entry(**change)]}, REPO_LABELS, PRIORITY_TUPLE)


def test_handback_requires_record_before_mutation(apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGh()
    monkeypatch.setattr(apply_mod, 'gh', fake)
    with pytest.raises(SystemExit):
        apply_mod.apply_entry(_handback_entry('ready'), _config(apply_mod))
    assert fake.verbs() == ['issue view']


def test_handback_preserves_assignment_and_provisional_label(apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _handback_entry('provisional')
    fake = FakeGh(labels=('needs: product-grill',), comments=(entry['decision_comment'],))
    monkeypatch.setattr(apply_mod, 'gh', fake)
    with pytest.raises(SystemExit):
        apply_mod.apply_entry(entry, _config(apply_mod))
    assert fake.verbs() == ['issue view']
    fake.issue['assignees'] = [{'login': 'owner'}]
    with pytest.raises(SystemExit):
        apply_mod.apply_entry(entry, _config(apply_mod))
    assert fake.verbs() == ['issue view', 'issue view']


@pytest.mark.parametrize('auto,flags,allowed', [
    ('ask', [], False), ('auto', [], False),
    ('ask', ['--confirm-summary'], False),
    ('auto', ['--confirm-summary'], True),
    ('ask', ['--confirm-summary', '--confirm-handback'], True),
])
def test_handback_confirmation_gates(apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                    auto: str, flags: list[str], allowed: bool) -> None:
    entry = _handback_entry('permanent')
    fake, _ = _run_main(apply_mod, monkeypatch, tmp_path, {'issues': [entry]}, '--apply', *flags)
    fake.issue['comments'] = [{'body': entry['decision_comment']}]
    monkeypatch.setattr(apply_mod, 'load_config', lambda root: _config(apply_mod, grill_handback=auto))
    if allowed:
        assert apply_mod.main() == 0
        assert 'issue edit' in fake.verbs()
    else:
        with pytest.raises(SystemExit):
            apply_mod.main()
        assert not any(call[1] in ('edit', 'comment', 'close') for call in fake.calls)


def test_handback_auto_never_authorizes_close(apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entry = _handback_entry('dont-build')
    fake, _ = _run_main(apply_mod, monkeypatch, tmp_path, {'issues': [entry]}, '--apply', '--confirm-summary')
    monkeypatch.setattr(apply_mod, 'load_config', lambda root: _config(apply_mod, grill_handback='auto', stale_action='close'))
    with pytest.raises(SystemExit):
        apply_mod.main()
    assert fake.calls == []
    fake, _ = _run_main(apply_mod, monkeypatch, tmp_path, {'issues': [entry]}, '--apply', '--confirm-summary', '--confirm-handback', '--confirm-close', '7')
    fake.issue['comments'] = [{'body': entry['decision_comment']}]
    assert apply_mod.main() == 0
    assert fake.verbs()[-1] == 'issue close'


def test_handback_split_retries_created_child_after_link_failure(apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _handback_entry('split')
    children: list[dict[str, Any]] = []
    linked: list[dict[str, Any]] = []
    calls: list[list[str]] = []
    fail_link = True

    def gh(args: list[str], *, stdin: str | None = None) -> str:
        nonlocal fail_link
        calls.append(args)
        if args[:2] == ['issue', 'list']:
            return json.dumps(children)
        if args[0] == 'api' and args[1].endswith('/sub_issues'):
            if '--method' not in args:
                return json.dumps([linked])
            if fail_link:
                fail_link = False
                raise SystemExit('simulated link failure')
            linked.extend(children)
            return '{}'
        if args[0] == 'api' and args[1].endswith('/issues'):
            assert stdin is not None
            child = {'id': 80, 'number': 8, **json.loads(stdin)}
            children.append(child)
            return json.dumps(child)
        if args[0] == 'api':
            return json.dumps(children[0])
        if args[:2] == ['issue', 'view']:
            return json.dumps({'state': 'OPEN', 'labels': [], 'comments': [{'body': entry['decision_comment']}] if args[2]=='7' else []})
        if args[:2] == ['issue', 'edit'] and args[2]=='8' and '--body-file' in args:
            children[0]['body'] = Path(args[args.index('--body-file')+1]).read_text()
        return ''

    monkeypatch.setattr(apply_mod, 'gh', gh)
    with pytest.raises(SystemExit):
        apply_mod.apply_entry(entry, _config(apply_mod))
    assert len(children) == 1
    assert not any(c[:3] == ['issue', 'edit', '7'] for c in calls)
    apply_mod.apply_entry(entry, _config(apply_mod))
    apply_mod.apply_entry(entry, _config(apply_mod))
    assert len(children) == len(linked) == 1
    assert 'refinement-child parent: 7; key: bounded-task' in children[0]['body']
    assert any(c[:3] == ['issue', 'edit', '8'] for c in calls)
    assert any(c[:3] == ['issue', 'edit', '7'] for c in calls)


def test_handback_unconfirmed_standalone_split_refused(apply_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entry = _handback_entry('split')
    entry.pop('decision_comment')
    fake, _ = _run_main(apply_mod, monkeypatch, tmp_path, {'issues': [entry]}, '--apply')
    with pytest.raises(SystemExit):
        apply_mod.main()
    assert fake.calls == []
