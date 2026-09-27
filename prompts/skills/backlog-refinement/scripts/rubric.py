"""Read a repository's backlog-refinement settings from its local rubric.

Shared by `candidates.py`, `precheck.py`, and `apply-plan.py`. Settings live in
`.backlog/refinement.local.md` at the repository root, falling back to a legacy
per-harness `RUBRIC.md`. Every default that stands in for a missing setting is
announced on stderr, because a silent default changes what refinement does
without anyone noticing.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from typing import NamedTuple

LOCAL_RUBRIC = os.path.join(".backlog", "refinement.local.md")
LEGACY_HARNESS_ROOTS = (".claude", ".codex", ".agents")
STALE_ACTIONS = ("recommend", "close")
REWRITE_MODES = ("edit", "suggest")


def marker(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?m)^[ \t]*<!--\s*{name}:\s*(.*?)\s*-->[ \t]*$")


# Issues a scheduled workflow both OPENS and CLOSES. They are never refinement
# tasks, and a refinement comment on one resets its `updatedAt` — which can
# DELAY that auto-close.
AUTO_MANAGED_MARKER = marker("auto-managed-labels")
# The repository's four priority label names, highest first. Empty disables
# priority-setting.
PRIORITY_MARKER = marker("priority-labels")
# Title prefixes that record a priority someone already set, highest first and
# positionally matched to the priority labels (e.g. `[P0], [P1], [P2], [P3]`).
TITLE_PREFIX_MARKER = marker("priority-title-prefixes")
# `close` lets refinement close a verified-stale issue itself; `recommend`
# (the default) only recommends it.
STALE_ACTION_MARKER = marker("stale-action")
GRILL_HANDBACK_MARKER = marker("grill-handback")

# The template's settings bullets: "- **Integration branch:** `staging`".
_SETTING = r"(?m)^\s*-\s*\*\*{label}:\*\*\s*`([^`]+)`"
_INTEGRATION_BRANCH = re.compile(_SETTING.format(label="Integration branch"))
_REWRITE_MODE = re.compile(_SETTING.format(label="Rewrite mode"))


class RubricConfig(NamedTuple):
    """Repository settings read from the local (or legacy) rubric."""

    source: str | None
    auto_managed_labels: tuple[str, ...]
    priority_labels: tuple[str, ...]
    title_prefixes: tuple[str, ...] = ()
    stale_action: str = "recommend"
    integration_branch: str | None = None
    rewrite_mode: str = "edit"
    grill_handback: str = "ask"


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


def read_marker(pattern: re.Pattern[str], name: str, text: str, path: str) -> tuple[str, ...] | None:
    """Values listed in a single-line marker; None when the marker is absent."""
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
    return tuple(value.strip() for value in matches[0].split(",") if value.strip())


def _setting(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None or "TODO(backlog)" in match.group(1):
        return None
    return match.group(1).strip()


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
    auto_managed = read_marker(AUTO_MANAGED_MARKER, "auto-managed-labels", text, path) or ()
    priority = read_marker(PRIORITY_MARKER, "priority-labels", text, path)
    if priority is None:
        sys.stderr.write(
            f"NOTE: no priority-labels marker in {path}; priority backfill is off.\n"
        )
        priority = ()
    prefixes = read_marker(TITLE_PREFIX_MARKER, "priority-title-prefixes", text, path) or ()
    if prefixes and len(prefixes) != len(priority):
        sys.stderr.write(
            f"The priority-title-prefixes marker in {path} lists {len(prefixes)} prefixes "
            f"for {len(priority)} priority labels; ignoring it. List one prefix per label.\n"
        )
        prefixes = ()
    stale = read_marker(STALE_ACTION_MARKER, "stale-action", text, path)
    stale_action = stale[0] if stale else "recommend"
    if stale_action not in STALE_ACTIONS:
        sys.stderr.write(
            f"Unknown stale-action {stale_action!r} in {path}; expected one of "
            f"{', '.join(STALE_ACTIONS)}.\n"
        )
        sys.exit(1)
    rewrite_mode = _setting(_REWRITE_MODE, text)
    if rewrite_mode is None:
        sys.stderr.write(
            f"NOTE: no Rewrite mode setting in {path}; using `edit`, which rewrites "
            "issue bodies. Write it as - **Rewrite mode:** `edit` or `suggest`.\n"
        )
        rewrite_mode = "edit"
    if rewrite_mode not in REWRITE_MODES:
        sys.stderr.write(
            f"Unknown rewrite mode {rewrite_mode!r} in {path}; expected one of "
            f"{', '.join(REWRITE_MODES)}.\n"
        )
        sys.exit(1)
    handback = read_marker(GRILL_HANDBACK_MARKER, "grill-handback", text, path)
    if handback is not None and handback not in (("ask",), ("auto",)):
        sys.stderr.write(f"Invalid grill-handback in {path}; expected ask or auto.\n")
        sys.exit(1)
    return RubricConfig(
        path,
        auto_managed,
        priority,
        prefixes,
        stale_action,
        _setting(_INTEGRATION_BRANCH, text),
        rewrite_mode,
        handback[0] if handback else "ask",
    )


def title_priority(title: str, config: RubricConfig) -> str | None:
    """The priority label a recognised title prefix maps to, if any."""
    stripped = title.lstrip()
    for prefix, label in zip(config.title_prefixes, config.priority_labels):
        if stripped.startswith(prefix):
            return label
    return None


DECISION_MARKER = re.compile(
    r"<!-- grill-decision kind: (?P<kind>grill|product-grill); "
    r"outcome: (?P<outcome>settled|provisional|dont-build|split); "
    r"next: (?P<next>refine|grill|product-grill|none) -->"
)
QUESTION = re.compile(
    r"^Question for\s+(?:the\s+)?/?(?P<kind>product-grill|grill)\s*:?\s*(?P<question>.*)$",
    re.IGNORECASE,
)


def prose_lines(text: str) -> list[str]:
    """Ignore quoted and fenced examples when reading protocol records."""
    lines = []
    fence = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            if not fence:
                fence = stripped[:3]
            elif stripped.startswith(fence):
                fence = ""
            continue
        if not fence and not stripped.startswith(">"):
            lines.append(stripped)
    return lines


def parse_decision(text: str) -> dict[str, str] | None:
    """A decision record ends with one valid marker outside a code example."""
    lines = [line for line in prose_lines(text) if line]
    if len(lines) < 2 or lines[-1] != text.strip().splitlines()[-1].strip():
        return None
    match = DECISION_MARKER.fullmatch(lines[-1])
    if not match or sum("<!-- grill-decision" in line for line in lines) != 1:
        return None
    return match.groupdict()


def parse_question(text: str, kind: str = "grill", *, legacy_tail: bool = False) -> dict[str, str] | None:
    """Read the fixed line, older bold/heading shapes, or a refinement's final question."""
    lines = [line for line in prose_lines(text) if line]
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].lstrip("# -*").replace("**", "").replace("`", "")
        match = QUESTION.fullmatch(line)
        if match:
            question = match["question"].strip()
            if not question and index + 1 < len(lines):
                question = lines[index + 1]
            if question:
                return {"kind": match["kind"].lower(), "question": question}
    if legacy_tail and lines and lines[-1].endswith("?"):
        return {"kind": kind, "question": lines[-1]}
    return None


def comment_time(comment: dict[str, Any]) -> datetime | None:
    value = comment.get("updatedAt") or comment.get("createdAt")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def latest_decision(
    comments: list[dict[str, Any]], kind: str
) -> tuple[dict[str, Any], dict[str, str]] | None:
    """Newest valid decision from the interview that owns the active question."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    records = [
        (comment, decision)
        for comment in comments
        if (decision := parse_decision(comment.get("body", "")))
        and decision["kind"] == kind
    ]
    return max(records, key=lambda item: comment_time(item[0]) or floor) if records else None


def grill_context(body: str, comments: list[dict[str, Any]], kind: str = "grill") -> dict[str, Any]:
    """Question and decision evidence; a newer decision is not a readiness verdict."""
    refinements = [c for c in comments if any(
        re.match(r"^(?:#+\s*)?(?:\*\*)?(?:Backlog refinement|Refined for agent-loop)\b", line, re.I)
        or QUESTION.match(line.lstrip("# -*").replace("**", ""))
        for line in prose_lines(c.get("body", ""))
    ) and parse_decision(c.get("body", "")) is None]
    floor = datetime.min.replace(tzinfo=timezone.utc)
    latest = max(refinements, key=lambda c: comment_time(c) or floor, default=None)
    question = parse_question(latest.get("body", ""), kind, legacy_tail=True) if latest else None
    if question is None:
        question = parse_question(body, kind)
    record = latest_decision(comments, question["kind"] if question else kind)
    decision, parsed = record if record else (None, None)
    decision_at = comment_time(decision) if decision else None
    refinement_at = comment_time(latest) if latest else None
    newer = bool(decision_at and (not latest or (refinement_at and decision_at > refinement_at)))
    return {
        "question": question,
        "decision": {**(parsed or {}), "url": decision.get("url"),
                     "createdAt": decision.get("createdAt")} if decision else None,
        "newer_decision": newer,
    }
