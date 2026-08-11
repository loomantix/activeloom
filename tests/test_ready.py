"""Unit coverage for the `/issues ready` query script.

Focus is the PR-addressed exclusion (`fetch_addressed_numbers`) and its
helpers — the GitHub-touching code is exercised by monkeypatching the
two subprocess wrappers so the tests stay hermetic.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest


@pytest.fixture(scope="session")
def ready_mod() -> ModuleType:
    """Load the Codex `/issues ready` script without changing shared fixtures."""
    path = (
        Path(__file__).resolve().parent.parent
        / ".codex"
        / "skills"
        / "issues"
        / "scripts"
        / "ready.py"
    )
    spec = importlib.util.spec_from_file_location("ready", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = ModuleType("ready")
    sys.modules["ready"] = module
    spec.loader.exec_module(module)
    return module


def _kw(mod: ModuleType, body: str) -> set[int]:
    return cast(set[int], mod.parse_closing_keywords(body))


def test_closing_keyword_regex_matches_all_keyword_forms(ready_mod: ModuleType) -> None:
    body = "Fixes #12, closes #34. Resolved #5; fix: #6; closed #7; resolves #8"
    assert _kw(ready_mod, body) == {12, 34, 5, 6, 7, 8}


def test_closing_keyword_regex_is_case_insensitive(ready_mod: ModuleType) -> None:
    assert _kw(ready_mod, "CLOSES #9 / Fix #10") == {9, 10}


def test_closing_keyword_regex_ignores_bare_mentions(ready_mod: ModuleType) -> None:
    # A plain reference is not a closing keyword — must not be excluded.
    assert _kw(ready_mod, "see #99, related to #100, part of #101") == set()


def test_closing_keyword_regex_respects_word_boundary(ready_mod: ModuleType) -> None:
    # "prefix" / "refixes" must not trip the fix/resolve stems.
    assert _kw(ready_mod, "prefix #11 and refixes #12") == set()


def test_closing_keyword_regex_does_not_cross_lines(ready_mod: ModuleType) -> None:
    assert _kw(ready_mod, "This fixes\n#42 is a separate discussion") == set()


def test_closing_keyword_fallback_ignores_non_prose_contexts(
    ready_mod: ModuleType,
) -> None:
    body = """
```markdown
Fixes #40
```
> Prior report said fixes #41
Inline example: `Fixes #42`
<!-- Fixes #43 -->
Actual directive: Fixes #44
"""
    assert _kw(ready_mod, body) == {44}


def test_blocker_regex_still_parses_dependencies(ready_mod: ModuleType) -> None:
    assert ready_mod.parse_blockers("Blocked by #3\n- Depends on #4") == {3, 4}


def test_hard_exclude_labels_cover_blocked_and_on_staging(ready_mod: ModuleType) -> None:
    assert ready_mod.HARD_EXCLUDE_LABELS == {"status: blocked", "status: on-staging"}


def test_is_hard_excluded_matches_either_state_in_any_position(ready_mod: ModuleType) -> None:
    assert ready_mod.is_hard_excluded(["status: blocked"])
    assert ready_mod.is_hard_excluded(["status: on-staging"])
    # Position-independent: the exclude label can sit among unrelated labels.
    assert ready_mod.is_hard_excluded(["area: backend", "status: on-staging", "dev: agent"])
    assert ready_mod.is_hard_excluded(["dev: agent", "agent-bail: spec-gap"])


def test_is_hard_excluded_ignores_actionable_and_lookalike_labels(ready_mod: ModuleType) -> None:
    assert not ready_mod.is_hard_excluded([])
    assert not ready_mod.is_hard_excluded(["dev: agent", "area: backend", "priority: high"])
    # Substring / prefix lookalikes must not trip the exact-match exclusion.
    assert not ready_mod.is_hard_excluded(["status: on-staging-soak", "status: unblocked"])


def test_ref_repo_extracts_owner_and_name(ready_mod: ModuleType) -> None:
    ref = {"number": 1, "repository": {"name": "platform", "owner": {"login": "acme"}}}
    assert ready_mod._ref_repo(ref) == "acme/platform"


def test_ref_repo_returns_none_when_incomplete(ready_mod: ModuleType) -> None:
    assert ready_mod._ref_repo({}) is None
    assert ready_mod._ref_repo({"repository": {"name": "x"}}) is None


def _ref(num: int, owner: str = "acme", name: str = "platform") -> dict[str, Any]:
    return {"number": num, "repository": {"name": name, "owner": {"login": owner}}}


def test_fetch_addressed_uses_closing_references_and_falls_back_to_keywords(
    ready_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_prs = [
        # Authoritative link set wins; body keywords on the same PR are ignored.
        {"number": 100, "body": "fixes #999", "closingIssuesReferences": [_ref(10)]},
        # Empty link set -> fall back to closing keywords in the body.
        {"number": 101, "body": "closes #20 and fixes #21", "closingIssuesReferences": []},
        # Cross-repo closing reference must NOT shadow a local issue #99.
        {"number": 102, "body": "", "closingIssuesReferences": [_ref(99, name="other")]},
        # Missing repository metadata is not safe to assume local.
        {"number": 103, "body": "", "closingIssuesReferences": [{"number": 98}]},
    ]
    merged_prs = [{"number": 200, "body": "", "closingIssuesReferences": [_ref(30)]}]

    calls: list[list[str]] = []

    def fake_pr_list(extra_args: list[str]) -> list[dict[str, Any]]:
        calls.append(extra_args)
        return open_prs if "open" in extra_args else merged_prs

    monkeypatch.setattr(ready_mod, "_current_repo", lambda: "acme/platform")
    monkeypatch.setattr(ready_mod, "_pr_list", fake_pr_list)

    assert ready_mod.fetch_addressed_numbers() == {10, 20, 21, 30}
    # Exactly two batched queries (open + merged) — no per-issue fan-out.
    assert len(calls) == 2
    assert calls[0] == ["--state", "open"]
    assert calls[1][:3] == ["--state", "merged", "--search"]
    assert calls[1][3].startswith("merged:>=")


def test_fetch_addressed_excludes_only_wrapper_captured_pr(
    ready_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    prs = [
        {"number": 100, "body": "closes #10", "closingIssuesReferences": []},
        {"number": 101, "body": "closes #10 and closes #11", "closingIssuesReferences": []},
    ]
    monkeypatch.setattr(ready_mod, "_current_repo", lambda: "acme/platform")
    monkeypatch.setattr(
        ready_mod,
        "_pr_list",
        lambda extra_args: prs if "open" in extra_args else [],
    )

    assert ready_mod.fetch_addressed_numbers(exclude_pr_numbers={100}) == {10, 11}
    assert ready_mod.fetch_addressed_numbers(exclude_pr_numbers={100, 101}) == set()


def test_fetch_addressed_fails_closed_on_api_error(
    ready_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(extra_args: list[str]) -> list[dict[str, Any]]:
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(ready_mod, "_current_repo", lambda: "acme/platform")
    monkeypatch.setattr(ready_mod, "_pr_list", boom)
    with pytest.raises(SystemExit) as exc_info:
        ready_mod.fetch_addressed_numbers()
    assert exc_info.value.code == 1


def test_fetch_addressed_fails_closed_when_repo_unknown(
    ready_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown_repo() -> str:
        raise RuntimeError("repo lookup failed")

    monkeypatch.setattr(ready_mod, "_current_repo", unknown_repo)
    with pytest.raises(SystemExit) as exc_info:
        ready_mod.fetch_addressed_numbers()
    assert exc_info.value.code == 1


def test_main_excludes_non_actionable_issues_without_hiding_unrelated(
    ready_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def issue(
        number: int,
        *,
        labels: list[str] | None = None,
        body: str = "",
    ) -> dict[str, Any]:
        return {
            "number": number,
            "title": f"Issue {number}",
            "body": body,
            "labels": [{"name": label} for label in (labels or [])],
            "assignees": [],
            "url": f"https://example.invalid/issues/{number}",
        }

    issues = [
        issue(1, labels=["priority: low"]),
        issue(2, labels=["status: on-staging", "dev: agent"]),
        issue(3, labels=["dev: agent"]),
        issue(4, labels=["status: blocked"]),
        issue(5, body="Blocked by #6"),
        issue(6, labels=["priority: medium"]),
        issue(7, labels=["dev: agent", "agent-bail: spec-gap"]),
        issue(8, labels=["priority: high"]),
    ]
    args = argparse.Namespace(
        mine=False,
        unassigned=False,
        agent=False,
        priority=None,
        area=None,
        limit=20,
        json=True,
    )
    monkeypatch.setattr(ready_mod, "parse_args", lambda: args)
    monkeypatch.setattr(ready_mod, "fetch_issues", lambda filters: issues)
    monkeypatch.setattr(ready_mod, "fetch_addressed_numbers", lambda: {3})

    assert ready_mod.main() == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["number"] for row in rows] == [8, 6, 1]
