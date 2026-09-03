#!/usr/bin/env python3
"""Aggregate `agent-bail:*` issues + their inline RCA stubs for the post-loop RCA pass.

Reads every open/closed issue carrying an `agent-bail:*` label, parses the
`<!-- agent-loop-rca ... -->` stub (RUBRIC.md §4) from its comments when present,
and groups by category + A/B bucket so `/backlog-refinement rca` can turn the
run's bails into rubric edits. Bucket-A bails are flagged loudly: they mean
refinement tagged something `dev: agent` that the loop couldn't finish.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

BAIL_PREFIX = "agent-bail:"
# Backward-compatible defaults for bails whose older comments lack an RCA stub.
DEFAULT_BUCKET_A = {"agent-bail: stale", "agent-bail: spec-gap", "agent-bail: loop-mechanics"}
RCA_STUB_RE = re.compile(r"<!--\s*agent-loop-rca\s*(.*?)-->", re.DOTALL | re.IGNORECASE)
GH_LIST_LIMIT = 1000


def run_gh(args: list[str]) -> Any:
    action = " ".join(args[:2])
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"Timed out after 60s while running `gh {action}`. "
            "Check GitHub auth/network connectivity and retry.\n"
        )
        sys.exit(1)
    except OSError as exc:
        sys.stderr.write(f"Could not run `gh {action}`: {exc}\n")
        sys.exit(1)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"Invalid JSON from `gh {action}`: {exc}\n")
        sys.exit(1)


def _parse_iso_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_since(value: str) -> datetime:
    """Parse an ISO date/time argument and normalize it to UTC."""
    try:
        return _parse_iso_datetime(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date/time: {value}") from exc


def fetch_bailed(since: datetime | None) -> list[dict[str, Any]]:
    """All issues (any state) with an agent-bail:* label, newest activity first."""
    issues = run_gh([
        "issue", "list", "--state", "all", "--limit", str(GH_LIST_LIMIT),
        "--json", "number,title,labels,state,updatedAt,url",
    ]) or []
    if len(issues) >= GH_LIST_LIMIT:
        sys.stderr.write(
            f"Issue query reached the {GH_LIST_LIMIT}-item gh limit; "
            "refusing a possibly truncated RCA report.\n"
        )
        sys.exit(1)
    out = []
    for issue in issues:
        labels = [label["name"] for label in issue.get("labels", [])]
        bail_labels = [name for name in labels if name.startswith(BAIL_PREFIX)]
        if not bail_labels:
            continue
        if since and _parse_iso_datetime(issue["updatedAt"]) < since:
            continue
        issue["_bail_labels"] = bail_labels
        out.append(issue)
    return out


def parse_rca_stub(number: int) -> dict[str, str] | None:
    """Pull the most recent agent-loop-rca stub from an issue's comments."""
    data = run_gh(["issue", "view", str(number), "--json", "comments"]) or {}
    for comment in reversed(data.get("comments", [])):
        match = RCA_STUB_RE.search(comment.get("body", ""))
        if match:
            fields: dict[str, str] = {}
            for line in match.group(1).splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    fields[key.strip()] = val.strip()
            return fields
    return None


def is_bucket_a(issue: dict[str, Any], category: str) -> bool:
    """Use the consumer-recorded RCA bucket, falling back for legacy comments."""
    bucket = str((issue.get("_rca") or {}).get("bucket", "")).upper()
    if bucket in {"A", "B"}:
        return bucket == "A"
    return category in DEFAULT_BUCKET_A


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=parse_since,
        help="ISO date/time; only issues updated on/after it",
    )
    parser.add_argument("--json", action="store_true", help="output JSON instead of a report")
    parser.add_argument(
        "--no-stubs", action="store_true",
        help="skip per-issue comment fetch (faster; omits RCA stub fields)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bailed = fetch_bailed(args.since)

    for issue in bailed:
        issue["_rca"] = None if args.no_stubs else parse_rca_stub(issue["number"])

    by_category: dict[str, list[dict[str, Any]]] = {}
    for issue in bailed:
        for label in issue["_bail_labels"]:
            by_category.setdefault(label, []).append(issue)

    if args.json:
        print(json.dumps({
            "total": len(bailed),
            "by_category": {k: [i["number"] for i in v] for k, v in by_category.items()},
            "issues": [
                {k: v for k, v in i.items() if not k.startswith("labels")}
                for i in bailed
            ],
        }, indent=2, default=str))
        return 0

    if not bailed:
        print("No agent-bail:* issues in window. Nothing to RCA.")
        return 0

    print(f"agent-bail issues in window: {len(bailed)}\n")
    bucket_a_hits = []
    for category in sorted(
        by_category,
        key=lambda c: (not any(is_bucket_a(i, c) for i in by_category[c]), c),
    ):
        rows = by_category[category]
        category_has_bucket_a = any(is_bucket_a(issue, category) for issue in rows)
        flag = "  ⚠ BUCKET A — refinement miss" if category_has_bucket_a else ""
        print(f"{category}  ({len(rows)}){flag}")
        for issue in rows:
            rca = issue.get("_rca") or {}
            diff = rca.get("what-could-differ", "—")
            print(f"   #{issue['number']:<6} [{issue['state']:<6}] {issue['title'][:60]}")
            if diff != "—":
                print(f"            ↳ {diff}")
            if is_bucket_a(issue, category):
                bucket_a_hits.append(issue["number"])
        print()

    if bucket_a_hits:
        print(
            f"→ {len(bucket_a_hits)} Bucket-A bail(s) {bucket_a_hits}: ask which RUBRIC §2 "
            "transformation / §1 check should have caught these at refinement time."
        )
    print("→ For repeated Bucket-B shapes, sharpen the §3 disqualifier. "
          "Append each lesson to LEARNINGS.md; bump rubric version if criteria changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
