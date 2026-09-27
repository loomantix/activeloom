#!/usr/bin/env python3
"""Apply a reviewed refinement plan to GitHub issues.

Assessors propose; this script applies. It reads a JSON plan (one entry per
issue, shape below), validates the whole plan before touching anything, prints
what it would do, and only mutates with ``--apply``. Mutations run one issue at
a time, in a fixed order — body, labels, comment, close — and each finished
issue is recorded in ``<plan>.applied.json`` so a re-run resumes rather than
double-posting. Split recovery uses the private ``<plan>.children.json`` state
to bind every created child to this exact plan before the first GitHub write.

Interview handbacks add ``decision_comment`` (the exact posted record) and,
for a split, ``children`` with stable keys, titles, bodies and assessments.
See the skill's Interview handback section for the schema and confirmation
flags. Child creation/linking and assessment precede the parent's mutations.

Plan shape::

    {"issues": [{
        "number": 22,
        "verdict": "ready" | "exclude" | "refined-only" | "stale" | "dont-build",
        "add_labels": ["dev: agent", "priority: medium"],
        "remove_labels": [],
        "comment": "Backlog refinement (...): ...",
        "body": "full rewritten body (ready only), or null",
        "close_reason": "completed" | "not planned" (stale/dont-build), or null
    }]}

`agent: refined` is always added. Label hygiene follows the core rubric: a
ready issue loses any `agent-bail:`/`needs:`/`status: blocked` left from an
earlier assessment, and an excluded or stale one loses `dev: agent`. A stale
issue is closed when the local rubric sets `stale-action: close`; handback
closes instead require explicit issue-specific confirmation. Otherwise the
plan's comment stands as the recommendation. With `Rewrite mode:
suggest`, a rewritten body is posted as a comment instead of replacing the body.

    apply-plan.py plan.json            # validate and preview
    apply-plan.py plan.json --apply    # apply, resuming past finished issues
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from typing import Any, Iterator, NoReturn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rubric import (  # noqa: E402
    RubricConfig,
    decision_is_newer,
    latest_decision,
    load_config,
    parse_decision,
    parse_question,
    repo_root,
)

REFINED = "agent: refined"
READY = "dev: agent"
STALE = "agent-bail: stale"
BLOCKED = "status: blocked"
BAIL_PREFIX = "agent-bail:"
NEEDS_PREFIX = "needs:"
GRILL_CLASS_BAILS = frozenset({
    "agent-bail: open-decision",
    "agent-bail: spec-gap",
    "agent-bail: epic",
})
VERDICTS = ("ready", "exclude", "refined-only", "stale", "dont-build")
CLOSE_REASONS = ("completed", "not planned")
TIMEOUT = 60


def fail(message: str) -> NoReturn:
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


def refinement_label(label: str, priority_labels: tuple[str, ...]) -> bool:
    """Whether ``label`` belongs to a family the core rubric lets refinement set."""
    return (
        label in (REFINED, READY, BLOCKED)
        or label.startswith((BAIL_PREFIX, NEEDS_PREFIX))
        or label in priority_labels
    )


def validate(plan: Any, repo_labels: set[str], priority_labels: tuple[str, ...]) -> list[str]:
    """Every problem with the plan; empty means it is safe to apply."""
    if not isinstance(plan, dict) or not isinstance(plan.get("issues"), list):
        return ['The plan must be an object with an "issues" list.']
    errors: list[str] = []
    if REFINED not in repo_labels:
        errors.append(f"label {REFINED!r} does not exist in this repository; every entry adds it")
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
            elif not refinement_label(label, priority_labels):
                errors.append(f"{where}: label {label!r} is not one refinement sets")
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
        if verdict not in ("stale", "dont-build") and close_reason is not None:
            errors.append(f"{where}: close_reason is only for a stale or dont-build verdict")
        if verdict == "dont-build" and close_reason != "not planned":
            errors.append(f"{where}: dont-build needs close_reason 'not planned'")
        bails = [x for x in add if x.startswith(BAIL_PREFIX)]
        needs = [x for x in add if x.startswith(NEEDS_PREFIX)]
        if sum(x in priority_labels for x in add) > 1:
            errors.append(f"{where}: more than one priority label")
        if len(needs) > 1:
            errors.append(f"{where}: more than one needs: label — choose the interview that comes first")
        if verdict == "ready":
            if READY not in add:
                errors.append(f"{where}: ready must add {READY!r}")
            if bails or needs or BLOCKED in add:
                errors.append(f"{where}: ready cannot carry agent-bail:, needs: or {BLOCKED!r} labels")
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
        errors.extend(validate_handback(entry, repo_labels, priority_labels))
    return errors


def validate_handback(entry: dict[str, Any], labels: set[str], priorities: tuple[str, ...]) -> list[str]:
    """Keep each decision outcome consistent with its proposed assessment."""
    where = f"#{entry['number']}"
    errors = []
    text = entry.get("decision_comment")
    decision = parse_decision(text) if isinstance(text, str) else None
    if text is not None and decision is None:
        errors.append(f"{where}: decision_comment must end with a valid grill-decision marker")
    add = entry.get("add_labels", [])
    bails = [x for x in add if x.startswith(BAIL_PREFIX)]
    needs = [x for x in add if x.startswith(NEEDS_PREFIX)]
    verdict = entry["verdict"]
    children = entry.get("children", [])
    if verdict == "dont-build":
        if not decision or decision['outcome'] != 'dont-build':
            errors.append(f"{where}: dont-build needs its decision_comment")
        if any(x.startswith((BAIL_PREFIX, NEEDS_PREFIX)) or x == BLOCKED for x in add):
            errors.append(f"{where}: dont-build carries no bail, needs or blocked label")
    if decision:
        outcome, next_step = decision['outcome'], decision['next']
        if outcome == 'settled' and next_step == 'none':
            errors.append(f"{where}: settled must continue to refine, grill or product-grill")
        if outcome == 'settled' and next_step == 'refine' and needs:
            errors.append(f"{where}: settled with next: refine removes the resolved needs: label")
        if outcome == 'settled' and next_step == 'refine' and not (
            verdict in ('ready', 'stale')
            or (verdict == 'exclude' and len(bails) == 1 and bails[0] not in GRILL_CLASS_BAILS)
        ):
            errors.append(
                f"{where}: settled with next: refine requires ready, stale, or a permanent bail"
            )
        if verdict == 'ready' and (outcome != 'settled' or next_step != 'refine'):
            errors.append(f"{where}: ready requires settled with next: refine")
        if outcome == 'provisional' and (
            verdict != 'exclude'
            or next_step != decision['kind']
            or needs != [f"needs: {decision['kind']}"]
        ):
            errors.append(f"{where}: provisional must stay excluded and return to its interview")
        if outcome == 'dont-build' and (verdict != 'dont-build' or next_step != 'none'):
            errors.append(f"{where}: dont-build requires its verdict and next: none")
        if outcome == 'split' and (not children or next_step != 'refine'):
            errors.append(f"{where}: split requires children and next: refine")
        if outcome != 'split' and children:
            errors.append(f"{where}: only a split decision creates children")
        if next_step in ('grill', 'product-grill') and outcome != 'provisional':
            if verdict != 'exclude' or needs != [f'needs: {next_step}']:
                errors.append(f"{where}: next interview must match the needs: label")
        if needs and outcome != 'provisional':
            comment = entry.get('comment')
            final_line = comment.rstrip().splitlines()[-1] if isinstance(comment, str) and comment.strip() else ''
            question = parse_question(final_line)
            if (not question or needs != [f"needs: {question['kind']}"]
                    or not final_line.startswith(f"Question for /{question['kind']}: ")):
                errors.append(f"{where}: the next gap needs a matching Question for /grill or /product-grill line")
    if not isinstance(children, list):
        return [*errors, f"{where}: children must be a list"]
    if children:
        if verdict != 'exclude' or [x for x in add if x.startswith(BAIL_PREFIX)] != ['agent-bail: epic'] or needs:
            errors.append(f"{where}: a split parent is an excluded epic with no needs: label")
        keys = set()
        for child in children:
            if not isinstance(child, dict):
                errors.append(f"{where}: each child must be an object")
                continue
            key = child.get('key')
            if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', key) or key in keys:
                errors.append(f"{where}: child key must be a unique short lowercase slug")
            else:
                keys.add(key)
            if not all(isinstance(child.get(x), str) and child[x].strip() for x in ('title', 'body')):
                errors.append(f"{where}: each child needs a title and body")
            elif '<!-- refinement-child' in child['body'] or '\n' in child['title']:
                errors.append(f"{where}: child body must not supply a recovery marker; title must be one line")
            assessment = child.get('assessment')
            if not isinstance(assessment, dict) or any(x in assessment for x in ('number', 'children', 'decision_comment')):
                errors.append(f"{where}: child assessment must omit number, children and decision_comment")
            else:
                if assessment.get('verdict') not in ('ready', 'exclude'):
                    errors.append(f"{where}: assess each child as ready or exclude")
                if '<!-- refinement-child' in str(assessment.get('body', '')):
                    errors.append(f"{where}: child assessment must not supply a recovery marker")
                errors.extend(f"{where} child: {e}" for e in validate(
                    {'issues': [{'number': 1, **assessment}]}, labels, priorities))
    return errors


def label_changes(
    entry: dict[str, Any], current: set[str], priority_labels: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """Labels to add and remove, including the rubric's hygiene rules."""
    add = list(dict.fromkeys([*entry.get("add_labels", []), REFINED]))
    remove = set(entry.get("remove_labels", []))
    # An existing priority stands: never stack a second one on it.
    if (current - remove) & set(priority_labels):
        add = [x for x in add if x not in priority_labels or x in current]
    if entry["verdict"] == "ready":
        remove |= {x for x in current if x.startswith((BAIL_PREFIX, NEEDS_PREFIX))} | {BLOCKED}
    else:
        remove.add(READY)
    if entry["verdict"] in ("stale", "dont-build"):
        remove.add(BLOCKED)
    if entry["verdict"] == "dont-build":
        remove |= {x for x in current if x.startswith((BAIL_PREFIX, NEEDS_PREFIX))}
    # Replace an earlier assessment's bail/needs rather than stacking a second;
    # a needs: label only ever accompanies the bail it was set with.
    if any(x.startswith(BAIL_PREFIX) for x in add):
        remove |= {x for x in current if x.startswith((BAIL_PREFIX, NEEDS_PREFIX))}
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


@contextmanager
def plan_apply_lock(plan_path: str) -> Iterator[None]:
    """Serialize one plan's progress and child create-and-record transitions."""
    lock_path = plan_path + ".apply.lock"
    try:
        descriptor = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        fail(f"Could not lock plan application {lock_path}: {exc}")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as exc:
        descriptor.close()
        fail(f"Could not lock plan application {lock_path}: {exc}")
    try:
        yield
    finally:
        descriptor.close()


def child_recovery_path(plan_path: str) -> str:
    return plan_path + ".children.json"


def plan_digest(plan: dict[str, Any]) -> str:
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save_child_recovery(path: str, state: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile(
        "w", dir=directory, prefix=".children-", delete=False, encoding="utf-8"
    ) as fh:
        json.dump(state, fh, sort_keys=True)
        tmp = fh.name
    os.replace(tmp, path)


def load_child_recovery(path: str, plan: dict[str, Any]) -> dict[str, Any] | None:
    keys = [
        f"{entry['number']}:{child['key']}"
        for entry in plan["issues"]
        for child in entry.get("children", [])
    ]
    digest = plan_digest(plan)
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        if not keys:
            return None
        created_state: dict[str, Any] = {
            "plan_sha256": digest,
            "children": {
                key: {"nonce": secrets.token_hex(16), "number": None} for key in keys
            },
        }
        save_child_recovery(path, created_state)
        return created_state
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Could not read child recovery state {path}: {exc}")
    if not isinstance(state, dict):
        fail(f"Child recovery state {path} does not match this plan; use a new plan path")
    records = state.get("children")
    valid = (
        state.get("plan_sha256") == digest
        and isinstance(records, dict)
        and set(records) == set(keys)
        and all(
            isinstance(record, dict)
            and isinstance(record.get("nonce"), str)
            and re.fullmatch(r"[0-9a-f]{32}", record["nonce"])
            and (
                record.get("number") is None
                or (
                    isinstance(record.get("number"), int)
                    and not isinstance(record.get("number"), bool)
                    and record["number"] > 0
                )
            )
            for record in records.values()
        )
    )
    if not valid:
        fail(f"Child recovery state {path} does not match this plan; use a new plan path")
    return state


def check_handback(entry: dict[str, Any], issue: dict[str, Any]) -> None:
    """A draft can be previewed; application needs the recorded, confirmed decision."""
    if not entry.get('decision_comment') and not entry.get('children'):
        return
    if issue.get('assignees'):
        fail(f"#{entry['number']}: assigned since assessment; reassess without reassigning")
    text = entry.get('decision_comment')
    if text:
        decision = parse_decision(text)
        comments = issue.get('comments', [])
        record = latest_decision(comments, decision['kind']) if decision else None
        if (
            not record
            or record[0].get('body', '').strip() != text.strip()
            or not decision_is_newer(comments, record[0], decision['kind'])
        ):
            fail(f"#{entry['number']}: post a current confirmed decision before applying")
        current = {x['name'] for x in issue.get('labels', []) if x['name'].startswith(NEEDS_PREFIX)}
        if decision and decision['outcome'] == 'provisional' and current and current != {f"needs: {decision['kind']}"}:
            fail(f"#{entry['number']}: provisional handback must preserve the current needs: label")


def apply_children(
    entry: dict[str, Any],
    config: RubricConfig,
    recovery: dict[str, Any] | None,
    recovery_path: str | None,
) -> None:
    """Use durable child markers to resume creation/linking without relying on search indexing."""
    children = entry.get('children', [])
    if not children:
        return
    if recovery is None or recovery_path is None:
        fail(f"#{entry['number']}: child recovery state is required before creation")
    issues = json.loads(gh(['issue', 'list', '--state', 'all', '--limit', '10000', '--json', 'number,body']))
    if len(issues) >= 10000:
        fail('Child lookup reached its limit; refusing possible duplicate creation')
    parent = str(entry['number'])
    linked = json.loads(gh(['api', f'repos/{{owner}}/{{repo}}/issues/{parent}/sub_issues', '--paginate', '--slurp']))
    linked_ids = {item['id'] for page in linked for item in page}
    for child in children:
        recovery_key = f"{parent}:{child['key']}"
        record = recovery["children"][recovery_key]
        marker = (
            f"<!-- refinement-child parent: {parent}; key: {child['key']}; "
            f"nonce: {record['nonce']} -->"
        )
        if record['number'] is not None:
            created = json.loads(gh(['api', f"repos/{{owner}}/{{repo}}/issues/{record['number']}"]))
            if marker not in created.get('body', ''):
                fail(f"#{parent}: recorded child {child['key']} lost its recovery marker")
        else:
            matches = [i for i in issues if marker in i.get('body', '')]
            if len(matches) > 1:
                fail(f"#{parent}: multiple children match {child['key']}; reconcile manually")
            if matches:
                created = json.loads(gh(['api', f"repos/{{owner}}/{{repo}}/issues/{matches[0]['number']}"]))
            else:
                created = json.loads(gh(['api', 'repos/{owner}/{repo}/issues', '--method', 'POST', '--input', '-'],
                                       stdin=json.dumps({'title': child['title'], 'body': child['body'].rstrip()+'\n\n'+marker})))
                issues.append(created)
            record['number'] = created['number']
            save_child_recovery(recovery_path, recovery)
        if created['id'] not in linked_ids:
            gh(['api', f'repos/{{owner}}/{{repo}}/issues/{parent}/sub_issues', '--method', 'POST', '--input', '-'],
               stdin=json.dumps({'sub_issue_id': created['id']}))
            linked_ids.add(created['id'])
        assessment = {'number': created['number'], **child['assessment']}
        if assessment.get('body'):
            assessment['body'] = assessment['body'].rstrip()+'\n\n'+marker
        apply_entry(assessment, config, require_open=True)


def apply_entry(
    entry: dict[str, Any],
    config: RubricConfig,
    *,
    close_confirmed: bool = False,
    child_recovery: dict[str, Any] | None = None,
    child_recovery_file: str | None = None,
    require_open: bool = False,
) -> str:
    number = str(entry["number"])
    issue = json.loads(gh(["issue", "view", number, "--json", "state,labels,comments,assignees"]))
    current = {label["name"] for label in issue.get("labels") or []}
    existing = {c.get("body", "").strip() for c in issue.get("comments") or []}
    if issue["state"] != "OPEN":
        if require_open or entry.get('decision_comment') or entry.get('children'):
            fail(f"#{number}: required handback work cannot be applied to a closed issue")
        return "skipped: already closed"
    check_handback(entry, issue)
    if entry['verdict'] == 'dont-build' and not close_confirmed:
        fail(f"#{number}: closing a dont-build decision requires explicit confirmation")
    if entry.get('decision_comment') and entry['verdict'] == 'stale' and not close_confirmed:
        fail(f"#{number}: handback closes require explicit confirmation")
    apply_children(entry, config, child_recovery, child_recovery_file)
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
    add, remove = label_changes(entry, current, config.priority_labels)
    if add or remove:
        args = ["issue", "edit", number]
        if add:
            args += ["--add-label", ",".join(add)]
        if remove:
            args += ["--remove-label", ",".join(remove)]
        gh(args)
    if entry["comment"].strip() not in existing:
        gh(["issue", "comment", number, "--body-file", "-"], stdin=entry["comment"])
    if entry["verdict"] in ("stale", "dont-build"):
        if close_confirmed or config.stale_action == "close":
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
        if entry.get('decision_comment') or entry.get('children'):
            print(json.dumps(entry, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("plan", help="path to the plan JSON")
    parser.add_argument("--apply", action="store_true", help="mutate GitHub (default: preview only)")
    parser.add_argument('--confirm-summary', action='store_true', help='the interview summary was confirmed')
    parser.add_argument('--confirm-handback', action='store_true', help='the displayed handback was confirmed')
    parser.add_argument('--confirm-close', action='append', type=int, default=[], metavar='ISSUE', help='explicitly confirmed close for this issue')
    parser.add_argument('--confirm-split', action='append', type=int, default=[], metavar='ISSUE', help='human-approved split outside an interview')
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
    with plan_apply_lock(args.plan):
        done = load_progress(progress)
        # Validate every confirmation and recorded decision before the first write.
        for entry in plan['issues']:
            if entry['number'] in done:
                continue
            if entry.get('decision_comment'):
                if not args.confirm_summary:
                    fail('Handback requires --confirm-summary; auto never skips summary confirmation')
                if config.grill_handback != 'auto' and not args.confirm_handback:
                    fail('Handback requires --confirm-handback (or local grill-handback: auto)')
                if entry['verdict'] in ('dont-build', 'stale') and entry['number'] not in args.confirm_close:
                    fail(f"#{entry['number']}: handback close requires --confirm-close {entry['number']}")
            elif entry.get('children') and entry['number'] not in args.confirm_split:
                fail(f"#{entry['number']}: child creation requires --confirm-split {entry['number']}")
            if entry.get('decision_comment') or entry.get('children'):
                issue = json.loads(gh(['issue', 'view', str(entry['number']), '--json', 'state,labels,comments,assignees']))
                check_handback(entry, issue)
        recovery_file = child_recovery_path(args.plan)
        child_recovery = load_child_recovery(recovery_file, plan)
        for entry in plan["issues"]:
            if entry["number"] in done:
                continue
            result = apply_entry(
                entry,
                config,
                close_confirmed=entry['number'] in args.confirm_close,
                child_recovery=child_recovery,
                child_recovery_file=recovery_file if child_recovery else None,
            )
            done.add(entry["number"])
            save_progress(progress, done)
            print(f"#{entry['number']}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
