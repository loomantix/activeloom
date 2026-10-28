#!/usr/bin/env python3
"""Manual test plan helper for the manual-tests skill.

The plan is a portable JSON file (`plan.json`, schema `plan.schema.json`): the
changes in scope, the manual cases with stable IDs and revisions, and the
latest applicable result per case. Any front-end can render it; the Claude
Code front-end embeds it in an Artifact page whose database holds every result
submission. This helper owns the rules that must not drift between runs:

- case IDs are stable and never reused for another change;
- a case whose steps, pass criteria, failure pointer or platform change gets a
  new revision;
- a case leaves the page only by being retired or promoted, never deleted;
- a result applies only to the case revision and basis (the change's head, plus
  a merged fix's head) it was recorded against;
- only allowlisted result fields reach the plan; notes and references stay in
  the page's database.

Commands print JSON (or text for `summary`). Exit codes: 0 ok, 2 invalid input,
3 stop and ask the user (unreadable or conflicting existing state, a held lock).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "activeloom.manual-tests.plan/v1"
STATUSES = ("passed", "failed", "blocked", "skipped")
TARGETS = ("local", "integration")
PLATFORMS = ("web", "android", "ios", "desktop", "cli", "api", "any")
KINDS = ("pr", "prs", "integration")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
CHANGE_RE = re.compile(r"^(?:pr-(?P<pr>[1-9][0-9]{0,8})|commit-(?P<sha>[0-9a-f]{7}))$")
CASE_RE = re.compile(r"^(?:pr[1-9][0-9]{0,8}|commit-[0-9a-f]{7})-[a-z0-9][a-z0-9-]{0,47}$")
KEY_MAX = 64
TEXT_MAX = 2000
FINGERPRINT_FIELDS = ("steps", "pass", "on_failure", "platform")


class Invalid(Exception):
    """Input breaks the plan contract (exit 2)."""


class Stop(Exception):
    """Existing state is unreadable or conflicts; the user decides (exit 3)."""


# --- scope keys and paths -----------------------------------------------------


def _pos_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def scope_key(kind: str, prs: list[int]) -> str:
    if not isinstance(prs, list) or any(not _pos_int(n) for n in prs):
        raise Invalid("a pr or prs scope needs positive PR numbers")
    if kind == "integration":
        return "integration"
    if not prs:
        raise Invalid("a pr or prs scope needs positive PR numbers")
    if kind == "pr":
        if len(prs) != 1:
            raise Invalid("a pr scope names exactly one PR")
        return f"pr-{prs[0]}"
    if kind == "prs":
        numbers = sorted(set(prs))
        if len(numbers) < 2:
            raise Invalid("a prs scope names at least two PRs")
        key = "prs-" + "-".join(str(n) for n in numbers)
        if len(key) > KEY_MAX:
            digest = hashlib.sha256(key.encode()).hexdigest()[:8]
            key = key[: KEY_MAX - 9].rstrip("-") + "-" + digest
        return key
    raise Invalid(f"scope kind must be one of {', '.join(KINDS)}")


def state_dir(repo: str, key: str) -> Path:
    if not REPO_RE.match(repo) or ".." in repo.split("/"):
        raise Invalid(f"repository must be owner/name, got {repo!r}")
    owner, name = repo.split("/")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "activeloom-herd" / f"{owner}__{name}" / "manual-tests" / key


def artifact_title(repo: str, key: str) -> str:
    return f"Manual tests · {repo} · {key}"


# --- small validators ----------------------------------------------------------


def _text(value: Any, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > TEXT_MAX or "\x00" in value:
        raise Invalid(f"{where}: must be a non-empty string under {TEXT_MAX} characters")
    return value


def _sha(value: Any, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not SHA_RE.match(value):
        raise Invalid(f"{where}: must be a full 40-character commit SHA")
    return value


def _steps(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise Invalid(f"{where}: must be a non-empty list of strings")
    return [_text(step, f"{where}[{i}]") for i, step in enumerate(value)]  # type: ignore[misc]


def _env(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Invalid(f"{where}: must be an object")
    status = value.get("status")
    if status == "not documented":
        return {"status": status, "source": None, "steps": []}
    if status != "documented":
        raise Invalid(f"{where}.status: must be 'documented' or 'not documented'")
    return {
        "status": status,
        "source": _text(value.get("source"), f"{where}.source"),
        "steps": _steps(value.get("steps"), f"{where}.steps"),
    }


def _case_prefix(change_ref: str) -> str:
    match = CHANGE_RE.match(change_ref)
    assert match
    return f"pr{match['pr']}-" if match["pr"] else f"commit-{match['sha']}-"


def _fingerprint(case: dict[str, Any]) -> str:
    body = json.dumps({k: case[k] for k in FINGERPRINT_FIELDS}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- proposal ---------------------------------------------------------------------


def validate_proposal(raw: Any) -> dict[str, Any]:
    """Check what the session proposes for this run; return it normalized."""
    if not isinstance(raw, dict):
        raise Invalid("proposal: must be a JSON object")
    repo = raw.get("repo")
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        raise Invalid("repo: must be owner/name")
    scope = raw.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") not in KINDS:
        raise Invalid(f"scope.kind: must be one of {', '.join(KINDS)}")
    prs = scope.get("prs", [])
    key = scope_key(scope["kind"], prs)
    target = raw.get("target")
    if target not in TARGETS:
        raise Invalid(f"target: must be one of {', '.join(TARGETS)}")
    heads = raw.get("heads")
    if heads is None:
        heads = {}
    if not isinstance(heads, dict):
        raise Invalid("heads: must be an object")
    heads = {
        "integration": _sha(heads.get("integration"), "heads.integration", optional=True),
        "production": _sha(heads.get("production"), "heads.production", optional=True),
    }
    if scope["kind"] == "integration" and not heads["integration"]:
        raise Invalid("heads.integration: required for the integration scope")

    review_raw = raw.get("review")
    if review_raw is None:
        review_raw = []
    if not isinstance(review_raw, list):
        raise Invalid("review: must be a list")
    review = []
    for i, entry in enumerate(review_raw):
        if not isinstance(entry, dict) or not _pos_int(entry.get("pr")):
            raise Invalid(f"review[{i}]: needs pr, head and converged")
        if not isinstance(entry.get("converged"), bool):
            raise Invalid(f"review[{i}].converged: must be true or false")
        review.append({"pr": entry["pr"], "head": _sha(entry.get("head"), f"review[{i}].head"), "converged": entry["converged"]})

    changes_raw = raw.get("changes")
    if not isinstance(changes_raw, list):
        raise Invalid("changes: must be a list")
    changes: dict[str, dict[str, Any]] = {}
    for i, change in enumerate(changes_raw):
        where = f"changes[{i}]"
        if not isinstance(change, dict) or not isinstance(change.get("ref"), str) or not CHANGE_RE.match(change["ref"]):
            raise Invalid(f"{where}.ref: must be pr-<n> or commit-<sha7>")
        ref = change["ref"]
        if ref in changes:
            raise Invalid(f"{where}.ref: {ref} listed twice")
        manual = change.get("manual")
        if not isinstance(manual, bool):
            raise Invalid(f"{where}.manual: must be true or false")
        head = _sha(change.get("head"), f"{where}.head")
        if ref.startswith("commit-") and not head.startswith(ref[7:]):  # type: ignore[union-attr]
            raise Invalid(f"{where}.head: does not match {ref}")
        changes[ref] = {
            "ref": ref,
            "title": _text(change.get("title"), f"{where}.title"),
            "head": head,
            "merged": bool(change.get("merged", False)),
            "manual": manual,
            "reason": None if manual else _text(change.get("reason"), f"{where}.reason"),
        }
    if scope["kind"] != "integration":
        expected = {f"pr-{n}" for n in prs}
        if set(changes) != expected:
            raise Invalid(f"changes: a {scope['kind']} scope lists exactly {', '.join(sorted(expected))}")

    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list):
        raise Invalid("cases: must be a list")
    cases: dict[str, dict[str, Any]] = {}
    for i, case in enumerate(cases_raw):
        where = f"cases[{i}]"
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not CASE_RE.match(case["id"]):
            raise Invalid(f"{where}.id: must be pr<n>-<slug> or commit-<sha7>-<slug>")
        cid = case["id"]
        if cid in cases:
            raise Invalid(f"{where}.id: {cid} listed twice")
        ref = case.get("change")
        if ref not in changes:
            raise Invalid(f"{where}.change: {ref!r} is not in changes")
        if not cid.startswith(_case_prefix(ref)):
            raise Invalid(f"{where}.id: {cid} does not belong to {ref}")
        if not changes[ref]["manual"]:
            raise Invalid(f"{where}: {ref} is marked as needing no manual test")
        platform = case.get("platform", "any")
        if platform not in PLATFORMS:
            raise Invalid(f"{where}.platform: must be one of {', '.join(PLATFORMS)}")
        fix = case.get("fix")
        if fix is not None:
            if not isinstance(fix, dict) or not _pos_int(fix.get("pr")) or not isinstance(fix.get("merged"), bool):
                raise Invalid(f"{where}.fix: needs pr and merged")
            fix = {
                "pr": fix["pr"],
                "merged": fix["merged"],
                "head": _sha(fix.get("head"), f"{where}.fix.head", optional=not fix["merged"]),
            }
        cases[cid] = {
            "id": cid,
            "change": ref,
            "title": _text(case.get("title"), f"{where}.title"),
            "steps": _steps(case.get("steps"), f"{where}.steps"),
            "pass": _text(case.get("pass"), f"{where}.pass"),
            "on_failure": _text(case.get("on_failure"), f"{where}.on_failure"),
            "platform": platform,
            "fix": fix,
        }
    for ref, change in changes.items():
        if change["manual"] and not any(c["change"] == ref for c in cases.values()):
            raise Invalid(f"changes: {ref} needs at least one case, or manual: false with a reason")

    promoted = raw.get("promoted")
    if promoted is not None:
        if scope["kind"] != "integration" or not isinstance(promoted, dict):
            raise Invalid("promoted: only the integration scope records promotions")
        refs = promoted.get("changes")
        if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and CHANGE_RE.match(r) for r in refs):
            raise Invalid("promoted.changes: must list change refs")
        if set(refs) & set(changes):
            raise Invalid("promoted.changes: a promoted change cannot also be in scope")
        promoted = {
            "date": _text(promoted.get("date"), "promoted.date"),
            "production_head": _sha(promoted.get("production_head"), "promoted.production_head"),
            "changes": sorted(set(refs)),
        }

    return {
        "repo": repo,
        "scope": {"key": key, "kind": scope["kind"], "prs": sorted(set(prs))},
        "target": target,
        "environment": _env(raw.get("environment", {"status": "not documented"}), "environment"),
        "install": _env(raw["install"], "install") if raw.get("install") is not None else None,
        "heads": heads,
        "review": review,
        "changes": list(changes.values()),
        "cases": list(cases.values()),
        "promoted": promoted,
    }


# --- plan file -----------------------------------------------------------------------


def read_plan(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Stop(f"cannot read the existing plan {path}: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA or not isinstance(plan.get("cases"), list):
        raise Stop(f"{path} is not a {SCHEMA} plan")
    return plan


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".plan-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, path)


def _basis(change: dict[str, Any] | None, fix: dict[str, Any] | None) -> str | None:
    if change is None:
        return None
    basis = change["head"]
    if fix and fix["merged"] and fix["head"]:
        basis += f"+fix:{fix['head']}"
    return basis


def merge(existing: dict[str, Any] | None, proposal: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fold this run's proposal into the plan. Returns (plan, report)."""
    key = proposal["scope"]["key"]
    if existing is not None:
        if existing.get("repo") != proposal["repo"] or existing.get("scope", {}).get("key") != key:
            raise Stop(
                f"the existing plan is for {existing.get('repo')} {existing.get('scope', {}).get('key')}, "
                f"not {proposal['repo']} {key}"
            )
    old_cases = {c["id"]: c for c in (existing or {}).get("cases", [])}
    old_changes = {c["ref"]: c for c in (existing or {}).get("changes", [])}
    old_in_scope = {ref for ref, c in old_changes.items() if c.get("in_scope", True)}
    promotions = list((existing or {}).get("promotions", []))
    new_changes = {c["ref"]: c for c in proposal["changes"]}

    for cid, case in ((c["id"], c) for c in proposal["cases"]):
        if cid in old_cases and old_cases[cid]["change"] != case["change"]:
            raise Stop(f"case {cid} belongs to {old_cases[cid]['change']}, not {case['change']}")

    promotion_index = None
    promoted_refs: set[str] = set()
    if proposal["promoted"] is not None:
        promoted_refs = set(proposal["promoted"]["changes"])
        promotions.append({k: proposal["promoted"][k] for k in ("date", "production_head", "changes")})
        promotion_index = len(promotions) - 1

    report: dict[str, list[str]] = {
        "added": [], "revised": [], "retired": [], "promoted": [], "reactivated": [],
        "changes_added": sorted(set(new_changes) - old_in_scope),
        "changes_removed": sorted(old_in_scope - set(new_changes) - promoted_refs),
    }
    cases: list[dict[str, Any]] = []
    for case in proposal["cases"]:
        old = old_cases.pop(case["id"], None)
        fp = _fingerprint(case)
        entry = {
            **case,
            "rev": 1,
            "fingerprint": fp,
            "lifecycle": "active",
            "promotion": None,
            "basis": _basis(new_changes[case["change"]], case["fix"]),
            "latest": None,
            "attempts": 0,
            "state": "not run",
        }
        if old is None:
            report["added"].append(case["id"])
        else:
            entry["rev"] = old["rev"] + (1 if old.get("fingerprint") != fp else 0)
            entry["latest"] = old.get("latest")
            entry["attempts"] = old.get("attempts", 0)
            if entry["rev"] != old["rev"]:
                report["revised"].append(case["id"])
            if old.get("lifecycle") != "active":
                report["reactivated"].append(case["id"])
        cases.append(entry)

    for old in old_cases.values():
        kept = dict(old)
        if old.get("lifecycle") == "active":
            if old["change"] in promoted_refs:
                kept["lifecycle"], kept["promotion"] = "promoted", promotion_index
                report["promoted"].append(old["id"])
            else:
                kept["lifecycle"] = "retired"
                report["retired"].append(old["id"])
        cases.append(kept)

    retained_changes = [c for ref, c in old_changes.items() if ref not in new_changes]
    plan = {
        "schema": SCHEMA,
        "repo": proposal["repo"],
        "scope": {**proposal["scope"], "title": artifact_title(proposal["repo"], key)},
        "target": proposal["target"],
        "environment": proposal["environment"],
        "install": proposal["install"],
        "heads": proposal["heads"],
        "review": proposal["review"],
        "updated_at": _now(),
        "changes": [{**c, "in_scope": True} for c in proposal["changes"]]
        + [{**c, "in_scope": False} for c in retained_changes],
        "cases": cases,
        "promotions": promotions,
    }
    plan["cases"] = [_with_state(c, plan) for c in plan["cases"]]
    return plan, report


# --- results ---------------------------------------------------------------------------


def _result_bodies(raw: Any) -> list[Any]:
    """Accept a list of documents, or an object wrapping one (an ArtifactData listing)."""
    if isinstance(raw, dict):
        for key in ("documents", "docs", "items", "results", "rows"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        raise Invalid("results: expected a list of documents")
    bodies = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("data"), dict):
            item = item["data"]
        bodies.append(item)
    return bodies


def clean_result(body: Any) -> dict[str, Any] | None:
    """Allowlist a submission's fields. Anything else (notes, references) is dropped."""
    if not isinstance(body, dict):
        return None
    case_id, rev, basis = body.get("case_id"), body.get("case_rev"), body.get("basis")
    status, tested, recorded = body.get("status"), body.get("tested_head"), body.get("recorded_at")
    if not (isinstance(case_id, str) and CASE_RE.match(case_id)):
        return None
    if not (isinstance(rev, int) and not isinstance(rev, bool) and rev >= 1):
        return None
    if status not in STATUSES or not isinstance(basis, str) or not isinstance(recorded, str):
        return None
    if not (isinstance(tested, str) and SHA_RE.match(tested)):
        return None
    return {"case_id": case_id, "rev": rev, "basis": basis, "status": status, "tested_head": tested, "recorded_at": recorded}


def _with_state(case: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    case = dict(case)
    latest = case.get("latest")
    applicable = bool(latest and latest["rev"] == case["rev"] and latest["basis"] == case["basis"])
    fix = case.get("fix")
    if fix and not fix["merged"]:
        case["state"], case["group"] = "fix pending", "pending"
    elif applicable:
        case["state"] = latest["status"]  # type: ignore[index]
        current = plan["heads"]["integration"] if plan["scope"]["kind"] == "integration" else None
        carried = bool(current and latest["tested_head"] != current)  # type: ignore[index]
        case["group"] = "carried" if carried else "done"
    elif case.get("attempts"):
        case["state"], case["group"] = "stale", "todo"
    else:
        case["state"], case["group"] = "not run", "todo"
    if case.get("lifecycle") != "active":
        case["group"] = case["lifecycle"]
    return case


def apply_results(plan: dict[str, Any], raw: Any) -> tuple[dict[str, Any], dict[str, int]]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    ignored = 0
    for body in _result_bodies(raw):
        result = clean_result(body)
        if result is None:
            ignored += 1
            continue
        by_case.setdefault(result["case_id"], []).append(result)
    known = {c["id"] for c in plan["cases"]}
    unknown = sum(len(v) for k, v in by_case.items() if k not in known)
    cases = []
    for case in plan["cases"]:
        results = sorted(by_case.get(case["id"], []), key=lambda r: r["recorded_at"])
        case = dict(case)
        case["attempts"] = len(results)
        matching = [r for r in results if r["rev"] == case["rev"] and r["basis"] == case["basis"]]
        pick = matching[-1] if matching else (results[-1] if results else None)
        case["latest"] = {k: pick[k] for k in ("status", "rev", "basis", "tested_head", "recorded_at")} if pick else None
        cases.append(_with_state(case, plan))
    plan = {**plan, "cases": cases, "updated_at": _now()}
    return plan, {"read": sum(len(v) for v in by_case.values()), "ignored": ignored, "unknown_case": unknown}


# --- summary ----------------------------------------------------------------------------


def summarize(plan: dict[str, Any]) -> dict[str, Any]:
    active = [c for c in plan["cases"] if c.get("lifecycle") == "active"]
    counts: dict[str, int] = {}
    for case in active:
        counts[case["state"]] = counts.get(case["state"], 0) + 1
    failed = [
        f"{c['id']}@{c['latest']['tested_head'][:7]}"
        for c in active
        if c["state"] == "failed" and c.get("latest")
    ]
    return {
        "repo": plan["repo"],
        "scope": plan["scope"]["key"],
        "active": len(active),
        "counts": dict(sorted(counts.items())),
        "retired": sum(1 for c in plan["cases"] if c.get("lifecycle") == "retired"),
        "promoted": sum(1 for c in plan["cases"] if c.get("lifecycle") == "promoted"),
        "failed": failed,
        "no_manual_test": [c["ref"] for c in plan["changes"] if c.get("in_scope") and not c["manual"]],
        "heads": {c["ref"]: c["head"][:7] for c in plan["changes"] if c.get("in_scope")},
        "integration_head": (plan["heads"].get("integration") or "")[:7] or None,
        "not_converged": [f"pr-{r['pr']}@{r['head'][:7]}" for r in plan.get("review", []) if not r["converged"]],
    }


def summary_text(s: dict[str, Any]) -> str:
    counts = ", ".join(f"{k} {v}" for k, v in s["counts"].items()) or "none"
    lines = [
        f"manual-tests {s['repo']} {s['scope']}: {s['active']} active ({counts}); retired {s['retired']}; promoted {s['promoted']}",
        "failed: " + (" ".join(s["failed"]) or "none"),
        "heads: " + " ".join(f"{k}={v}" for k, v in s["heads"].items()),
    ]
    if s["integration_head"]:
        lines.append(f"integration: {s['integration_head']}")
    if s["not_converged"]:
        lines.append("not converged: " + " ".join(s["not_converged"]))
    return "\n".join(lines)


# --- render ------------------------------------------------------------------------------

PLAN_SLOT = re.compile(r"/\*@@PLAN_JSON@@\*/\s*null")
TOKENS_SLOT = "/*@@HOUSE_TOKENS@@*/"
TITLE_SLOT = "@@TITLE@@"


def render(plan: dict[str, Any], template: str, tokens: str) -> str:
    if not PLAN_SLOT.search(template) or TOKENS_SLOT not in template or TITLE_SLOT not in template:
        raise Invalid("template is missing its plan, token or title slot")
    data = json.dumps(plan, ensure_ascii=True, separators=(",", ":"))
    # Keep the embedded JSON from closing its own <script> element.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = html.escape(plan["scope"]["title"], quote=True)
    tokens = re.sub(r"/\*.*?\*/", "", tokens, flags=re.S).strip()
    page = template.replace(TITLE_SLOT, title, 1).replace(TOKENS_SLOT, tokens, 1)
    return PLAN_SLOT.sub(lambda _: data, page, count=1)


# --- lock --------------------------------------------------------------------------------


def lock(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        held = path.read_text(encoding="utf-8", errors="replace").strip()
        raise Stop(f"another manual-tests run holds {path} ({held}); ask the user before removing it") from exc
    with os.fdopen(fd, "w") as handle:
        handle.write(f"pid={os.getpid()} at={_now()}\n")


def unlock(directory: Path) -> None:
    (directory / "lock").unlink(missing_ok=True)


# --- CLI ---------------------------------------------------------------------------------


def load_results(path: Path) -> Any:
    """A JSON listing file, or a directory of one JSON file per document (ArtifactData `out_dir`)."""
    if not path.is_dir():
        return _load(str(path))
    return [_load(str(f)) for f in sorted(path.rglob("*.json"))]


def _load(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Invalid(f"cannot read {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("paths", help="scope key, state directory and artifact title")
    p.add_argument("--repo", required=True)
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--pr", type=int, action="append", default=[])
    for name in ("lock", "unlock"):
        sub.add_parser(name).add_argument("--dir", required=True)
    p = sub.add_parser("merge", help="fold a proposal into the plan")
    p.add_argument("--plan", required=True)
    p.add_argument("--proposal", required=True)
    p = sub.add_parser("apply-results", help="record the latest applicable result per case")
    p.add_argument("--plan", required=True)
    p.add_argument("--results", required=True, help="a listing file, or a directory of per-document JSON files")
    p = sub.add_parser("summary", help="report-only counts")
    p.add_argument("--plan", required=True)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("render", help="embed the plan in the Artifact page")
    p.add_argument("--plan", required=True)
    p.add_argument("--out", required=True)
    here = Path(__file__).resolve().parent
    p.add_argument("--template", default=str(here.parent / "assets" / "page.html"))
    p.add_argument("--tokens", default=str(here.parents[2] / "references" / "house-tokens.css"))
    args = parser.parse_args(argv)

    try:
        if args.cmd == "paths":
            key = scope_key(args.kind, args.pr)
            directory = state_dir(args.repo, key)
            out = {
                "key": key,
                "dir": str(directory),
                "plan": str(directory / "plan.json"),
                "state": str(directory / "state.md"),
                "title": artifact_title(args.repo, key),
            }
            print(json.dumps(out, indent=2))
        elif args.cmd == "lock":
            lock(Path(args.dir))
        elif args.cmd == "unlock":
            unlock(Path(args.dir))
        elif args.cmd == "merge":
            plan_path = Path(args.plan)
            plan, report = merge(read_plan(plan_path), validate_proposal(_load(args.proposal)))
            write_json(plan_path, plan)
            print(json.dumps(report, indent=2))
        elif args.cmd == "apply-results":
            plan_path = Path(args.plan)
            plan = read_plan(plan_path)
            if plan is None:
                raise Stop(f"no plan at {plan_path}")
            plan, counts = apply_results(plan, load_results(Path(args.results)))
            write_json(plan_path, plan)
            print(json.dumps(counts))
        elif args.cmd == "summary":
            plan = read_plan(Path(args.plan))
            if plan is None:
                raise Stop(f"no plan at {args.plan}")
            s = summarize(plan)
            print(json.dumps(s, indent=2) if args.json else summary_text(s))
        elif args.cmd == "render":
            plan = read_plan(Path(args.plan))
            if plan is None:
                raise Stop(f"no plan at {args.plan}")
            page = render(plan, Path(args.template).read_text(encoding="utf-8"), Path(args.tokens).read_text(encoding="utf-8"))
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(page, encoding="utf-8")
            print(args.out)
    except Invalid as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 2
    except Stop as exc:
        print(f"stop: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
