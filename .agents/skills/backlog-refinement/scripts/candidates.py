#!/usr/bin/env python3
"""List the backlog-refinement queue: open issues not yet assessed for agent-readiness.

An issue is "un-refined" when it carries neither `agent: refined` nor any
`agent-bail:*` label and is not already `dev: agent`. Those are the issues
`/backlog-refinement refine` should process. Epics and obvious tracking issues
are surfaced separately so the operator can see them without them polluting the
work queue.

A `dev: agent` issue that is ALSO `agent: refined` was tagged by this skill and
is trusted-ready. But a `dev: agent` issue WITHOUT `agent: refined` was tagged by
something else — older triage, a bulk import, a parallel pass — and has never been
verified-against-HEAD. `refine --all` walks only the un-refined bucket, so these
pre-tagged issues are silently skipped and feed `/agent-loop` stale work. They are
surfaced as a distinct "re-verify" bucket so the operator re-assesses them (same
verify-against-HEAD + §1 pass as a fresh refine) before trusting the queue.

Mirrors the gh-invocation conventions of `../../issues/scripts/ready.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any

REFINED_LABEL = "agent: refined"
READY_LABEL = "dev: agent"
BAIL_PREFIX = "agent-bail:"
# Surfaced but never auto-queued — these read as coordination, not bounded work.
EPIC_TITLE_MARKERS = ("epic:",)
LABEL_PREFIXES_TO_SHOW = ("area:", "dev:", "agent-bail:", "agent:", "status:", "priority:")
GH_LIST_LIMIT = 1000

# Workflow-auto-managed labels: issues a scheduled workflow both OPENS and
# CLOSES (e.g. a nightly metrics/digest issue auto-closed after N days). They
# are never refinement tasks, and a refinement comment on one resets its
# `updatedAt` — which can DELAY that auto-close. The label list is repo-specific,
# so it is declared in a marker in the consumer-owned RUBRIC.md rather than
# hard-coded here (keeps repo config in the rubric, the skill's source of truth):
#     <!-- auto-managed-labels: label-a, label-b -->
# Absent or empty marker → no skipping (safe default; pre-existing repos are
# unaffected until they opt in).
_AUTO_MANAGED_MARKER = re.compile(
    r"(?m)^[ \t]*<!--\s*auto-managed-labels:\s*(.*?)\s*-->[ \t]*$"
)


def load_auto_managed_labels() -> tuple[str, ...]:
    """Repo-specific skip labels, read from the sibling RUBRIC.md marker."""
    rubric_path = os.path.join(os.path.dirname(__file__), "..", "RUBRIC.md")
    try:
        with open(rubric_path, encoding="utf-8") as fh:
            matches = _AUTO_MANAGED_MARKER.findall(fh.read())
    except OSError as exc:
        sys.stderr.write(f"Could not read required backlog rubric {rubric_path}: {exc}\n")
        sys.exit(1)
    if not matches:
        # Absent marker → the repo hasn't opted into auto-managed skipping.
        # Return no labels (the documented safe default) rather than failing;
        # pre-existing consumers whose RUBRIC.md predates this marker must keep
        # working. A present-but-empty marker yields the same empty result below.
        return ()
    if len(matches) > 1:
        sys.stderr.write(
            f"Expected at most one auto-managed-labels marker in {rubric_path}; "
            f"found {len(matches)}\n"
        )
        sys.exit(1)
    match = matches[0]
    return tuple(label.strip() for label in match.split(",") if label.strip())


AUTO_MANAGED_LABELS = load_auto_managed_labels()


def fetch_open_issues() -> list[dict[str, Any]]:
    """Every open issue with the fields refinement triage needs."""
    cmd = [
        "gh", "issue", "list",
        "--state", "open",
        "--limit", str(GH_LIST_LIMIT),
        "--json", "number,title,labels,assignees,url",
    ]
    try:
        # 60s timeout matches ready.py: a hung GitHub API shouldn't stall callers.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            "Timed out after 60s while running `gh issue list`. "
            "Check GitHub auth/network connectivity and retry.\n"
        )
        sys.exit(1)
    except OSError as exc:
        sys.stderr.write(f"Could not run `gh issue list`: {exc}\n")
        sys.exit(1)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"Invalid JSON from `gh issue list`: {exc}\n")
        sys.exit(1)
    if len(issues) >= GH_LIST_LIMIT:
        sys.stderr.write(
            f"Open-issue query reached the {GH_LIST_LIMIT}-item gh limit; "
            "refusing a possibly truncated refinement queue.\n"
        )
        sys.exit(1)
    return issues


def label_names(issue: dict[str, Any]) -> list[str]:
    return [label["name"] for label in issue.get("labels", [])]


def classify(issue: dict[str, Any]) -> str:
    """One of: skipped | ready | reverify | excluded | epic | unrefined."""
    labels = label_names(issue)
    if any(name in AUTO_MANAGED_LABELS for name in labels):
        # Opened AND closed by a scheduled workflow — never a refinement task.
        return "skipped"
    if any(name.startswith(BAIL_PREFIX) for name in labels):
        # Bail labels are exclusion signals even if a stale dev: agent label
        # remains after a partial/manual relabel.
        return "excluded"
    if READY_LABEL in labels:
        # dev: agent + refined = this skill tagged it (trusted ready).
        # dev: agent WITHOUT refined = pre-tagged elsewhere, never verified — re-verify.
        return "ready" if REFINED_LABEL in labels else "reverify"
    if REFINED_LABEL in labels:
        # Assessed but neither ready nor bailed — treat as excluded-without-reason.
        return "excluded"
    title = issue["title"].lower()
    if any(marker in title for marker in EPIC_TITLE_MARKERS):
        return "epic"
    return "unrefined"


def format_row(issue: dict[str, Any]) -> str:
    display = [n for n in label_names(issue) if n.startswith(LABEL_PREFIXES_TO_SHOW)]
    label_str = " ".join(f"[{n}]" for n in display)
    assignees = issue.get("assignees") or []
    assignee = f"@{assignees[0]['login']}" if assignees else "unassigned"
    title = issue["title"]
    if len(title) > 68:
        title = title[:65] + "..."
    return f"#{issue['number']:<6} {label_str:<48} ({assignee:<15}) {title}"


def non_negative_int(value: str) -> int:
    """Argparse type for row limits, where zero intentionally prints no rows."""
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="output JSON instead of a table")
    parser.add_argument(
        "--limit",
        type=non_negative_int,
        default=40,
        help="max rows to print (default 40)",
    )
    parser.add_argument(
        "--include-refined", action="store_true",
        help="also list issues already assessed (ready / excluded)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    issues = fetch_open_issues()

    buckets: dict[str, list[dict[str, Any]]] = {
        "unrefined": [], "reverify": [], "ready": [], "excluded": [], "epic": [],
        "skipped": [],
    }
    for issue in issues:
        buckets[classify(issue)].append(issue)

    for items in buckets.values():
        items.sort(key=lambda i: i["number"])

    if args.json:
        # The work queue is `unrefined` + `reverify` (+ epics for visibility); counts for the rest.
        print(json.dumps({
            "counts": {k: len(v) for k, v in buckets.items()},
            "unrefined": buckets["unrefined"][: args.limit],
            "reverify": buckets["reverify"][: args.limit],
            "epic": buckets["epic"],
        }, indent=2))
        return 0

    c = {k: len(v) for k, v in buckets.items()}
    print(
        f"Open: {len(issues)}  |  ready (dev: agent + refined): {c['ready']}  |  "
        f"RE-VERIFY (dev: agent, NOT refined): {c['reverify']}  |  "
        f"excluded (agent-bail:*): {c['excluded']}  |  epics: {c['epic']}  |  "
        # Only surface the auto-managed skip count when the repo actually uses it.
        + (f"skipped (auto-managed): {c['skipped']}  |  " if c["skipped"] else "")
        + f"UN-REFINED: {c['unrefined']}"
    )

    def section(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        print(f"\n{title} ({len(rows)}):")
        for issue in rows[: args.limit]:
            print(format_row(issue))

    section("Re-verify — pre-tagged dev: agent, never assessed (do BEFORE trusting the queue)",
            buckets["reverify"])
    section("Un-refined — refinement queue", buckets["unrefined"])
    section("Epics / coordination (review manually, do not auto-queue)", buckets["epic"])
    if args.include_refined:
        section("Ready (dev: agent + refined)", buckets["ready"])
        section("Excluded (agent-bail:*)", buckets["excluded"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
