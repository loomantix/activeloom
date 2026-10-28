"""Contract for the manual-tests plan helper (`.claude/skills/manual-tests/scripts/plan.py`)."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude/skills/manual-tests"


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan_mod = load(SKILL / "scripts/plan.py", "manual_tests_plan")
herd = load(ROOT / "prompts/skills/hreview/scripts/herd-config.py", "herd_config_mt")

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
E = "e" * 40
NOTE = "the tester typed this note"
REF = "run-7f3a"


def case(cid: str, change: str = "pr-12", **extra: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "change": change,
        "title": "Sign-in page redirects after login",
        "steps": ["Open the sign-in page.", "Sign in as the example user test-user-1."],
        "pass": "The dashboard opens.",
        "on_failure": "Browser console and the web server log.",
        "platform": "web",
        **extra,
    }


def pr_proposal(head: str = A, cases: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "repo": "acme/widgets",
        "scope": {"kind": "pr", "prs": [12]},
        "target": "local",
        "environment": {"status": "documented", "source": "AGENTS.md", "steps": ["Run the dev server."]},
        "install": None,
        "heads": {"integration": None, "production": None},
        "review": [{"pr": 12, "head": head, "converged": True}],
        "changes": [{"ref": "pr-12", "title": "Fix sign-in redirect", "head": head, "merged": False, "manual": True}],
        "cases": cases if cases is not None else [case("pr12-signin-redirect")],
        "promoted": None,
        **extra,
    }


def integration_proposal(head: str, changes: list[dict[str, Any]], cases: list[dict[str, Any]], promoted: Any = None) -> dict[str, Any]:
    return {
        "repo": "acme/widgets",
        "scope": {"kind": "integration"},
        "target": "integration",
        "environment": {"status": "not documented"},
        "heads": {"integration": head, "production": E},
        "changes": changes,
        "cases": cases,
        "promoted": promoted,
    }


def change(ref: str, head: str, manual: bool = True, reason: str | None = None) -> dict[str, Any]:
    return {"ref": ref, "title": f"Change {ref}", "head": head, "merged": True, "manual": manual, "reason": reason}


def merged(existing: dict[str, Any] | None, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    plan, report = plan_mod.merge(existing, plan_mod.validate_proposal(copy.deepcopy(raw)))
    return dict(plan), dict(report)


def result(cid: str, rev: int, basis: str, tested: str, status: str, at: str) -> dict[str, Any]:
    return {
        "case_id": cid,
        "case_rev": rev,
        "basis": basis,
        "tested_head": tested,
        "status": status,
        "ref": REF,
        "note": NOTE,
        "recorded_at": at,
    }


def by_id(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in plan["cases"]}


# --- scope keys and paths ------------------------------------------------------------


def test_scope_keys() -> None:
    assert plan_mod.scope_key("pr", [12]) == "pr-12"
    assert plan_mod.scope_key("prs", [30, 4, 30]) == "prs-4-30"
    assert plan_mod.scope_key("integration", []) == "integration"
    long = plan_mod.scope_key("prs", list(range(1000, 1030)))
    assert len(long) <= 64 and herd.KEY_RE.match(long)
    assert long != plan_mod.scope_key("prs", list(range(1000, 1031)))
    for kind, prs in (("pr", [1, 2]), ("prs", [3]), ("pr", [])):
        with pytest.raises(plan_mod.Invalid):
            plan_mod.scope_key(kind, prs)


def test_state_dir_is_the_one_horch_records_offers_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for key in ("pr-12", "prs-4-30", "integration"):
        assert str(plan_mod.state_dir("acme/widgets", key)) == herd.state_paths("acme/widgets", "manual-tests", key)["state"]


# --- proposal validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["changes"][0].update(manual=False, reason="Docs only."), "marked as needing no manual test"),
        (lambda p: p.update(cases=[]), "needs at least one case"),
        (lambda p: p["cases"][0].update(id="pr13-signin"), "does not belong to pr-12"),
        (lambda p: p["cases"][0].update(steps=[]), "steps: must be a non-empty list"),
        (lambda p: p["changes"][0].update(head="abc"), "full 40-character"),
        (lambda p: p.update(target="staging"), "target:"),
        (lambda p: p["changes"].append(change("pr-13", B)), "lists exactly pr-12"),
        (lambda p: p["scope"].update(prs=[True]), "positive PR numbers"),
        (lambda p: p["review"][0].update(pr=True), "review\\[0\\]: needs pr"),
        (lambda p: p["review"][0].update(pr=0), "review\\[0\\]: needs pr"),
        (lambda p: p["cases"][0].update(fix={"pr": 0, "merged": False}), "cases\\[0\\]\\.fix: needs pr"),
        (lambda p: p.update(heads=[]), "heads: must be an object"),
        (lambda p: p.update(review="bad"), "review: must be a list"),
        (lambda p: p.update(changes=None), "changes: must be a list"),
        (lambda p: p.update(cases=None), "cases: must be a list"),
    ],
)
def test_invalid_proposals_name_the_problem(mutate: Any, message: str) -> None:
    raw = pr_proposal()
    mutate(raw)
    with pytest.raises(plan_mod.Invalid, match=message):
        plan_mod.validate_proposal(raw)


def test_no_manual_test_needs_a_reason() -> None:
    raw = integration_proposal(B, [change("commit-aaaaaaa", A, manual=False)], [])
    with pytest.raises(plan_mod.Invalid, match="reason"):
        plan_mod.validate_proposal(raw)
    raw["changes"][0]["reason"] = "Covered by the unit suite; no user-facing change."
    assert plan_mod.validate_proposal(raw)["changes"][0]["manual"] is False


# --- identity and revisions ----------------------------------------------------------------


def test_first_merge_creates_revision_one_cases() -> None:
    plan, report = merged(None, pr_proposal())
    c = by_id(plan)["pr12-signin-redirect"]
    assert (c["rev"], c["lifecycle"], c["state"], c["group"], c["basis"]) == (1, "active", "not run", "todo", A)
    assert report["added"] == ["pr12-signin-redirect"]
    assert plan["scope"]["title"] == "Manual tests · acme/widgets · pr-12"


def test_rewording_the_title_keeps_the_revision_but_new_steps_bump_it() -> None:
    plan, _ = merged(None, pr_proposal())
    plan, report = merged(plan, pr_proposal(cases=[case("pr12-signin-redirect", title="Reworded title")]))
    assert by_id(plan)["pr12-signin-redirect"]["rev"] == 1 and report["revised"] == []
    plan, report = merged(plan, pr_proposal(cases=[case("pr12-signin-redirect", steps=["A new step."])]))
    assert by_id(plan)["pr12-signin-redirect"]["rev"] == 2 and report["revised"] == ["pr12-signin-redirect"]


def test_a_dropped_case_is_retired_with_its_result_never_deleted() -> None:
    plan, _ = merged(None, pr_proposal(cases=[case("pr12-signin-redirect"), case("pr12-logout")]))
    plan, _ = plan_mod.apply_results(plan, [result("pr12-logout", 1, A, A, "passed", "2026-10-01T10:00:00Z")])
    plan, report = merged(plan, pr_proposal())
    logout = by_id(plan)["pr12-logout"]
    assert logout["lifecycle"] == "retired" and logout["group"] == "retired"
    assert logout["latest"]["status"] == "passed" and logout["attempts"] == 1
    assert report["retired"] == ["pr12-logout"]
    plan, report = merged(plan, pr_proposal(cases=[case("pr12-signin-redirect"), case("pr12-logout")]))
    assert by_id(plan)["pr12-logout"]["lifecycle"] == "active" and report["reactivated"] == ["pr12-logout"]


def test_an_id_that_moves_to_another_change_stops() -> None:
    changes = [change("pr-12", A)]
    plan, _ = merged(None, integration_proposal(C, changes, [case("pr12-signin")]))
    stored = copy.deepcopy(plan)
    stored["cases"][0]["change"] = "pr-13"
    with pytest.raises(plan_mod.Stop, match="belongs to pr-13"):
        merged(stored, integration_proposal(C, changes, [case("pr12-signin")]))


def test_a_plan_for_another_scope_stops() -> None:
    plan, _ = merged(None, pr_proposal())
    other = pr_proposal(scope={"kind": "pr", "prs": [13]})
    other["changes"][0]["ref"] = "pr-13"
    other["cases"] = [case("pr13-signin", change="pr-13")]
    with pytest.raises(plan_mod.Stop, match="pr-12"):
        merged(plan, other)


# --- results and staleness -------------------------------------------------------------------


def test_two_saves_to_one_case_are_two_attempts_and_notes_never_reach_the_plan() -> None:
    plan, _ = merged(None, pr_proposal())
    listing = {
        "documents": [
            {"id": "r1", "data": result("pr12-signin-redirect", 1, A, A, "failed", "2026-10-01T10:00:00Z")},
            {"id": "r2", "data": result("pr12-signin-redirect", 1, A, A, "passed", "2026-10-01T11:00:00Z")},
            {"id": "r3", "data": {"case_id": "pr12-signin-redirect", "status": "passed"}},
        ]
    }
    plan, counts = plan_mod.apply_results(plan, listing)
    c = by_id(plan)["pr12-signin-redirect"]
    assert counts == {"read": 2, "ignored": 1, "unknown_case": 0}
    assert c["attempts"] == 2 and c["state"] == "passed" and c["group"] == "done"
    text = json.dumps(plan)
    assert NOTE not in text and REF not in text
    assert set(c["latest"]) == {"status", "rev", "basis", "tested_head", "recorded_at"}


def test_a_revised_case_or_moved_head_makes_results_stale() -> None:
    plan, _ = merged(None, pr_proposal())
    plan, _ = plan_mod.apply_results(plan, [result("pr12-signin-redirect", 1, A, A, "passed", "2026-10-01T10:00:00Z")])
    revised, _ = merged(plan, pr_proposal(cases=[case("pr12-signin-redirect", steps=["Changed."])]))
    assert by_id(revised)["pr12-signin-redirect"]["state"] == "stale"
    moved, _ = merged(plan, pr_proposal(head=B))
    assert by_id(moved)["pr12-signin-redirect"]["state"] == "stale"
    # A stale tab that saves against the old revision still counts as stale.
    again, _ = plan_mod.apply_results(revised, [result("pr12-signin-redirect", 1, A, A, "passed", "2026-10-02T10:00:00Z")])
    assert by_id(again)["pr12-signin-redirect"]["state"] == "stale"


def test_an_unrelated_merge_carries_a_pass_over_on_the_integration_page() -> None:
    first = [change("pr-12", A)]
    plan, _ = merged(None, integration_proposal(C, first, [case("pr12-signin")]))
    plan, _ = plan_mod.apply_results(plan, [result("pr12-signin", 1, A, C, "passed", "2026-10-01T10:00:00Z")])
    assert by_id(plan)["pr12-signin"]["group"] == "done"
    later = first + [change("pr-13", B)]
    plan, report = merged(plan, integration_proposal(D, later, [case("pr12-signin"), case("pr13-upload", change="pr-13")]))
    cases = by_id(plan)
    assert (cases["pr12-signin"]["state"], cases["pr12-signin"]["group"]) == ("passed", "carried")
    assert cases["pr13-upload"]["group"] == "todo"
    assert report["changes_added"] == ["pr-13"]


def test_a_known_failure_waits_on_its_fix_then_needs_a_retest() -> None:
    changes = [change("pr-12", A)]
    pending = case("pr12-signin", fix={"pr": 15, "merged": False})
    plan, _ = merged(None, integration_proposal(C, changes, [pending]))
    plan, _ = plan_mod.apply_results(plan, [result("pr12-signin", 1, A, C, "failed", "2026-10-01T10:00:00Z")])
    assert (by_id(plan)["pr12-signin"]["state"], by_id(plan)["pr12-signin"]["group"]) == ("fix pending", "pending")
    fixed = case("pr12-signin", fix={"pr": 15, "merged": True, "head": B})
    plan, _ = merged(plan, integration_proposal(D, changes, [fixed]))
    c = by_id(plan)["pr12-signin"]
    assert c["basis"] == f"{A}+fix:{B}" and (c["state"], c["group"]) == ("stale", "todo")


def test_promoted_changes_fold_into_a_promotion_with_results_kept() -> None:
    changes = [change("pr-12", A), change("pr-13", B)]
    plan, _ = merged(None, integration_proposal(C, changes, [case("pr12-signin"), case("pr13-upload", change="pr-13")]))
    plan, _ = plan_mod.apply_results(plan, [result("pr12-signin", 1, A, C, "failed", "2026-10-01T10:00:00Z")])
    promoted = {"date": "2026-10-02", "production_head": D, "changes": ["pr-12"]}
    plan, report = merged(plan, integration_proposal(C, changes[1:], [case("pr13-upload", change="pr-13")], promoted))
    c = by_id(plan)["pr12-signin"]
    assert (c["lifecycle"], c["promotion"], c["latest"]["status"]) == ("promoted", 0, "failed")
    assert report["promoted"] == ["pr12-signin"] and report["changes_removed"] == []
    assert plan["promotions"] == [{"date": "2026-10-02", "production_head": D, "changes": ["pr-12"]}]
    # A later run must not re-report already-promoted pr-12 in changes_removed.
    plan, later_report = merged(plan, integration_proposal(C, [], []))
    assert later_report["changes_removed"] == ["pr-13"]
    # And when pr-13 returns to scope, it is reported in changes_added again.
    _, return_report = merged(plan, integration_proposal(C, changes[1:], [case("pr13-upload", change="pr-13")]))
    assert return_report["changes_added"] == ["pr-13"] and return_report["reactivated"] == ["pr13-upload"]


# --- summary ---------------------------------------------------------------------------------


def test_summary_is_counts_ids_and_heads_only() -> None:
    plan, _ = merged(None, pr_proposal(cases=[case("pr12-signin-redirect"), case("pr12-logout")]))
    plan["review"][0]["converged"] = False
    plan, _ = plan_mod.apply_results(plan, [result("pr12-logout", 1, A, A, "failed", "2026-10-01T10:00:00Z")])
    s = plan_mod.summarize(plan)
    assert s["counts"] == {"failed": 1, "not run": 1}
    assert s["failed"] == ["pr12-logout@aaaaaaa"]
    assert s["not_converged"] == ["pr-12@aaaaaaa"]
    text = plan_mod.summary_text(s)
    assert NOTE not in text and REF not in text and "pr12-logout@aaaaaaa" in text


# --- render ----------------------------------------------------------------------------------


def test_render_embeds_the_plan_inline_and_escapes_it() -> None:
    plan, _ = merged(None, pr_proposal(cases=[case("pr12-signin-redirect", title="Close </script><b>x</b> & go")]))
    template = (SKILL / "assets/page.html").read_text()
    tokens = (ROOT / ".claude/references/house-tokens.css").read_text()
    page = plan_mod.render(plan, template, tokens)
    assert "@@" not in page
    assert "<title>Manual tests · acme/widgets · pr-12</title>" in page[:8192]
    assert "--accent:" in page and "<link" not in page and "@import" not in page
    assert not re.search(r"https?://", page)
    body = re.search(r'<script\s+type="application/json"\s+id="plan-data"\s*>(.*?)</script>', page, re.S)
    assert body and json.loads(body.group(1)) == plan
    assert page.count("</script>") == template.count("</script>")


def test_plan_matches_the_portable_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SKILL / "scripts/plan.schema.json").read_text())
    plan, _ = merged(None, pr_proposal())
    plan, _ = plan_mod.apply_results(plan, [result("pr12-signin-redirect", 1, A, A, "passed", "2026-10-01T10:00:00Z")])
    jsonschema.Draft202012Validator(schema).validate(plan)


# --- CLI -------------------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps(pr_proposal()))
    plan = tmp_path / "scope" / "plan.json"
    assert plan_mod.main(["merge", "--plan", str(plan), "--proposal", str(proposal)]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == ["pr12-signin-redirect"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(pr_proposal(target="nowhere")))
    assert plan_mod.main(["merge", "--plan", str(plan), "--proposal", str(bad)]) == 2
    plan.write_text("{not json")
    assert plan_mod.main(["merge", "--plan", str(plan), "--proposal", str(proposal)]) == 3
    assert plan_mod.main(["summary", "--plan", str(tmp_path / "missing.json")]) == 3


def test_lock_is_exclusive(tmp_path: Path) -> None:
    assert plan_mod.main(["lock", "--dir", str(tmp_path)]) == 0
    assert plan_mod.main(["lock", "--dir", str(tmp_path)]) == 3
    assert plan_mod.main(["unlock", "--dir", str(tmp_path)]) == 0
    assert plan_mod.main(["lock", "--dir", str(tmp_path)]) == 0


def test_cli_render_uses_the_shipped_template_and_tokens(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan, _ = merged(None, pr_proposal())
    plan_mod.write_json(plan_path, plan)
    out = tmp_path / "page.html"
    assert plan_mod.main(["render", "--plan", str(plan_path), "--out", str(out)]) == 0
    assert "pr12-signin-redirect" in out.read_text()


def test_cli_reads_results_saved_one_file_per_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path = tmp_path / "plan.json"
    plan, _ = merged(None, pr_proposal())
    plan_mod.write_json(plan_path, plan)
    docs = tmp_path / "listing" / "results"
    docs.mkdir(parents=True)
    for name, status, at in (("r1", "failed", "2026-10-01T10:00:00Z"), ("r2", "passed", "2026-10-01T11:00:00Z")):
        (docs / f"{name}.json").write_text(json.dumps(result("pr12-signin-redirect", 1, A, A, status, at)))
    assert plan_mod.main(["apply-results", "--plan", str(plan_path), "--results", str(tmp_path / "listing")]) == 0
    assert json.loads(capsys.readouterr().out)["read"] == 2
    stored = json.loads(plan_path.read_text())
    assert by_id(stored)["pr12-signin-redirect"]["state"] == "passed"
    assert NOTE not in plan_path.read_text()
