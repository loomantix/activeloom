#!/usr/bin/env python3
"""Apply a reviewed refinement plan to GitHub issues.

Assessors propose; this script applies. It reads a JSON plan (one entry per
issue, shape below), validates the whole plan before touching anything, prints
what it would do, and only mutates with ``--apply``. Mutations run one issue at
a time, in a fixed order — body, labels, comment, close — and each finished
issue is recorded in ``<plan>.applied.json`` so a re-run resumes rather than
double-posting.

Plan shape::

    {"issues": [{
        "number": 22,
        "verdict": "ready" | "exclude" | "refined-only" | "stale",
        "add_labels": ["dev: agent", "priority: medium"],
        "remove_labels": [],
        "comment": "Backlog refinement (...): ...",
        "body": "full rewritten body (ready only), or null",
        "close_reason": "completed" | "not planned" (stale only), or null
    }]}

`agent: refined` is always added. Label hygiene follows the core rubric: a
ready issue loses any `agent-bail:`/`needs:`/`status: blocked` left from an
earlier assessment, and an excluded or stale one loses `dev: agent`. A stale
issue is closed only when the local rubric sets `stale-action: close`;
otherwise the plan's comment stands as the recommendation. With `Rewrite mode:
suggest`, a rewritten body is posted as a comment instead of replacing the body.

    apply-plan.py plan.json            # validate and preview
    apply-plan.py plan.json --apply    # apply, resuming past finished issues
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rubric import RubricConfig, load_config, repo_root  # noqa: E402

REFINED = "agent: refined"
READY = "dev: agent"
STALE = "agent-bail: stale"
BLOCKED = "status: blocked"
BAIL_PREFIX = "agent-bail:"
NEEDS_PREFIX = "needs:"
VERDICTS = ("ready", "exclude", "refined-only", "stale")
CLOSE_REASONS = ("completed", "not planned")
TIMEOUT = 60


def fail(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
    sys.exit(1)


def gh(args: list[str], *, stdin: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], input=stdin, capture_output=True, text=True, timeout=TIMEOUT
        )
    except subprocess.TimeoutExpired:
        fail(f"Timed out after {TIMEOUT}s: gh {' '.join(args[:3])}")
    except OSError as exc:
        fail(f"Could not run gh: {exc}")
    if result.returncode != 0:
        fail(result.stderr or f"gh {' '.join(args[:3])} exited {result.returncode}")
    return result.stdout


# --- validation (pure) ------------------------------------------------------


def validate(plan: Any, repo_labels: set[str], priority_labels: tuple[str, ...]) -> list[str]:
    """Every problem with the plan; empty means it is safe to apply."""
    if not isinstance(plan, dict) or not isinstance(plan.get("issues"), list):
        return ['The plan must be an object with an "issues" list.']
    errors: list[str] = []
    seen: set[int] = set()
    for index, entry in enumerate(plan["issues"]):
        where = f"issues[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: not an object")
            continue
        number = entry.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            errors.append(f"{where}: number must be a positive integer")
            continue
        where = f"#{number}"
        if number in seen:
            errors.append(f"{where}: appears more than once")
        seen.add(number)
        verdict = entry.get("verdict")
        if verdict not in VERDICTS:
            errors.append(f"{where}: verdict must be one of {', '.join(VERDICTS)}")
            continue
        add, remove = entry.get("add_labels", []), entry.get("remove_labels", [])
        if not isinstance(add, list) or not isinstance(remove, list) or not all(isinstance(x, str) for x in [*add, *remove]):
            errors.append(f"{where}: add_labels and remove_labels must be lists of label names")
            continue
        for label in [*add, *remove]:
            # Also catches placeholder text ("none", "none (no label present)")
            # that an assessor wrote where it meant an empty list.
            if label not in repo_labels:
                errors.append(f"{where}: label {label!r} does not exist in this repository")
        if set(add) & set(remove):
            errors.append(f"{where}: a label is both added and removed")
        comment = entry.get("comment")
        if not isinstance(comment, str) or not comment.strip():
            errors.append(f"{where}: comment is required — every assessment explains itself")
        body = entry.get("body")
        if body is not None and (verdict != "ready" or not isinstance(body, str) or not body.strip()):
            errors.append(f"{where}: body is only for a ready verdict, and must be non-empty text")
        close_reason = entry.get("close_reason")
        if verdict == "stale" and close_reason not in CLOSE_REASONS:
            errors.append(f"{where}: stale needs close_reason {' or '.join(map(repr, CLOSE_REASONS))}")
        if verdict != "stale" and close_reason is not None:
            errors.append(f"{where}: close_reason is only for a stale verdict")
        bails = [x for x in add if x.startswith(BAIL_PREFIX)]
        needs = [x for x in add if x.startswith(NEEDS_PREFIX)]
        if sum(x in priority_labels for x in add) > 1:
            errors.append(f"{where}: more than one priority label")
        if len(needs) > 1:
            errors.append(f"{where}: more than one needs: label — choose the interview that comes first")
        if verdict == "ready":
            if READY not in add:
                errors.append(f"{where}: ready must add {READY!r}")
            if bails or needs:
                errors.append(f"{where}: ready cannot carry agent-bail: or needs: labels")
            if body is None:
                errors.append(f"{where}: ready needs the rewritten body")
        elif READY in add:
            errors.append(f"{where}: only a ready verdict adds {READY!r}")
        if verdict == "exclude" and (len(bails) != 1 or STALE in bails):
            errors.append(f"{where}: exclude takes exactly one agent-bail: label other than stale")
        if verdict == "stale" and bails != [STALE]:
            errors.append(f"{where}: stale takes {STALE!r} and no other bail")
        if verdict == "refined-only" and (bails or needs):
            errors.append(f"{where}: refined-only carries no agent-bail: or needs: label")
        if needs and not bails:
            errors.append(f"{where}: a needs: label only accompanies an agent-bail: label")
    return errors


def label_changes(entry: dict[str, Any], current: set[str]) -> tuple[list[str], list[str]]:
    """Labels to add and remove, including the rubric's hygiene rules."""
    add = list(dict.fromkeys([*entry.get("add_labels", []), REFINED]))
    remove = set(entry.get("remove_labels", []))
    if entry["verdict"] == "ready":
        remove |= {x for x in current if x.startswith((BAIL_PREFIX, NEEDS_PREFIX))} | {BLOCKED}
    else:
        remove.add(READY)
    if entry["verdict"] == "stale":
        remove.add(BLOCKED)
    # Replace an earlier assessment's bail/needs rather than stacking a second.
    if any(x.startswith(BAIL_PREFIX) for x in add):
        remove |= {x for x in current if x.startswith(BAIL_PREFIX)}
    if any(x.startswith(NEEDS_PREFIX) for x in add):
        remove |= {x for x in current if x.startswith(NEEDS_PREFIX)}
    to_add = [x for x in add if x not in current]
    to_remove = sorted((remove & current) - set(add))
    return to_add, to_remove


# --- application ------------------------------------------------------------


def progress_path(plan_path: str) -> str:
    return plan_path + ".applied.json"


def load_progress(path: str) -> set[int]:
    try:
        with open(path, encoding="utf-8") as fh:
            return set(json.load(fh))
    except FileNotFoundError:
        return set()


def save_progress(path: str, done: set[int]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sorted(done), fh)


def apply_entry(entry: dict[str, Any], config: RubricConfig) -> str:
    number = str(entry["number"])
    issue = json.loads(gh(["issue", "view", number, "--json", "state,labels,comments"]))
    current = {label["name"] for label in issue.get("labels") or []}
    existing = {c.get("body", "").strip() for c in issue.get("comments") or []}
    if issue["state"] != "OPEN":
        return "skipped: already closed"
    notes = []
    body = entry.get("body")
    if body:
        if config.rewrite_mode == "suggest":
            suggestion = "Suggested agent-ready body (rewrite mode: suggest):\n\n" + body
            if suggestion.strip() not in existing:
                gh(["issue", "comment", number, "--body-file", "-"], stdin=suggestion)
            notes.append("body suggested")
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
                fh.write(body.rstrip() + "\n")
                tmp = fh.name
            try:
                gh(["issue", "edit", number, "--body-file", tmp])
            finally:
                os.unlink(tmp)
            notes.append("body rewritten")
    add, remove = label_changes(entry, current)
    if add or remove:
        args = ["issue", "edit", number]
        if add:
            args += ["--add-label", ",".join(add)]
        if remove:
            args += ["--remove-label", ",".join(remove)]
        gh(args)
    if entry["comment"].strip() not in existing:
        gh(["issue", "comment", number, "--body-file", "-"], stdin=entry["comment"])
    if entry["verdict"] == "stale":
        if config.stale_action == "close":
            gh(["issue", "close", number, "--reason", entry["close_reason"]])
            notes.append(f"closed as {entry['close_reason']}")
        else:
            notes.append("close recommended (stale-action: recommend)")
    return ", ".join(notes) or "labelled"


def preview(plan: dict[str, Any], config: RubricConfig, done: set[int]) -> None:
    print(f"{'#':<7}{'verdict':<14}{'labels':<60}notes")
    for entry in plan["issues"]:
        labels = " ".join(f"+{x}" for x in entry.get("add_labels", []))
        labels += "".join(f" -{x}" for x in entry.get("remove_labels", []))
        notes = []
        if entry.get("body"):
            notes.append("body→comment" if config.rewrite_mode == "suggest" else "body")
        if entry["verdict"] == "stale":
            notes.append(f"close ({entry['close_reason']})" if config.stale_action == "close" else "recommend close")
        if entry["number"] in done:
            notes.append("already applied")
        print(f"#{entry['number']:<6}{entry['verdict']:<14}{labels[:58]:<60}{', '.join(notes)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("plan", help="path to the plan JSON")
    parser.add_argument("--apply", action="store_true", help="mutate GitHub (default: preview only)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with open(args.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Could not read plan {args.plan}: {exc}")
    config = load_config(repo_root())
    labels = {x["name"] for x in json.loads(gh(["label", "list", "--limit", "1000", "--json", "name"]))}
    errors = validate(plan, labels, config.priority_labels)
    if errors:
        fail("The plan is not safe to apply; nothing was changed:\n  " + "\n  ".join(errors))
    progress = progress_path(args.plan)
    done = load_progress(progress)
    preview(plan, config, done)
    if not args.apply:
        print("\nPreview only. Re-run with --apply to make these changes.")
        return 0
    for entry in plan["issues"]:
        if entry["number"] in done:
            continue
        result = apply_entry(entry, config)
        done.add(entry["number"])
        save_progress(progress, done)
        print(f"#{entry['number']}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
