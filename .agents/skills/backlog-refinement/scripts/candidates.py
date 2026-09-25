#!/usr/bin/env python3
"""List the backlog-refinement queue: open issues not yet assessed for agent-readiness.

An issue is "un-refined" when it carries neither `agent: refined` nor any
`agent-bail:*` label and is not already `dev: agent`. Those are the issues
`backlog-refinement refine` should process. Epics and obvious tracking issues
are surfaced separately so the operator can see them without them polluting the
work queue.

A `dev: agent` issue that is ALSO `agent: refined` was tagged by this skill and
is trusted-ready. But a `dev: agent` issue WITHOUT `agent: refined` was tagged by
something else — older triage, a bulk import, a parallel pass — and has never been
verified-against-HEAD. `refine --all` walks only the un-refined bucket, so these
pre-tagged issues are silently skipped and feed `agent-loop` stale work. They are
surfaced as a distinct "re-verify" bucket so the operator re-assesses them (same
verify-against-HEAD + §1 pass as a fresh refine) before trusting the queue. When
such an issue ALSO carries a bail label the bail wins and it classifies as
excluded, but it is still surfaced — under "Conflicted" — because the leftover
`dev: agent` label misrepresents the issue's real queue state to anything that
reads labels directly. `agent-loop` itself is not fooled: it selects through
`ready.py`, which hard-excludes `agent-bail:*`.

Assessed issues missing what the current core rubric sets — a priority label,
or the `needs:` label a grill-class bail requires — are listed as "backfill"
for `refine --backfill`. An epic already split into GitHub sub-issues owes no
interview, so it is not listed for a missing `needs:` label. `--grill` lists the
`needs: grill` and `needs: product-grill` queues, highest priority first.
Repository settings (priority label names, title prefixes, auto-managed skip
labels) come from `.backlog/refinement.local.md` at the repository root,
falling back to a legacy per-harness `RUBRIC.md` (see `rubric.py`).

Mirrors the gh-invocation conventions of `../../issues/scripts/ready.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

REFINED_LABEL = "agent: refined"
READY_LABEL = "dev: agent"
BAIL_PREFIX = "agent-bail:"
# Bail categories whose next step is an interview (core rubric, "Needs labels").
GRILL_CLASS_BAILS = frozenset({
    "agent-bail: open-decision",
    "agent-bail: spec-gap",
    "agent-bail: epic",
})
EPIC_BAIL = "agent-bail: epic"
GRILL_NEEDS_LABELS = frozenset({
    "needs: grill",
    "needs: product-grill",
})
# Surfaced but never auto-queued — these read as coordination, not bounded work.
EPIC_TITLE_MARKERS = ("epic:", "[epic]")
LABEL_PREFIXES_TO_SHOW = ("area:", "dev:", "agent-bail:", "agent:", "status:", "needs:", "priority:")
# `gh issue list` pages internally; the cap only guards against truncation.
GH_LIST_LIMIT = 10000

# Sibling module shared by every backlog-refinement script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rubric import (  # noqa: E402
    LOCAL_RUBRIC,
    RubricConfig,
    load_config,
    repo_root,
    title_priority,
)

__all__ = ["LOCAL_RUBRIC", "RubricConfig"]

CONFIG = load_config(repo_root())


def fetch_open_issues() -> list[dict[str, Any]]:
    """Every open issue with the fields refinement triage needs."""
    cmd = [
        "gh", "issue", "list",
        "--state", "open",
        "--limit", str(GH_LIST_LIMIT),
        "--json", "number,title,labels,assignees,url",
    ]
    try:
        # 120s: a large backlog pages through many requests.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            "Timed out after 120s while running `gh issue list`. "
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
        issues: list[dict[str, Any]] = json.loads(result.stdout)
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
    if any(name in CONFIG.auto_managed_labels for name in labels):
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


def backfill_gaps(issue: dict[str, Any], *, decomposed: bool = False) -> list[str]:
    """What an assessed issue lacks under the current core rubric.

    ``decomposed`` marks an epic already split into sub-issues: no interview is
    owed there, so a missing ``needs:`` label is not a gap.
    """
    labels = label_names(issue)
    gaps = []
    if CONFIG.priority_labels and not any(n in CONFIG.priority_labels for n in labels):
        gaps.append("priority")
    grill_class = [n for n in labels if n in GRILL_CLASS_BAILS]
    if decomposed and grill_class == [EPIC_BAIL]:
        grill_class = []
    if grill_class and not any(n in GRILL_NEEDS_LABELS for n in labels):
        gaps.append("needs")
    return gaps


def needs_sub_issue_check(issue: dict[str, Any]) -> bool:
    """An epic bail with no needs: label, whose backfill verdict depends on its children."""
    labels = label_names(issue)
    grill_class = [n for n in labels if n in GRILL_CLASS_BAILS]
    return grill_class == [EPIC_BAIL] and not any(n in GRILL_NEEDS_LABELS for n in labels)


def has_sub_issues(number: int) -> bool:
    """Whether GitHub records sub-issues under this issue. Fails open to False."""
    cmd = [
        "gh", "api", f"repos/{{owner}}/{{repo}}/issues/{number}",
        "--jq", ".sub_issues_summary.total // 0",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    try:
        return result.returncode == 0 and int(result.stdout.strip() or "0") > 0
    except ValueError:
        return False


def suggested_priority(issue: dict[str, Any]) -> str | None:
    """The priority a recognised title prefix records, when no label is set yet."""
    labels = label_names(issue)
    if any(n in CONFIG.priority_labels for n in labels):
        return None
    return title_priority(issue["title"], CONFIG)


def priority_rank(issue: dict[str, Any]) -> int:
    """Position of the issue's priority label, highest first; unprioritised last."""
    for index, label in enumerate(CONFIG.priority_labels):
        if label in label_names(issue):
            return index
    return len(CONFIG.priority_labels)


def priority_conflicted(issue: dict[str, Any]) -> bool:
    """More than one of the repository's priority labels — a human call."""
    return sum(n in CONFIG.priority_labels for n in label_names(issue)) > 1


def format_row(issue: dict[str, Any]) -> str:
    shown = LABEL_PREFIXES_TO_SHOW + CONFIG.priority_labels
    display = [n for n in label_names(issue) if n.startswith(shown)]
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
    parser.add_argument(
        "--grill", action="store_true",
        help="list the needs: grill and needs: product-grill queues, highest priority first",
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

    assessed = buckets["ready"] + buckets["excluded"]
    # One API call per epic bail that lacks a needs: label — few in practice.
    decomposed = {
        i["number"] for i in assessed if needs_sub_issue_check(i) and has_sub_issues(i["number"])
    }

    def gaps(issue: dict[str, Any]) -> list[str]:
        return backfill_gaps(issue, decomposed=issue["number"] in decomposed)

    backfill = sorted((i for i in assessed if gaps(i)), key=lambda i: i["number"])
    grill_queues = {
        label: sorted(
            (i for i in issues if label in label_names(i)),
            key=lambda i: (priority_rank(i), i["number"]),
        )
        for label in sorted(GRILL_NEEDS_LABELS)
    }
    conflicted_priority = sorted(
        (i for i in issues if priority_conflicted(i)), key=lambda i: i["number"]
    )

    conflicted = [i for i in buckets["excluded"] if READY_LABEL in label_names(i)]

    if args.json:
        # The work queue is `unrefined` + `reverify` (+ epics for visibility); counts for the rest.
        counts = {k: len(v) for k, v in buckets.items()}
        counts["backfill"] = len(backfill)
        counts["priority_conflicted"] = len(conflicted_priority)
        counts["conflicted"] = len(conflicted)
        for label, queue in grill_queues.items():
            counts[label] = len(queue)
        payload: dict[str, Any] = {
            "rubric": CONFIG.source,
            "counts": counts,
            "unrefined": buckets["unrefined"][: args.limit],
            "reverify": buckets["reverify"][: args.limit],
            "epic": buckets["epic"][: args.limit],
            "backfill": [
                {**i, "gaps": gaps(i), "suggested_priority": suggested_priority(i)}
                for i in backfill[: args.limit]
            ],
            "priority_conflicted": [i["number"] for i in conflicted_priority],
        }
        if args.grill:
            payload["grill_queues"] = {
                label: queue[: args.limit] for label, queue in grill_queues.items()
            }
        if args.include_refined:
            payload["ready"] = buckets["ready"][: args.limit]
            payload["excluded"] = buckets["excluded"][: args.limit]
        print(json.dumps(payload, indent=2))
        return 0

    c = {k: len(v) for k, v in buckets.items()}
    print(
        f"Open: {len(issues)}  |  ready (dev: agent + refined): {c['ready']}  |  "
        f"RE-VERIFY (dev: agent, NOT refined): {c['reverify']}  |  "
        f"conflicted: {len(conflicted)}  |  "
        f"excluded (agent-bail:*): {c['excluded']}  |  epics: {c['epic']}  |  "
        # Only surface the auto-managed skip count when the repo actually uses it.
        + (f"skipped (auto-managed): {c['skipped']}  |  " if c["skipped"] else "")
        + f"backfill: {len(backfill)}  |  "
        + "  |  ".join(f"{label}: {len(q)}" for label, q in grill_queues.items())
        + f"  |  UN-REFINED: {c['unrefined']}"
    )

    def section(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        print(f"\n{title} ({len(rows)}):")
        for issue in rows[: args.limit]:
            print(format_row(issue))

    section("Re-verify — pre-tagged dev: agent, never assessed (do BEFORE trusting the queue)",
            buckets["reverify"])
    # An excluded issue that still carries READY_LABEL is a conflicted state:
    # the label says queue-me and the bail says do-not. agent-loop resolves it
    # correctly today — it selects through ready.py, which hard-excludes
    # agent-bail:* — so this is label hygiene, not a live queue leak. It still
    # matters: the stale label misreports queue state to a human scanning labels
    # and to any consumer that does not filter through ready.py.
    # Surface it by default — the `excluded` section below is --include-refined only.
    section("Conflicted — excluded but still dev: agent (stale label; strip it so the labels match the bail)",
            conflicted)
    section("Conflicted priority — more than one priority label (human call)",
            conflicted_priority)
    section("Un-refined — refinement queue", buckets["unrefined"])
    section("Backfill — assessed, missing a priority or needs label (refine --backfill)",
            backfill)
    section("Epics / coordination (review manually, do not auto-queue)", buckets["epic"])
    if args.grill:
        for label, queue in grill_queues.items():
            section(f"Grill queue — {label}, highest priority first", queue)
    if args.include_refined:
        section("Ready (dev: agent + refined)", buckets["ready"])
        section("Excluded (agent-bail:*)", buckets["excluded"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
