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
        "body": "ready rewrite or explicitly bound existing-child update, or null",
        "close_reason": "completed" | "not planned" (stale/dont-build), or null,
        "evidence": ["file:line, commit or PR that shows the work shipped"],
        "validated": true   # stale only: the orchestrator re-ran the evidence
    }]}

`agent: refined` is always added. Label hygiene follows the core rubric: a
ready issue loses any `agent-bail:`/`needs:`/`status: blocked` left from an
earlier assessment, and an excluded or stale one loses `dev: agent`. A stale
issue is closed when the local rubric sets `stale-action: close` (the default)
and the entry is validated: `validated: true` with non-empty `evidence`, set by
the orchestrator after re-running that evidence against the integration
branch. An unvalidated stale entry is only recommended for close. Handback
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
    comment_time,
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
        if body is not None and ((verdict != "ready" and not entry.get("handback_parent")) or not isinstance(body, str) or not body.strip()):
            errors.append(f"{where}: body is only for a ready verdict, and must be non-empty text")
        close_reason = entry.get("close_reason")
        if verdict == "stale" and close_reason not in CLOSE_REASONS:
            errors.append(f"{where}: stale needs close_reason {' or '.join(map(repr, CLOSE_REASONS))}")
        if verdict not in ("stale", "dont-build") and close_reason is not None:
            errors.append(f"{where}: close_reason is only for a stale or dont-build verdict")
        if verdict == "dont-build" and close_reason != "not planned":
            errors.append(f"{where}: dont-build needs close_reason 'not planned'")
        validated = entry.get("validated", False)
        evidence = entry.get("evidence", [])
        if not isinstance(validated, bool):
            errors.append(f"{where}: validated must be true or false")
        elif validated:
            if verdict != "stale":
                errors.append(f"{where}: validated is only for a stale verdict")
            if not isinstance(evidence, list) or not any(isinstance(x, str) and x.strip() for x in evidence):
                errors.append(f"{where}: a validated stale entry needs non-empty evidence")
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
    return errors if errors else validate_existing_children(plan)


def validate_existing_children(plan: dict[str, Any]) -> list[str]:
    """Validate the complete, explicit scope of an existing-child handback."""
    entries = {e['number']: e for e in plan['issues'] if isinstance(e, dict)
               and type(e.get('number')) is int}
    bound = [e for e in entries.values() if 'existing_children' in e or 'handback_parent' in e]
    if not bound:
        return []
    errors = []
    claimed: set[int] = set()
    for entry in bound:
        number = entry['number']
        children = entry.get('existing_children')
        parent = entry.get('handback_parent')
        if children is not None:
            if (not isinstance(children, list) or not children
                    or any(type(n) is not int or n <= 0 for n in children)):
                errors.append(f'#{number}: existing_children must be non-empty positive issue numbers')
                continue
            if len(set(children)) != len(children) or number in children or parent is not None:
                errors.append(f'#{number}: existing-child ownership must be unique and acyclic')
            text = entry.get('decision_comment')
            decision = parse_decision(text) if isinstance(text, str) else None
            if (decision is None or decision['outcome'] != 'settled' or decision['next'] != 'refine'
                    or entry.get('body') is not None):
                errors.append(f'#{number}: existing coordination parent needs a settled/refine record, not a body rewrite')
            for child in children:
                if child in claimed or child not in entries or entries[child].get('handback_parent') != number:
                    errors.append(f'#{number}: existing child #{child} must occur once and bind this parent')
                claimed.add(child)
        else:
            siblings = entries.get(parent, {}).get('existing_children') if type(parent) is int else None
            if not isinstance(siblings, list) or number not in siblings:
                errors.append(f'#{number}: handback_parent needs its matching coordination entry')
            if entry.get('decision_comment') is not None or not isinstance(entry.get('body'), str) or not entry['body'].strip():
                errors.append(f'#{number}: existing child needs its exact replacement body, not a separate decision')
        add = entry.get('add_labels', [])
        verdict = entry.get('verdict')
        expected = (['agent-bail: epic'] if children is not None
                    else [READY] if verdict == 'ready'
                    else [x for x in add if x.startswith(BAIL_PREFIX)])
        if ((verdict != 'exclude' if children is not None else verdict not in ('ready', 'exclude'))
                or entry.get('children') or entry.get('close_reason') is not None
                or entry.get('remove_labels') or len(expected) != 1
                or set(add) - {REFINED, *expected} or not set(expected) <= set(add)
                or (children is None and verdict == 'exclude' and expected[0] in GRILL_CLASS_BAILS | {STALE})):
            errors.append(f'#{number}: existing handback preserves its ready or excluded state, with no new interview, priority or close')
        if not isinstance(entry.get('expected_body_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', entry['expected_body_sha256']):
            errors.append(f'#{number}: existing handback needs the reviewed current body SHA256')
    if len(bound) != len(entries):
        errors.append('Existing-child handbacks contain only their explicit parents and owned children')
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
            or bool(entry.get('existing_children'))
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
        if decision is None:
            fail(f"#{entry['number']}: post a current confirmed decision before applying")
        comments = issue.get('comments', [])
        record = latest_decision(comments, decision['kind'])
        if (
            not record
            or record[0].get('body', '').strip() != text.strip()
            or not decision_is_newer(comments, record[0], decision['kind'])
        ):
            fail(f"#{entry['number']}: post a current confirmed decision before applying")
        current = {x['name'] for x in issue.get('labels', []) if x['name'].startswith(NEEDS_PREFIX)}
        if decision and decision['outcome'] == 'provisional' and current and current != {f"needs: {decision['kind']}"}:
            fail(f"#{entry['number']}: provisional handback must preserve the current needs: label")


def read_json_object(path: str) -> dict[str, Any]:
    """Read a required private handback artifact without creating or repairing it."""
    try:
        with open(path, encoding='utf-8') as fh:
            value = json.load(fh)
    except (OSError, ValueError) as exc:
        fail(f'Could not read handback artifact {path}: {exc}')
    if not isinstance(value, dict):
        fail(f'Handback artifact {path} must be an object')
    return value


def check_coordination_decision(entry: dict[str, Any], issue: dict[str, Any]) -> None:
    """A settled coordination record must supersede both interview frontiers."""
    check_handback(entry, issue)
    comments = issue.get('comments', [])
    text = entry['decision_comment']
    decision = parse_decision(text)
    record = latest_decision(comments, decision['kind']) if decision else None
    if record is None or comment_time(record[0]) is None:
        fail('Coordination decision has no verified ordering')
    current_time = comment_time(record[0])
    for kind in ('grill', 'product-grill'):
        other = latest_decision(comments, kind)
        if other and other[0].get('body', '').strip() != text.strip():
            other_time = comment_time(other[0])
            if other_time is None or current_time is None or other_time >= current_time:
                fail('A newer or unordered interview decision blocks coordination settlement')
        if not decision_is_newer(comments, record[0], kind):
            fail('An unresolved interview question blocks coordination settlement')


def existing_child_ownership(paths: list[str]) -> dict[tuple[int, int], tuple[str, str]]:
    """Derive ownership from original split plans and their private recovery records."""
    owned: dict[tuple[int, int], tuple[str, str]] = {}
    for path in paths:
        original = read_json_object(path)
        recovery = read_json_object(child_recovery_path(path))
        digest = plan_digest(original)
        if recovery.get('plan_sha256') != digest or not isinstance(original.get('issues'), list):
            fail('Original split plan and private recovery identity do not match')
        records = recovery.get('children')
        if not isinstance(records, dict):
            fail('Original split has no private child records')
        for parent in original['issues']:
            if not isinstance(parent, dict) or type(parent.get('number')) is not int:
                fail('Original split parent is invalid')
            if not parent.get('children'):
                continue
            if not isinstance(parent['children'], list) or parent['number'] not in load_progress(progress_path(path)):
                fail('Existing children require a completed original split, not an unfinished creation intent')
            decision_text = parent.get('decision_comment')
            if decision_text is not None:
                decision = parse_decision(decision_text) if isinstance(decision_text, str) else None
                if decision is None or decision['outcome'] != 'split' or decision['next'] != 'refine':
                    fail('Original ownership decision is not a split')
            for child in parent.get('children', []):
                key = child.get('key') if isinstance(child, dict) else None
                record = records.get(f"{parent['number']}:{key}")
                if (not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', key)
                        or not isinstance(record, dict)
                        or type(record.get('number')) is not int or record['number'] <= 0
                        or not isinstance(record.get('nonce'), str)
                        or not re.fullmatch(r'[0-9a-f]{32}', record['nonce'])):
                    fail('Original split child has no completed private ownership record')
                pair = (parent['number'], record['number'])
                marker = f"<!-- refinement-child parent: {pair[0]}; key: {key}; nonce: {record['nonce']} -->"
                if pair in owned:
                    fail('Existing child has duplicate private ownership records')
                owned[pair] = (marker, plan_digest({'original': digest, 'record': record}))
    return owned


class ExistingHandback:
    """Preflight and journal one exact approved existing-child handback."""

    def __init__(self, plan: dict[str, Any], path: str, originals: list[str],
                 config: RubricConfig, done: set[int]) -> None:
        self.entries = {e['number']: e for e in json.loads(json.dumps(plan))['issues']}
        self.config = config
        self.path = path + '.existing-handback.json'
        self.owned = existing_child_ownership(originals)
        self.markers: dict[int, str] = {}
        proofs = []
        for parent in self.entries.values():
            for child in parent.get('existing_children', []):
                proof = self.owned.get((parent['number'], child))
                if proof is None:
                    fail(f'#{child}: private creation proof for this parent is required')
                self.markers[child] = proof[0]
                proofs.append([parent['number'], child, *proof])
        identity = {
            'plan_sha256': plan_digest(plan),
            'repo': json.loads(gh(['repo', 'view', '--json', 'nameWithOwner']))['nameWithOwner'],
            'ownership_sha256': plan_digest({'proofs': proofs}),
            'rewrite_mode': config.rewrite_mode,
        }
        if os.path.exists(self.path):
            self.state = read_json_object(self.path)
            if any(self.state.get(k) != v for k, v in identity.items()):
                fail('Existing handback identity changed; use a new reviewed plan path')
            for key in ('started', 'done'):
                values = self.state.get(key)
                if (not isinstance(values, list) or any(type(n) is not int for n in values)
                        or len(set(values)) != len(values) or not set(values) <= self.entries.keys()):
                    fail('Existing handback progress is invalid')
            if not set(self.state['done']) <= set(self.state['started']) or not done <= set(self.state['done']):
                fail('Existing handback progress is not bound to this plan')
        else:
            if done:
                fail('Unbound progress cannot authorize an existing-child handback')
            self.state = {**identity, 'started': [], 'done': []}
        self.guard()
        self.save()

    def save(self) -> None:
        """Atomically persist intent/progress before relying on a possibly posted write."""
        save_child_recovery(self.path, self.state)

    def guard(self) -> None:
        """Re-read the whole scope, relationship and decision before each mutation."""
        if json.loads(gh(['repo', 'view', '--json', 'nameWithOwner']))['nameWithOwner'] != self.state['repo']:
            fail('Existing handback repository identity changed')
        for number, entry in self.entries.items():
            issue = json.loads(gh(['issue', 'view', str(number), '--json', 'state,labels,comments,assignees,body']))
            labels = {x['name'] for x in issue.get('labels', [])}
            bails = {x for x in labels if x.startswith(BAIL_PREFIX)}
            expected = {x for x in entry['add_labels'] if x.startswith(BAIL_PREFIX)}
            is_ready = entry['verdict'] == 'ready'
            if (issue.get('state') != 'OPEN' or issue.get('assignees') or bails != expected
                    or REFINED not in labels or (READY in labels) != is_ready
                    or (is_ready and BLOCKED in labels)
                    or any(x.startswith(NEEDS_PREFIX) for x in labels)):
                fail(f'#{number}: existing handback requires OPEN, unassigned, unchanged ready or excluded state and no pending interview')
            body = issue.get('body')
            if not isinstance(body, str):
                fail(f'#{number}: missing current body precondition')
            target = entry['body'].rstrip() + '\n' if entry.get('body') else None
            intended = number in self.state['started'] and self.config.rewrite_mode == 'edit'
            if number in self.state['done'] and intended and target is not None and body != target:
                fail(f'#{number}: completed replacement body changed outside this exact approved handback')
            if hashlib.sha256(body.encode()).hexdigest() != entry['expected_body_sha256'] and not (intended and body == target):
                fail(f'#{number}: body changed outside this exact approved handback')
            if entry.get('existing_children'):
                checked = dict(issue)
                if number in self.state['started']:
                    checked['comments'] = [c for c in issue.get('comments', [])
                                           if c.get('body', '').strip() != entry['comment'].strip()]
                check_coordination_decision(entry, checked)
                pages = json.loads(gh(['api', f'repos/{{owner}}/{{repo}}/issues/{number}/sub_issues', '--paginate', '--slurp']))
                if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                    fail(f'#{number}: incomplete native child inventory')
                if any(not isinstance(row, dict) or type(row.get('number')) is not int for page in pages for row in page):
                    fail(f'#{number}: invalid native child inventory')
                linked = {row['number'] for page in pages for row in page}
                if not set(entry['existing_children']) <= linked:
                    fail(f'#{number}: existing native parent-child relationship changed')
            else:
                marker = self.markers[number]
                if body.count(marker) != 1 or entry['body'].count(marker) != 1:
                    fail(f'#{number}: exact privately recorded child marker must be preserved')
                if re.findall(r'<!-- refinement-child\b.*?-->', body) != re.findall(r'<!-- refinement-child\b.*?-->', entry['body']):
                    fail(f'#{number}: replacement must preserve all child markers exactly')

    def begin(self, number: int) -> None:
        """Record bounded publication intent only after the complete scope passes."""
        self.guard()
        if number not in self.state['started']:
            self.state['started'].append(number)
            self.save()

    def complete(self, number: int) -> None:
        """Verify publication, then save progress before the legacy projection."""
        self.guard()
        entry = self.entries[number]
        issue = json.loads(gh(['issue', 'view', str(number), '--json', 'body,comments']))
        comments = {c.get('body', '').strip() for c in issue.get('comments', [])}
        if entry['comment'].strip() not in comments:
            fail(f'#{number}: refinement comment readback did not match')
        if entry.get('body'):
            prefix = ('Suggested agent-ready body (rewrite mode: suggest):\n\n'
                      if entry['verdict'] == 'ready'
                      else 'Suggested reviewed body (still excluded; rewrite mode: suggest):\n\n')
            if self.config.rewrite_mode == 'edit':
                if issue.get('body') != entry['body'].rstrip() + '\n':
                    fail(f'#{number}: replacement body readback did not match')
            elif (prefix + entry['body']).strip() not in comments:
                fail(f'#{number}: excluded body suggestion readback did not match')
        if number not in self.state['done']:
            self.state['done'].append(number)
            self.save()


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
    existing_handback: ExistingHandback | None = None,
) -> str:
    bound = 'existing_children' in entry or 'handback_parent' in entry
    if bound and existing_handback is None:
        fail('Existing-child entries require full-plan preflight and private ownership proof')
    if existing_handback is not None:
        if entry != existing_handback.entries.get(entry['number']):
            fail('Entry differs from the exact preflighted handback')
        existing_handback.begin(entry['number'])
    number = str(entry["number"])
    fields = 'state,labels,comments,assignees' + (',body' if bound else '')
    issue = json.loads(gh(["issue", "view", number, "--json", fields]))
    current = {label["name"] for label in issue.get("labels") or []}
    existing = {c.get("body", "").strip() for c in issue.get("comments") or []}
    if issue["state"] != "OPEN":
        if require_open or entry.get('decision_comment') or entry.get('children'):
            fail(f"#{number}: required handback work cannot be applied to a closed issue")
        return "skipped: already closed"
    if not bound:
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
            prefix = ('Suggested agent-ready body (rewrite mode: suggest):\n\n'
                      if entry['verdict'] == 'ready'
                      else 'Suggested reviewed body (still excluded; rewrite mode: suggest):\n\n')
            suggestion = prefix + body
            if suggestion.strip() not in existing:
                if existing_handback is not None:
                    existing_handback.guard()
                gh(["issue", "comment", number, "--body-file", "-"], stdin=suggestion)
            notes.append("body suggested")
        elif not bound or issue.get('body') != body.rstrip() + '\n':
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
                fh.write(body.rstrip() + "\n")
                tmp = fh.name
            try:
                if existing_handback is not None:
                    existing_handback.guard()
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
        if existing_handback is not None:
            existing_handback.guard()
        gh(args)
    if entry["comment"].strip() not in existing:
        if existing_handback is not None:
            existing_handback.guard()
        gh(["issue", "comment", number, "--body-file", "-"], stdin=entry["comment"])
    if entry["verdict"] in ("stale", "dont-build"):
        if close_confirmed or auto_closes(entry, config):
            gh(["issue", "close", number, "--reason", entry["close_reason"]])
            notes.append(f"closed as {entry['close_reason']}")
        elif config.stale_action == "close":
            notes.append("close recommended (not validated)")
        else:
            notes.append("close recommended (stale-action: recommend)")
    return ", ".join(notes) or "labelled"


def auto_closes(entry: dict[str, Any], config: RubricConfig) -> bool:
    """A stale entry closes without a per-issue confirmation only when the repo
    allows it and the orchestrator validated the evidence."""
    return (
        entry["verdict"] == "stale"
        and not entry.get("decision_comment")
        and config.stale_action == "close"
        and entry.get("validated") is True
    )


def preview(plan: dict[str, Any], config: RubricConfig, done: set[int]) -> None:
    print(f"{'#':<7}{'verdict':<14}{'labels':<60}notes")
    for entry in plan["issues"]:
        labels = " ".join(f"+{x}" for x in entry.get("add_labels", []))
        labels += "".join(f" -{x}" for x in entry.get("remove_labels", []))
        notes = []
        if entry.get("body"):
            notes.append("body→comment" if config.rewrite_mode == "suggest" else "body")
        if entry["verdict"] == "stale":
            if auto_closes(entry, config):
                notes.append(f"close ({entry['close_reason']})")
            elif config.stale_action == "close":
                notes.append("recommend close (not validated)")
            else:
                notes.append("recommend close")
        if entry["number"] in done:
            notes.append("already applied")
        print(f"#{entry['number']:<6}{entry['verdict']:<14}{labels[:58]:<60}{', '.join(notes)}")
        if entry.get('decision_comment') or entry.get('children') or entry.get('handback_parent'):
            print(json.dumps(entry, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("plan", help="path to the plan JSON")
    parser.add_argument("--apply", action="store_true", help="mutate GitHub (default: preview only)")
    parser.add_argument('--confirm-summary', action='store_true', help='the interview summary was confirmed')
    parser.add_argument('--confirm-handback', action='store_true', help='the displayed handback was confirmed')
    parser.add_argument('--confirm-close', action='append', type=int, default=[], metavar='ISSUE', help='explicitly confirmed close for this issue')
    parser.add_argument('--confirm-split', action='append', type=int, default=[], metavar='ISSUE', help='human-approved split outside an interview')
    parser.add_argument('--existing-child-plan', action='append', default=[], metavar='PLAN',
                        help='original split plan whose private child recovery proves ownership (read-only)')
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
    bound = any('existing_children' in e or 'handback_parent' in e for e in plan['issues'])
    if bound:
        journal = args.plan + '.existing-handback.json'
        if os.path.exists(journal):
            if read_json_object(journal).get('plan_sha256') != plan_digest(plan):
                fail('Existing handback identity changed; use a new reviewed plan path')
        elif done:
            fail('Unbound progress cannot authorize an existing-child handback')
    preview(plan, config, done)
    if not args.apply:
        print("\nPreview only. Re-run with --apply to make these changes.")
        return 0
    with plan_apply_lock(args.plan):
        done = load_progress(progress)
        if bound and (not args.confirm_summary or not args.confirm_handback):
            fail('Existing-child updates require exact --confirm-summary and --confirm-handback, including auto mode')
        # Validate every confirmation and recorded decision before the first write.
        for entry in plan['issues']:
            if bound:
                # The bound path checks every participant together, including retries.
                continue
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
        existing_handback = ExistingHandback(plan, args.plan, args.existing_child_plan, config, done) if bound else None
        if existing_handback is not None:
            done = set(existing_handback.state['done'])
        entries = sorted(plan['issues'], key=lambda e: bool(e.get('existing_children'))) if bound else plan['issues']
        for entry in entries:
            if entry["number"] in done:
                continue
            result = apply_entry(
                entry,
                config,
                close_confirmed=entry['number'] in args.confirm_close,
                child_recovery=child_recovery,
                child_recovery_file=recovery_file if child_recovery else None,
                existing_handback=existing_handback,
            )
            if existing_handback is not None:
                existing_handback.complete(entry['number'])
            done.add(entry["number"])
            save_progress(progress, done)
            print(f"#{entry['number']}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
