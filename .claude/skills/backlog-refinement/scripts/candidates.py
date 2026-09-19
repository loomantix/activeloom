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
for `refine --backfill`. Repository settings (priority label names,
auto-managed skip labels) come from `.backlog/refinement.local.md` at the
repository root, falling back to a legacy per-harness `RUBRIC.md`.

Mirrors the gh-invocation conventions of `../../issues/scripts/ready.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, NamedTuple

REFINED_LABEL = "agent: refined"
READY_LABEL = "dev: agent"
BAIL_PREFIX = "agent-bail:"
NEEDS_PREFIX = "needs:"
# Bail categories whose next step is an interview (core rubric, "Needs labels").
GRILL_CLASS_BAILS = frozenset({
    "agent-bail: open-decision",
    "agent-bail: spec-gap",
    "agent-bail: epic",
})
# Surfaced but never auto-queued — these read as coordination, not bounded work.
EPIC_TITLE_MARKERS = ("epic:",)
LABEL_PREFIXES_TO_SHOW = ("area:", "dev:", "agent-bail:", "agent:", "status:", "needs:")
# `gh issue list` pages internally; the cap only guards against truncation.
GH_LIST_LIMIT = 10000

LOCAL_RUBRIC = os.path.join(".backlog", "refinement.local.md")
LEGACY_HARNESS_ROOTS = (".claude", ".codex", ".agents")


def _marker(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?m)^[ \t]*<!--\s*{name}:\s*(.*?)\s*-->[ \t]*$")


# Workflow-auto-managed labels: issues a scheduled workflow both OPENS and
# CLOSES. They are never refinement tasks, and a refinement comment on one
# resets its `updatedAt` — which can DELAY that auto-close.
_AUTO_MANAGED_MARKER = _marker("auto-managed-labels")
# The repository's four priority label names, highest first. Empty disables
# priority-setting.
_PRIORITY_MARKER = _marker("priority-labels")


class RubricConfig(NamedTuple):
    """Repository settings read from the local (or legacy) rubric."""

    source: str | None
    auto_managed_labels: tuple[str, ...]
    priority_labels: tuple[str, ...]


def repo_root() -> str:
    """The repository the operator is refining — the working directory's checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return os.getcwd()
    return result.stdout.strip() if result.returncode == 0 else os.getcwd()


def locate_rubric(root: str) -> tuple[str | None, bool]:
    """Return (path, is_legacy) for the repository's local rubric, if any."""
    local = os.path.join(root, LOCAL_RUBRIC)
    if os.path.exists(local):
        return local, False
    for harness in LEGACY_HARNESS_ROOTS:
        legacy = os.path.join(root, harness, "skills", "backlog-refinement", "RUBRIC.md")
        if os.path.exists(legacy):
            return legacy, True
    return None, False


def _read_marker(pattern: re.Pattern[str], name: str, text: str, path: str) -> tuple[str, ...] | None:
    """Labels listed in a single-line marker; None when the marker is absent."""
    matches = pattern.findall(text)
    if not matches:
        if f"{name}:" in text:
            # Present but off-shape (indented under a bullet, trailing text on
            # the line). Say so — silently dropping a marker the repo believes
            # is live changes what refinement does without anyone noticing.
            sys.stderr.write(
                f"Found a {name} marker in {path} that is not on a line of its own; "
                "ignoring it. Put the marker alone on one line.\n"
            )
        return None
    if len(matches) > 1:
        sys.stderr.write(f"Expected at most one {name} marker in {path}; found {len(matches)}\n")
        sys.exit(1)
    return tuple(label.strip() for label in matches[0].split(",") if label.strip())


def load_config(root: str) -> RubricConfig:
    """Repository settings, announcing on stderr whenever a default stands in."""
    path, is_legacy = locate_rubric(root)
    if path is None:
        sys.stderr.write(
            f"NOTE: {LOCAL_RUBRIC} not found; using core defaults (no skip labels, "
            "priority off). Run `backlog-refinement setup` to configure this repository.\n"
        )
        return RubricConfig(None, (), ())
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        sys.stderr.write(f"Could not read backlog rubric {path}: {exc}\n")
        sys.exit(1)
    if is_legacy:
        sys.stderr.write(
            f"NOTE: reading legacy rubric {path}; run `backlog-refinement setup` "
            f"to migrate it to {LOCAL_RUBRIC}.\n"
        )
    auto_managed = _read_marker(_AUTO_MANAGED_MARKER, "auto-managed-labels", text, path) or ()
    priority = _read_marker(_PRIORITY_MARKER, "priority-labels", text, path)
    if priority is None:
        sys.stderr.write(
            f"NOTE: no priority-labels marker in {path}; priority backfill is off.\n"
        )
        priority = ()
    return RubricConfig(path, auto_managed, priority)


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


def backfill_gaps(issue: dict[str, Any]) -> list[str]:
    """What an assessed issue lacks under the current core rubric."""
    labels = label_names(issue)
    gaps = []
    if CONFIG.priority_labels and not any(n in CONFIG.priority_labels for n in labels):
        gaps.append("priority")
    if any(n in GRILL_CLASS_BAILS for n in labels) and not any(
        n.startswith(NEEDS_PREFIX) for n in labels
    ):
        gaps.append("needs")
    return gaps


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
    backfill = sorted(
        (i for i in assessed if backfill_gaps(i)), key=lambda i: i["number"]
    )
    conflicted_priority = sorted(
        (i for i in issues if priority_conflicted(i)), key=lambda i: i["number"]
    )

    if args.json:
        # The work queue is `unrefined` + `reverify` (+ epics for visibility); counts for the rest.
        counts = {k: len(v) for k, v in buckets.items()}
        counts["backfill"] = len(backfill)
        counts["priority_conflicted"] = len(conflicted_priority)
        print(json.dumps({
            "rubric": CONFIG.source,
            "counts": counts,
            "unrefined": buckets["unrefined"][: args.limit],
            "reverify": buckets["reverify"][: args.limit],
            "epic": buckets["epic"],
            "backfill": [
                {**i, "gaps": backfill_gaps(i)} for i in backfill[: args.limit]
            ],
            "priority_conflicted": [i["number"] for i in conflicted_priority],
        }, indent=2))
        return 0

    c = {k: len(v) for k, v in buckets.items()}
    print(
        f"Open: {len(issues)}  |  ready (dev: agent + refined): {c['ready']}  |  "
        f"RE-VERIFY (dev: agent, NOT refined): {c['reverify']}  |  "
        f"excluded (agent-bail:*): {c['excluded']}  |  epics: {c['epic']}  |  "
        # Only surface the auto-managed skip count when the repo actually uses it.
        + (f"skipped (auto-managed): {c['skipped']}  |  " if c["skipped"] else "")
        + f"backfill: {len(backfill)}  |  UN-REFINED: {c['unrefined']}"
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
            [i for i in buckets["excluded"] if READY_LABEL in label_names(i)])
    section("Conflicted priority — more than one priority label (human call)",
            conflicted_priority)
    section("Un-refined — refinement queue", buckets["unrefined"])
    section("Backfill — assessed, missing a priority or needs label (refine --backfill)",
            backfill)
    section("Epics / coordination (review manually, do not auto-queue)", buckets["epic"])
    if args.include_refined:
        section("Ready (dev: agent + refined)", buckets["ready"])
        section("Excluded (agent-bail:*)", buckets["excluded"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
