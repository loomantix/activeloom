#!/usr/bin/env python3
"""Sign-off page for hgrill assumptions blocks: render one local page, serve its return path.

Commands:

  render <ref>... --out-dir <dir> [--repo <owner/name>] [--body <ref>=<file>]...
      <ref> is `<n>` (with --repo) or `<owner/name>#<n>`. Reads each issue's
      assumptions block (the `hgrill-assumptions:v1` JSON copy when present,
      else the legacy prose block) and writes `<dir>/signoff.html`, one tab per
      issue, plus `<dir>/signoff-manifest.json`, which holds identifiers and
      hashes only. `--body` reads a body from a file instead of `gh`.

  serve --dir <dir> [--port 0] [--timeout <seconds>] [--keep-open]
      Serves the page on 127.0.0.1 with a random per-run token and accepts
      `POST /answers`. Each accepted `hgrill-signoff:v1` payload is validated
      against its issue's manifest entry and written to
      `<state>/signoff-answers.json`, where <state> is the hgrill state
      directory for `issue-<n>` (herd-config.py `paths`). Prints one JSON line
      with the URL on startup, then exits once every issue has an answer set
      (unless --keep-open), on timeout, or on SIGINT, printing a second JSON
      line with the stop reason and received issues.

This script never edits an issue, a label or a comment. Exit codes: 0 ok, 1
invalid input or block, 2 usage or `gh` error.

The `hgrill-assumptions:v1` JSON copy sits inside the block markers as
`<!-- hgrill-assumptions:v1 {json} -->`. Its object:

  {"schema": "hgrill-assumptions:v1",
   "items": [{"id": "P1", "kind": "product|assumption|refuted", "title": str,
              "disputed": bool, "options": [{"key": "a", "text": str,
              "engines": str}], "applied": "a"|null, "recommended": "a"|null,
              "passes": str, "evidence": str, "if_reversed": str,
              "if_wrong": str, "reconciled": str, "detail": str}],
   "checked": [str]}

Its snapshot hash is sha256 over the canonical JSON (sorted keys, compact
separators) of that object without any `snapshot_hash` key. A legacy block's
hash is sha256 over the text between the markers with CRLF as LF, trailing
whitespace stripped from each line, and the whole trimmed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

START = "<!-- hgrill-assumptions:start -->"
END = "<!-- hgrill-assumptions:end -->"
JSON_COPY_RE = re.compile(r"<!--\s*hgrill-assumptions:v1\s+(\{.*?\})\s*-->", re.S)
SNAPSHOT_SCHEMA = "hgrill-assumptions:v1"
SIGNOFF_SCHEMA = "hgrill-signoff:v1"
ANSWERS_SCHEMA = "hgrill-signoff-answers:v1"
SOURCE_LEGACY = "legacy-prose-sha256"
SOURCE_JSON = "v1-json"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
REF_RE = re.compile(r"^(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#)?(?P<n>[1-9][0-9]*)\Z")
ITEM_ID_RE = re.compile(r"^[PAR][1-9][0-9]*\Z")
OPTION_KEY_RE = re.compile(r"^[a-z]\Z")
HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")
KINDS = {"product", "assumption", "refuted"}
NOTE_MAX = 4000
BODY_MAX = 256 * 1024
TEXT_FIELDS = ("passes", "evidence", "if_reversed", "if_wrong", "reconciled", "detail")

HERE = Path(__file__).resolve().parent
HERD_CONFIG = HERE.parent.parent / "hreview" / "scripts" / "herd-config.py"


class SignoffError(Exception):
    """Invalid block, snapshot or submission. Exit 1."""


class UsageError(Exception):
    """Bad arguments or a failed `gh` call. Exit 2."""


# --- hashing -------------------------------------------------------------------------


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_block(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def legacy_hash(inner: str) -> str:
    return sha256(normalize_block(inner))


def json_hash(snapshot: dict[str, Any]) -> str:
    return sha256(canonical_json({k: v for k, v in snapshot.items() if k != "snapshot_hash"}))


def item_hash(item: dict[str, Any]) -> str:
    return sha256(canonical_json(item))


# --- block extraction ----------------------------------------------------------------


def extract_block(body: str) -> str:
    """Return the text between the markers; exactly one block must exist."""
    body = body.replace("\r\n", "\n")
    starts, ends = body.count(START), body.count(END)
    if starts == 0 and ends == 0:
        raise SignoffError("no hgrill-assumptions block in the issue body")
    if starts != 1 or ends != 1:
        raise SignoffError("the issue body must hold exactly one hgrill-assumptions block")
    head, _, rest = body.partition(START)
    inner, _, _ = rest.partition(END)
    if END in head:
        raise SignoffError("hgrill-assumptions end marker precedes the start marker")
    return inner


# --- legacy prose parser ---------------------------------------------------------------

SECTION_RES = (
    ("refuted", re.compile(r"^\*\*Refuted and not resolved\b.*\*\*\s*$", re.I)),
    ("product", re.compile(r"^\*\*Product calls\b.*\*\*\s*$", re.I)),
    ("assumption", re.compile(r"^\*\*Assumptions:?\*\*\s*$", re.I)),
    ("checked", re.compile(r"^\*\*Checked, no longer assumptions:?\*\*\s*$", re.I)),
    ("owed", re.compile(r"^\*\*Owed to other issues\b.*\*\*\s*$", re.I)),
)
NUMBERED_RE = re.compile(r"^(\d+)\.\s+(.*)$")
BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
HEADING_RE = re.compile(r"^\*\*[^*].*\*\*:?\s*$")
WARN = "⚠"
LABELS = {
    "Options": "options",
    "Applied now": "applied",
    "Recommended": "recommended",
    "Evidence": "evidence",
    "If reversed": "if_reversed",
    "If wrong": "if_wrong",
    "Reconciled": "reconciled",
}
# The block template's label order; a repeat or a reversal means a label sat inside text.
LABEL_ORDER = ("Options", "Applied now", "Recommended", "Evidence", "If reversed", "Reconciled", "If wrong")
LABEL_RE = re.compile(r"(?:^|(?<=[\s.;)]))(" + "|".join(map(re.escape, LABELS)) + r"):\s*")
OPTION_SPLIT_RE = re.compile(r"(?:^|;\s*)\(([a-z])\)\s*")
ENGINES_TAIL_RE = re.compile(r"\s+[—–]\s+([^—–]+?)\s*$")
LETTER_RE = re.compile(r"^\(([a-z])\)")


def _title_and_rest(text: str) -> tuple[str, str]:
    """Split `**title** rest`; bold titles may contain backticks and arrows."""
    m = re.match(r"^\*\*(.+?)\*\*(.*)$", text, re.S)
    if not m:
        return text.strip(), ""
    return m.group(1).strip(), m.group(2)


def _strip_sentence(text: str) -> str:
    return text.strip().strip("—–").strip().rstrip(".").strip()


def _valid_repo(repo: Any) -> bool:
    return isinstance(repo, str) and bool(REPO_RE.match(repo)) and ".." not in repo.split("/")


def _parse_options(text: str) -> list[dict[str, str]]:
    parts = OPTION_SPLIT_RE.split(text.strip())
    # parts = [prefix, key, body, key, body, ...]
    if len(parts) > 1 and parts[0].strip():
        raise SignoffError(f"unparsed option prefix in: {text.strip()[:80]}")
    options = []
    for key, body in zip(parts[1::2], parts[2::2]):
        body = body.strip().rstrip(";").rstrip(".").strip()
        engines = ""
        tail = ENGINES_TAIL_RE.search(body)
        if tail:
            engines, body = tail.group(1).strip(), body[: tail.start()].strip()
        options.append({"key": key, "text": body, "engines": engines})
    keys = [o["key"] for o in options]
    if len(set(keys)) != len(keys):
        raise SignoffError(f"duplicate option letters in: {text.strip()[:80]}")
    return options


def _parse_item(item_id: str, kind: str, text: str) -> dict[str, Any]:
    text = text.strip()
    disputed = False
    if text.startswith(WARN):
        disputed, text = True, text[len(WARN) :].lstrip("️").strip()
    if kind == "refuted" and text.startswith("❌"):
        text = text[1:].strip()
    title, rest = _title_and_rest(text)
    item: dict[str, Any] = {
        "id": item_id,
        "kind": kind,
        "title": title,
        "disputed": disputed,
        "options": [],
        "applied": None,
        "recommended": None,
        **{f: "" for f in TEXT_FIELDS},
    }
    pieces = LABEL_RE.split(rest)
    lead, labelled = pieces[0], list(zip(pieces[1::2], pieces[2::2]))
    ranks = [LABEL_ORDER.index(label) for label, _ in labelled]
    if any(a >= b for a, b in zip(ranks, ranks[1:])):
        # Fail closed: a split field would drop or misfile text, such as a later option.
        order = ", ".join(label for label, _ in labelled)
        raise SignoffError(f"{item_id}: field labels repeat or are out of order ({order}); is a label inside text?")
    lead = _strip_sentence(lead)
    if kind == "refuted":
        item["detail"] = lead
    elif lead:
        item["passes"] = lead
    for label, value in labelled:
        name = LABELS[label]
        value = value.strip()
        if name == "options":
            item["options"] = _parse_options(value)
            if kind == "product" and not item["options"]:
                raise SignoffError(f"{item_id}: no (a)-style options in: {value[:80]}")
        elif name in ("applied", "recommended"):
            m = LETTER_RE.match(value)
            item[name] = m.group(1) if m else None
            remainder = value[m.end() :] if m else value
            remainder = _strip_sentence(remainder.lstrip(",").strip())
            if name == "recommended" and remainder:
                choice = f" ({item[name]})" if item[name] else ""
                item["detail"] = (item["detail"] + " " if item["detail"] else "") + f"Recommended{choice}, {remainder}"
        else:
            item[name] = _strip_sentence(value)
    keys = {o["key"] for o in item["options"]}
    if kind == "product":
        for name in ("applied", "recommended"):
            if item[name] is not None and item[name] not in keys:
                raise SignoffError(f"{item_id}: {name} ({item[name]}) is not one of the options")
    if kind == "assumption":
        # An assumption's ⚠ marks a reconciled dispute; it never takes option radios.
        item["options"], item["applied"], item["recommended"] = [], None, None
    return item


def parse_legacy(inner: str) -> dict[str, Any]:
    text = normalize_block(inner)
    section: str | None = None
    raw: list[tuple[str, list[str]]] = []
    checked: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        matched = next((name for name, rx in SECTION_RES if rx.match(stripped)), None)
        if matched:
            section = matched
            continue
        if stripped.startswith("Passes:"):
            section = None
            continue
        if section in ("product", "assumption", "refuted"):
            # Fail closed: an unclaimed line here would leave an item off a "complete" sign-off.
            if section == "refuted":
                m, group = BULLET_RE.match(stripped), 1
            else:
                m, group = NUMBERED_RE.match(stripped), 2
            if m:
                raw.append((section, [m.group(group)]))
            elif raw and raw[-1][0] == section and line[:1].isspace():
                raw[-1][1].append(stripped)
            else:
                raise SignoffError(f"unrecognised line in the {section} section: {stripped[:80]}")
        elif stripped.startswith(">"):
            section = None
        elif NUMBERED_RE.match(stripped) or HEADING_RE.match(stripped) or (section is None and BULLET_RE.match(stripped)):
            # An item, bullet or unknown heading outside the item sections would vanish.
            raise SignoffError(f"unrecognised line outside the item sections: {stripped[:80]}")
        elif section == "checked":
            m = BULLET_RE.match(stripped)
            if m:
                checked.append(m.group(1).lstrip("✅").strip())
    counters = {"product": 0, "assumption": 0, "refuted": 0}
    prefix = {"product": "P", "assumption": "A", "refuted": "R"}
    items = []
    for kind, lines in raw:
        counters[kind] += 1
        items.append(_parse_item(f"{prefix[kind]}{counters[kind]}", kind, " ".join(lines)))
    if not items:
        raise SignoffError("the assumptions block has no product calls, assumptions or refuted entries")
    return {"schema": SNAPSHOT_SCHEMA, "items": items, "checked": checked}


# --- v1 JSON copy --------------------------------------------------------------------


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise SignoffError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def loads_strict(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise SignoffError(f"invalid JSON: {exc.msg}") from None


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise SignoffError(f"snapshot schema must be {SNAPSHOT_SCHEMA}")
    items = snapshot.get("items")
    if not isinstance(items, list) or not items:
        raise SignoffError("snapshot items must be a non-empty list")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise SignoffError("snapshot item must be an object")
        iid = item.get("id")
        if not isinstance(iid, str) or not ITEM_ID_RE.match(iid):
            raise SignoffError(f"invalid item id {iid!r}")
        if iid in seen:
            raise SignoffError(f"duplicate item id {iid}")
        seen.add(iid)
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in KINDS or iid[0] != {"product": "P", "assumption": "A", "refuted": "R"}[kind]:
            raise SignoffError(f"{iid}: kind {kind!r} does not match its id")
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            raise SignoffError(f"{iid}: title required")
        options = item.get("options", [])
        if not isinstance(options, list):
            raise SignoffError(f"{iid}: options must be a list")
        keys = []
        for opt in options:
            if not isinstance(opt, dict) or not OPTION_KEY_RE.match(str(opt.get("key", ""))):
                raise SignoffError(f"{iid}: invalid option")
            if not isinstance(opt.get("text", ""), str) or not isinstance(opt.get("engines", ""), str):
                raise SignoffError(f"{iid}: option text must be a string")
            keys.append(opt["key"])
        if len(set(keys)) != len(keys):
            raise SignoffError(f"{iid}: duplicate option keys")
        if options and kind != "product":
            raise SignoffError(f"{iid}: only product calls take options")
        for name in ("applied", "recommended"):
            value = item.get(name)
            if value is not None and value not in keys:
                raise SignoffError(f"{iid}: {name} {value!r} is not an option")
        for name in TEXT_FIELDS:
            if not isinstance(item.get(name, ""), str):
                raise SignoffError(f"{iid}: {name} must be a string")
        if not isinstance(item.get("disputed", False), bool):
            raise SignoffError(f"{iid}: disputed must be true or false")
    checked = snapshot.get("checked", [])
    if not isinstance(checked, list) or not all(isinstance(c, str) for c in checked):
        raise SignoffError("snapshot checked must be a list of strings")
    return snapshot


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": item["id"],
        "kind": item["kind"],
        "title": item["title"],
        "disputed": bool(item.get("disputed", False)),
        "options": [
            {"key": o["key"], "text": o.get("text", ""), "engines": o.get("engines", "")}
            for o in item.get("options", [])
        ],
        "applied": item.get("applied"),
        "recommended": item.get("recommended"),
    }
    for name in TEXT_FIELDS:
        out[name] = item.get(name, "")
    return out


@dataclass
class Snapshot:
    repo: str
    issue: int
    title: str
    source: str
    snapshot_hash: str
    items: list[dict[str, Any]]
    checked: list[str] = field(default_factory=list)


def load_snapshot(repo: str, issue: int, title: str, body: str) -> Snapshot:
    inner = extract_block(body)
    copies = JSON_COPY_RE.findall(inner)
    if len(copies) > 1:
        raise SignoffError(f"the block in {repo}#{issue} holds more than one hgrill-assumptions:v1 copy")
    if copies:
        data = validate_snapshot(loads_strict(copies[0]))
        source, digest = SOURCE_JSON, json_hash(data)
    elif SNAPSHOT_SCHEMA in inner:
        raise SignoffError(f"the block names {SNAPSHOT_SCHEMA} but holds no parseable copy")
    else:
        data = parse_legacy(inner)
        source, digest = SOURCE_LEGACY, legacy_hash(inner)
    items = [_normalize_item(i) for i in data["items"]]
    return Snapshot(repo, issue, title, source, digest, items, list(data.get("checked", [])))


# --- ordering and answer rules ---------------------------------------------------------


def display_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refuted first, then disputed items, then the rest; IDs never change."""

    def rank(item: dict[str, Any]) -> tuple[int, int]:
        if item["kind"] == "refuted":
            return (0, 0)
        return (1 if item["disputed"] else 2, 0 if item["kind"] == "product" else 1)

    return sorted(items, key=rank)  # sorted() is stable: block order within a rank


def allowed_answers(item: dict[str, Any]) -> set[str]:
    if item["kind"] == "refuted":
        return {"decide"}
    if item["kind"] == "assumption":
        return {"confirm", "doubt", "wrong"}
    if item["options"]:
        return {"choose", "amend"} | ({"confirm"} if item["applied"] else set())
    return {"confirm", "reverse", "amend"}


def note_required(item: dict[str, Any], answer: str) -> bool:
    if answer in ("amend", "wrong", "decide"):
        return True
    return answer == "reverse" and not item.get("if_reversed")


def manifest_item(item: dict[str, Any]) -> dict[str, Any]:
    """The identifier-only rule set the server validates against."""
    return {
        "id": item["id"],
        "kind": item["kind"],
        "options": [o["key"] for o in item["options"]],
        "applied": item["applied"],
        "has_if_reversed": bool(item["if_reversed"]),
        "item_hash": item_hash(item),
    }


def _rule_item(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": entry["kind"],
        "options": [{"key": k} for k in entry["options"]],
        "applied": entry["applied"],
        "if_reversed": "x" if entry["has_if_reversed"] else "",
    }


def validate_submission(payload: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Validate one issue's `hgrill-signoff:v1` payload; return its normalized form."""
    if not isinstance(payload, dict):
        raise SignoffError("payload must be an object")
    unknown = set(payload) - {"schema", "repo", "issue", "snapshot_source", "snapshot_hash", "answers"}
    if unknown:
        raise SignoffError(f"unknown payload keys: {', '.join(sorted(unknown))}")
    if payload.get("schema") != SIGNOFF_SCHEMA:
        raise SignoffError(f"schema must be {SIGNOFF_SCHEMA}")
    issue = payload.get("issue")
    if isinstance(issue, bool) or not isinstance(issue, int):
        raise SignoffError("issue must be an integer")
    if payload.get("repo") != entry["repo"] or issue != entry["issue"]:
        raise SignoffError("payload repo or issue does not match this page")
    if payload.get("snapshot_source") != entry["snapshot_source"]:
        raise SignoffError("snapshot_source does not match the rendered snapshot")
    if payload.get("snapshot_hash") != entry["snapshot_hash"]:
        raise SignoffError("snapshot_hash does not match the rendered snapshot; rebuild the page")
    answers = payload.get("answers")
    if not isinstance(answers, list):
        raise SignoffError("answers must be a list")
    rules = {i["id"]: i for i in entry["items"]}
    seen: dict[str, dict[str, Any]] = {}
    for raw in answers:
        if not isinstance(raw, dict):
            raise SignoffError("each answer must be an object")
        extra = set(raw) - {"id", "answer", "option", "note"}
        if extra:
            raise SignoffError(f"unknown answer keys: {', '.join(sorted(extra))}")
        iid = raw.get("id")
        if not isinstance(iid, str) or iid not in rules:
            raise SignoffError(f"unknown item id {iid!r}")
        if iid in seen:
            raise SignoffError(f"duplicate answer for {iid}")
        rule = _rule_item(rules[iid])
        answer, option, note = raw.get("answer"), raw.get("option"), raw.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str) or len(note) > NOTE_MAX:
            raise SignoffError(f"{iid}: note must be a string of at most {NOTE_MAX} characters")
        note = note.strip()
        if answer == "choose" and option is not None and option == rule["applied"]:
            answer, option = "confirm", None  # choosing the applied option confirms it
        if not isinstance(answer, str) or answer not in allowed_answers(rule):
            raise SignoffError(f"{iid}: answer {answer!r} is not allowed here")
        if answer == "choose":
            if not isinstance(option, str) or option not in rules[iid]["options"]:
                raise SignoffError(f"{iid}: choose needs one of the options")
        elif option is not None:
            raise SignoffError(f"{iid}: option is only valid with choose")
        if note_required(rule, answer) and not note:
            raise SignoffError(f"{iid}: a note is required for {answer}")
        seen[iid] = {"id": iid, "answer": answer, "option": option, "note": note}
    missing = [iid for iid in rules if iid not in seen]
    if missing:
        raise SignoffError(f"unanswered items: {', '.join(missing)}")
    return {
        "schema": SIGNOFF_SCHEMA,
        "repo": entry["repo"],
        "issue": entry["issue"],
        "snapshot_source": entry["snapshot_source"],
        "snapshot_hash": entry["snapshot_hash"],
        "answers": [seen[iid] for iid in rules],
    }


# --- page ----------------------------------------------------------------------------

PAGE_CSS = r"""
:root{color-scheme:light dark;--font-sans:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Inter,Roboto,'Helvetica Neue',Arial,sans-serif;--font-mono:ui-monospace,SFMono-Regular,'SF Mono','JetBrains Mono',Menlo,Consolas,'Liberation Mono',monospace;
--n-0:#ffffff;--n-50:#f6f7f9;--n-100:#eceef2;--n-200:#dcdfe6;--n-400:#98a0b0;--n-500:#6b7484;--n-700:#3a4150;--n-800:#262b36;--n-900:#181c24;--n-950:#0f1218;
--indigo-300:#a9b4f5;--indigo-500:#4f5ed4;--indigo-600:#3f4cbb;--amber-400:#d99a2b;--amber-500:#b87a12;--red-400:#e8776f;--red-600:#c03a30;--green-400:#5fbf8a;--green-600:#22815a;
--radius-sm:4px;--radius-md:8px;--radius-full:999px;
--bg:var(--n-50);--surface:var(--n-0);--surface-sunken:var(--n-100);--border:var(--n-200);--border-strong:var(--n-400);--text:var(--n-900);--text-muted:var(--n-500);--accent:var(--indigo-600);--accent-contrast:var(--n-0);--accent-soft:color-mix(in srgb,var(--indigo-500) 12%,transparent);--success:var(--green-600);--warning:var(--amber-500);--danger:var(--red-600)}
@media (prefers-color-scheme:dark){:root:not([data-theme='light']){--bg:var(--n-950);--surface:var(--n-900);--surface-sunken:var(--n-800);--border:var(--n-700);--border-strong:var(--n-500);--text:var(--n-50);--text-muted:var(--n-400);--accent:var(--indigo-300);--accent-contrast:var(--n-950);--accent-soft:color-mix(in srgb,var(--indigo-300) 16%,transparent);--success:var(--green-400);--warning:var(--amber-400);--danger:var(--red-400)}}
:root[data-theme='dark']{--bg:var(--n-950);--surface:var(--n-900);--surface-sunken:var(--n-800);--border:var(--n-700);--border-strong:var(--n-500);--text:var(--n-50);--text-muted:var(--n-400);--accent:var(--indigo-300);--accent-contrast:var(--n-950);--accent-soft:color-mix(in srgb,var(--indigo-300) 16%,transparent);--success:var(--green-400);--warning:var(--amber-400);--danger:var(--red-400)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font-sans);line-height:1.5}
main{max-width:920px;margin:0 auto;padding:16px}h1{font-size:1.3rem;margin:.5rem 0}
.muted{color:var(--text-muted);font-size:.9rem}code{font-family:var(--font-mono);font-size:.88em;background:var(--surface-sunken);padding:0 .25em;border-radius:var(--radius-sm)}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}.tabs button{border:1px solid var(--border);background:var(--surface);color:var(--text);border-radius:var(--radius-full);padding:4px 12px;cursor:pointer;font:inherit}
.tabs button[aria-selected=true]{background:var(--accent);color:var(--accent-contrast);border-color:var(--accent)}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 12px}
.btn{border:1px solid var(--border-strong);background:var(--surface);color:var(--text);border-radius:var(--radius-md);padding:6px 12px;cursor:pointer;font:inherit}
.btn.primary{background:var(--accent);color:var(--accent-contrast);border-color:var(--accent)}.btn:disabled{opacity:.5;cursor:not-allowed}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-md);padding:12px 14px;margin:10px 0}
.card.answered{border-color:var(--success)}.head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.id{font-family:var(--font-mono);font-weight:600}.badge{font-size:.75rem;border-radius:var(--radius-full);padding:0 8px;border:1px solid var(--border)}
.badge.warn{color:var(--warning);border-color:var(--warning)}.badge.bad{color:var(--danger);border-color:var(--danger)}
.title{font-weight:600}.meta{margin:4px 0;font-size:.92rem}.meta b{font-weight:600}
.opt{display:flex;gap:8px;align-items:flex-start;padding:6px 8px;border:1px solid var(--border);border-radius:var(--radius-md);margin:4px 0;cursor:pointer}
.opt.sel{background:var(--accent-soft);border-color:var(--accent)}.choices{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.choices label{border:1px solid var(--border);border-radius:var(--radius-full);padding:2px 10px;cursor:pointer}.choices label.sel{background:var(--accent-soft);border-color:var(--accent)}
textarea{width:100%;min-height:3.2em;margin-top:6px;font:inherit;background:var(--surface-sunken);color:var(--text);border:1px solid var(--border);border-radius:var(--radius-sm);padding:6px}
textarea.need{border-color:var(--danger)}.status{font-size:.92rem}.status.ok{color:var(--success)}.status.err{color:var(--danger)}
pre.copy{white-space:pre-wrap;word-break:break-word;background:var(--surface-sunken);padding:8px;border-radius:var(--radius-sm);font-family:var(--font-mono);font-size:.8rem;max-height:240px;overflow:auto}
details{margin:10px 0}.opt .badge{margin-left:6px}
"""

PAGE_JS = r"""
(function(){
'use strict';
var DATA=JSON.parse(document.getElementById('signoff-data').textContent);
var q=new URLSearchParams(location.search);var TOKEN=q.get('token')||'';
var SERVED=location.protocol==='http:'&&TOKEN!=='';
var root=document.getElementById('app');
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
function inline(parent,text){var parts=String(text).split('`');for(var i=0;i<parts.length;i++){if(i%2===1&&i<parts.length-1){parent.appendChild(el('code',null,parts[i]));}else{parent.appendChild(document.createTextNode((i%2===1?'`':'')+parts[i]));}}return parent;}
function allowed(it){if(it.kind==='refuted')return['decide'];if(it.kind==='assumption')return['confirm','doubt','wrong'];if(it.options.length){var a=['choose','amend'];if(it.applied)a.unshift('confirm');return a;}return['confirm','reverse','amend'];}
function needNote(it,a){return a==='amend'||a==='wrong'||a==='decide'||(a==='reverse'&&!it.if_reversed);}
function draftKey(iss){return 'hgrill-signoff:'+iss.repo+'#'+iss.issue+':'+iss.snapshot_hash;}
function loadDraft(iss){try{var v=localStorage.getItem(draftKey(iss));return v?JSON.parse(v):{};}catch(e){return{};}}
function saveDraft(iss){try{localStorage.setItem(draftKey(iss),JSON.stringify(iss.state));}catch(e){}}
function complete(it,s){if(!s||!s.answer)return false;if(allowed(it).indexOf(s.answer)<0)return false;if(s.answer==='choose'&&!s.option)return false;if(needNote(it,s.answer)&&!(s.note||'').trim())return false;return true;}
function count(iss){var n=0;iss.items.forEach(function(it){if(complete(it,iss.state[it.id]))n++;});return n;}
function bulkable(iss){return iss.items.filter(function(it){var s=iss.state[it.id];if(s&&s.answer)return false;if(it.kind==='refuted')return false;if(it.options.length&&!it.applied)return false;return true;});}
function payload(iss){return{schema:'hgrill-signoff:v1',repo:iss.repo,issue:iss.issue,snapshot_source:iss.snapshot_source,snapshot_hash:iss.snapshot_hash,answers:iss.items.map(function(it){var s=iss.state[it.id]||{};return{id:it.id,answer:s.answer,option:s.answer==='choose'?s.option:null,note:(s.note||'').trim()};})};}
var LABEL={confirm:'Confirm',doubt:'Doubt',wrong:'Wrong',reverse:'Reverse',amend:'Amend',decide:'Decide',choose:'Choose'};
var tabsEl=el('div','tabs');tabsEl.setAttribute('role','tablist');var panes=[];var current=0;
root.appendChild(el('h1',null,'Grill sign-off'));
root.appendChild(el('p','muted',SERVED?'Answers go straight back to the agent on this machine when you submit a tab.':'Opened as a file: submit is unavailable, so copy each tab’s answer block and give it to the agent.'));
root.appendChild(tabsEl);
DATA.issues.forEach(function(iss,ix){
  iss.state=loadDraft(iss);
  var tab=el('button');tab.type='button';tab.setAttribute('role','tab');tabsEl.appendChild(tab);
  var pane=el('section');pane.setAttribute('role','tabpanel');root.appendChild(pane);panes.push({tab:tab,pane:pane});
  tab.addEventListener('click',function(){select(ix);});
  var h=el('h2',null,null);inline(h,iss.repo+'#'+iss.issue+' — '+iss.title);pane.appendChild(h);
  pane.appendChild(el('div','muted','Snapshot '+iss.snapshot_hash.slice(0,12)+' ('+iss.snapshot_source+')'));
  var bar=el('div','bar');pane.appendChild(bar);
  var bulk=el('button','btn');bulk.type='button';bar.appendChild(bulk);
  var progress=el('span','muted');bar.appendChild(progress);
  var cards={};
  iss.items.forEach(function(it){var c=card(iss,it,refresh);cards[it.id]=c;pane.appendChild(c.node);});
  if(iss.checked.length){var d=el('details');d.appendChild(el('summary',null,'Checked, no longer assumptions ('+iss.checked.length+')'));iss.checked.forEach(function(t){d.appendChild(inline(el('div','meta'),'✅ '+t));});pane.appendChild(d);}
  var foot=el('div','bar');pane.appendChild(foot);
  var submit=el('button','btn primary',SERVED?'Submit answers for this issue':'Show answer block');submit.type='button';foot.appendChild(submit);
  var status=el('span','status');foot.appendChild(status);
  var copyWrap=el('div');copyWrap.hidden=true;pane.appendChild(copyWrap);
  var pre=el('pre','copy');var copyBtn=el('button','btn','Copy answer block');copyBtn.type='button';copyWrap.appendChild(copyBtn);copyWrap.appendChild(pre);
  copyBtn.addEventListener('click',function(){try{navigator.clipboard.writeText(pre.textContent).then(function(){status.textContent='Copied.';status.className='status ok';},function(){status.textContent='Copy failed; select the text instead.';status.className='status err';});}catch(e){status.textContent='Copy failed; select the text instead.';status.className='status err';}});
  function showCopy(){pre.textContent='```hgrill-signoff:v1\n'+JSON.stringify(payload(iss),null,2)+'\n```';copyWrap.hidden=false;}
  bulk.addEventListener('click',function(){bulkable(iss).forEach(function(it){var s=iss.state[it.id]||{};s.answer='confirm';s.option=null;iss.state[it.id]=s;cards[it.id].sync();});refresh();});
  submit.addEventListener('click',function(){
    if(iss.sending||count(iss)!==iss.items.length)return;
    if(!SERVED){showCopy();return;}
    iss.sending=true;submit.disabled=true;status.textContent='Sending…';status.className='status';
    var body=JSON.stringify(payload(iss));
    fetch('/answers',{method:'POST',headers:{'Content-Type':'application/json','X-Signoff-Token':TOKEN},body:body})
      .then(function(r){return r.json().catch(function(){return{ok:false,error:'HTTP '+r.status};}).then(function(b){if(!r.ok||!b.ok)throw new Error(b.error||('HTTP '+r.status));});})
      .then(function(){iss.sending=false;status.textContent='Sent to the agent at '+new Date().toLocaleTimeString()+'.';status.className='status ok';iss.sent=body;refresh();})
      .catch(function(e){iss.sending=false;status.textContent='Not sent: '+e.message+'. Copy the block below instead.';status.className='status err';showCopy();refresh();});
  });
  function refresh(){
    saveDraft(iss);
    var n=count(iss),total=iss.items.length,rem=bulkable(iss).length;
    var sent=!!iss.sent&&iss.sent===JSON.stringify(payload(iss));
    if(iss.sent&&!sent&&status.className==='status ok'){status.textContent='Changed since it was sent: submit again.';status.className='status';}
    bulk.textContent='Accept all remaining ('+rem+')';bulk.disabled=rem===0;
    progress.textContent=n+' of '+total+' answered';
    tab.textContent=iss.repo.split('/')[1]+'#'+iss.issue+' · '+n+'/'+total+(sent?' ✓':'');
    submit.disabled=!!iss.sending||n!==total;
    iss.items.forEach(function(it){cards[it.id].sync();});
    if(!copyWrap.hidden)showCopy();
  }
  iss.refresh=refresh;refresh();
});
function select(ix){current=ix;panes.forEach(function(p,i){p.pane.hidden=i!==ix;p.tab.setAttribute('aria-selected',String(i===ix));});}
select(0);
function card(iss,it,refresh){
  var node=el('div','card');var head=el('div','head');node.appendChild(head);
  head.appendChild(el('span','id',it.id));
  if(it.kind==='refuted')head.appendChild(el('span','badge bad','refuted'));
  if(it.disputed)head.appendChild(el('span','badge warn','⚠ disputed'));
  if(it.kind!=='refuted')head.appendChild(el('span','badge',it.kind==='product'?'product call':it.kind));
  node.appendChild(inline(el('div','title'),it.title));
  function meta(label,text){if(!text)return;var m=el('div','meta');m.appendChild(el('b',null,label+': '));inline(m,text);node.appendChild(m);}
  meta(it.kind==='refuted'?'Fact':'Passes',it.kind==='refuted'?it.detail:it.passes);
  meta('Evidence',it.evidence);meta('Reconciled',it.reconciled);
  if(it.kind!=='refuted')meta('Note',it.detail);
  meta('If reversed',it.if_reversed);meta('If wrong',it.if_wrong);
  var optNodes=[];var name='o-'+iss.repo+'#'+iss.issue+'-'+it.id;
  it.options.forEach(function(o){
    var row=el('label','opt');var r=el('input');r.type='radio';r.name=name;row.appendChild(r);
    var t=el('span');t.appendChild(el('b',null,'('+o.key+') '));inline(t,o.text);
    if(o.engines)t.appendChild(el('span','muted',' — '+o.engines));
    if(o.key===it.applied)t.appendChild(el('span','badge','applied'));
    if(o.key===it.recommended)t.appendChild(el('span','badge warn','recommended'));
    row.appendChild(t);node.appendChild(row);optNodes.push({key:o.key,row:row,radio:r});
    r.addEventListener('change',function(){var s=iss.state[it.id]||{};if(o.key===it.applied){s.answer='confirm';s.option=null;}else{s.answer='choose';s.option=o.key;}iss.state[it.id]=s;refresh();});
  });
  var choices=el('div','choices');node.appendChild(choices);var choiceNodes=[];
  allowed(it).forEach(function(a){
    if(it.options.length&&(a==='confirm'||a==='choose'))return;
    var l=el('label');var r=el('input');r.type='radio';r.name=name+'-a';l.appendChild(r);l.appendChild(document.createTextNode(' '+LABEL[a]));choices.appendChild(l);choiceNodes.push({a:a,label:l,radio:r});
    r.addEventListener('change',function(){var s=iss.state[it.id]||{};s.answer=a;s.option=null;iss.state[it.id]=s;refresh();});
  });
  var note=el('textarea');note.placeholder='Note';note.maxLength=4000;node.appendChild(note);
  note.addEventListener('input',function(){var s=iss.state[it.id]||{};s.note=note.value;iss.state[it.id]=s;refresh();});
  function sync(){
    var s=iss.state[it.id]||{};
    optNodes.forEach(function(o){var on=(s.answer==='confirm'&&o.key===it.applied)||(s.answer==='choose'&&s.option===o.key);o.radio.checked=on;o.row.classList.toggle('sel',on);});
    choiceNodes.forEach(function(c){var on=s.answer===c.a;c.radio.checked=on;c.label.classList.toggle('sel',on);});
    if(document.activeElement!==note)note.value=s.note||'';
    var need=!!s.answer&&needNote(it,s.answer);note.placeholder=need?'Note (required)':'Note (optional)';
    note.classList.toggle('need',need&&!(s.note||'').trim());
    node.classList.toggle('answered',complete(it,s));
  }
  return{node:node,sync:sync};
}
})();
"""


def _script_safe_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def page_data(snapshots: list[Snapshot]) -> dict[str, Any]:
    return {
        "issues": [
            {
                "repo": s.repo,
                "issue": s.issue,
                "title": s.title,
                "snapshot_source": s.source,
                "snapshot_hash": s.snapshot_hash,
                "items": display_order(s.items),
                "checked": s.checked,
            }
            for s in snapshots
        ]
    }


def render_page(snapshots: list[Snapshot]) -> str:
    script_hash = base64.b64encode(hashlib.sha256(PAGE_JS.encode("utf-8")).digest()).decode()
    csp = (
        "default-src 'none'; "
        f"script-src 'sha256-{script_hash}'; "
        "style-src 'unsafe-inline'; connect-src 'self'; img-src data:; "
        "base-uri 'none'; form-action 'none'"
    )
    title = "Grill sign-off" if len(snapshots) != 1 else f"Grill sign-off #{snapshots[0].issue}"
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=False)}">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"<title>{html.escape(title)}</title>\n<style>{PAGE_CSS}</style>\n</head>\n<body>\n"
        '<main id="app"></main>\n'
        f'<script type="application/json" id="signoff-data">{_script_safe_json(page_data(snapshots))}</script>\n'
        f"<script>{PAGE_JS}</script>\n</body>\n</html>\n"
    )


def build_manifest(snapshots: list[Snapshot]) -> dict[str, Any]:
    return {
        "schema": "hgrill-signoff-manifest:v1",
        "issues": [
            {
                "repo": s.repo,
                "issue": s.issue,
                "snapshot_source": s.source,
                "snapshot_hash": s.snapshot_hash,
                "items": [manifest_item(i) for i in s.items],
            }
            for s in snapshots
        ],
    }


# --- render CLI ----------------------------------------------------------------------


def parse_ref(ref: str, default_repo: str | None) -> tuple[str, int]:
    m = REF_RE.match(ref)
    if not m:
        raise UsageError(f"issue reference must be <n> or <owner/name>#<n>, got {ref!r}")
    repo = m.group("repo") or default_repo
    if not _valid_repo(repo):
        raise UsageError(f"{ref}: pass --repo <owner/name> or use <owner/name>#<n>")
    return str(repo), int(m.group("n"))


def fetch_issue(repo: str, issue: int) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ["gh", "issue", "view", str(issue), "--repo", repo, "--json", "title,body"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsageError(f"gh issue view {repo}#{issue}: {exc}") from None
    if proc.returncode != 0:
        raise UsageError(f"gh issue view {repo}#{issue} failed: {proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout)
    return data.get("title") or "", data.get("body") or ""


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cmd_render(args: argparse.Namespace) -> dict[str, Any]:
    bodies: dict[tuple[str, int], Path] = {}
    for spec in args.body or []:
        ref, sep, file = spec.partition("=")
        if not sep:
            raise UsageError(f"--body must be <ref>=<file>, got {spec!r}")
        bodies[parse_ref(ref, args.repo)] = Path(file)
    refs = [parse_ref(r, args.repo) for r in args.refs]
    if len(set(refs)) != len(refs):
        raise UsageError("an issue is named twice")
    snapshots = []
    for repo, issue in refs:
        if (repo, issue) in bodies:
            title, body = f"Issue {issue}", bodies[(repo, issue)].read_text(encoding="utf-8")
        else:
            title, body = fetch_issue(repo, issue)
        try:
            snapshots.append(load_snapshot(repo, issue, title, body))
        except SignoffError as exc:
            raise SignoffError(f"{repo}#{issue}: {exc}") from None
    out = Path(args.out_dir)
    _write_private(out / "signoff.html", render_page(snapshots))
    _write_private(out / "signoff-manifest.json", json.dumps(build_manifest(snapshots), indent=2) + "\n")
    return {
        "page": str(out / "signoff.html"),
        "manifest": str(out / "signoff-manifest.json"),
        "issues": [
            {"repo": s.repo, "issue": s.issue, "snapshot_source": s.source, "snapshot_hash": s.snapshot_hash, "items": len(s.items)}
            for s in snapshots
        ],
    }


# --- serve ---------------------------------------------------------------------------


def _load_herd_config() -> Any:
    spec = importlib.util.spec_from_file_location("signoff_herd_config", HERD_CONFIG)
    if spec is None or spec.loader is None or not HERD_CONFIG.is_file():
        raise UsageError(f"herd-config.py not found at {HERD_CONFIG}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def answers_path(repo: str, issue: int) -> Path:
    """`<hgrill state for issue-<n>>/signoff-answers.json`, as herd-config.py resolves it."""
    mod = _load_herd_config()
    try:
        paths = mod.state_paths(repo, "hgrill", f"issue-{issue}")
    except mod.UsageError as exc:
        raise UsageError(str(exc)) from None
    return Path(paths["state"]) / "signoff-answers.json"


def load_manifest(directory: Path) -> tuple[bytes, dict[str, Any]]:
    page = (directory / "signoff.html").read_bytes()
    manifest = loads_strict((directory / "signoff-manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != "hgrill-signoff-manifest:v1":
        raise SignoffError("not a signoff manifest")
    issues = manifest.get("issues")
    if not isinstance(issues, list) or not issues:
        raise SignoffError("manifest must list at least one issue")
    seen: set[tuple[str, int]] = set()
    for entry in issues:
        if not isinstance(entry, dict):
            raise SignoffError("manifest entry is malformed")
        repo, issue = entry.get("repo"), entry.get("issue")
        if (
            not _valid_repo(repo)
            or isinstance(issue, bool)
            or not isinstance(issue, int)
            or issue <= 0
            or entry.get("snapshot_source") not in {SOURCE_LEGACY, SOURCE_JSON}
            or not isinstance(entry.get("snapshot_hash"), str)
            or not HASH_RE.match(entry["snapshot_hash"])
        ):
            raise SignoffError("manifest entry is malformed")
        key = (str(repo), issue)
        if key in seen:
            raise SignoffError(f"duplicate manifest entry for {repo}#{issue}")
        seen.add(key)
        items = entry.get("items")
        if not isinstance(items, list) or not items:
            raise SignoffError("manifest entry must list at least one item")
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not ITEM_ID_RE.match(item["id"])
                or not isinstance(item.get("kind"), str)
                or item["kind"] not in KINDS
                or not isinstance(item.get("options"), list)
                or not all(isinstance(k, str) and OPTION_KEY_RE.match(k) for k in item["options"])
                or (item.get("applied") is not None and item.get("applied") not in item["options"])
                or not isinstance(item.get("has_if_reversed"), bool)
            ):
                raise SignoffError("manifest item is malformed")
    return page, manifest


class SignoffServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        page: bytes,
        manifest: dict[str, Any],
        token: str,
        port: int = 0,
        resolve_path: Any = answers_path,
    ) -> None:
        super().__init__(("127.0.0.1", port), SignoffHandler)
        self.page = page
        self.entries = {(e["repo"], e["issue"]): e for e in manifest["issues"]}
        self.token = token
        self.resolve_path = resolve_path
        self.received: set[tuple[str, int]] = set()
        self.lock = threading.Lock()
        self.all_done = threading.Event()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def store(self, normalized: dict[str, Any]) -> bool:
        """Write one issue's answers; return True once every issue has answers."""
        key = (normalized["repo"], normalized["issue"])
        record = {
            "schema": ANSWERS_SCHEMA,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "submission": normalized,
        }
        with self.lock:
            _write_private(self.resolve_path(*key), json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            self.received.add(key)
            return self.received >= set(self.entries)


class SignoffHandler(BaseHTTPRequestHandler):
    server: SignoffServer
    server_version = "hgrill-signoff"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return  # request lines carry the token; log nothing

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._send(status, json.dumps(value).encode("utf-8"), "application/json")

    def _host_ok(self) -> bool:
        # Refuse DNS-rebinding hosts: only the loopback names on this port.
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _token_ok(self, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(str(presented).encode(), self.server.token.encode())

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if not self._host_ok():
            return self._json(403, {"ok": False, "error": "bad host"})
        parts = urlsplit(self.path)
        if parts.path != "/":
            return self._json(404, {"ok": False, "error": "not found"})
        token = (parse_qs(parts.query).get("token") or [None])[0]
        if not self._token_ok(token):
            return self._json(403, {"ok": False, "error": "bad token"})
        self._send(200, self.server.page, "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        if not self._host_ok():
            return self._json(403, {"ok": False, "error": "bad host"})
        if urlsplit(self.path).path != "/answers":
            return self._json(404, {"ok": False, "error": "not found"})
        if not self._token_ok(self.headers.get("X-Signoff-Token")):
            return self._json(403, {"ok": False, "error": "bad token"})
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{name}:{self.server.port}" for name in ("127.0.0.1", "localhost")}:
            return self._json(403, {"ok": False, "error": "bad origin"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"ok": False, "error": "content type must be application/json"})
        try:
            length = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            length = -1
        if length < 0 or length > BODY_MAX:
            return self._json(413, {"ok": False, "error": "body missing or too large"})
        raw = self.rfile.read(length)
        try:
            payload = loads_strict(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise SignoffError("payload must be an object")
            repo, issue = payload.get("repo"), payload.get("issue")
            if not isinstance(repo, str) or isinstance(issue, bool) or not isinstance(issue, int):
                raise SignoffError("payload repo must be a string and issue must be an integer")
            entry = self.server.entries.get((repo, issue))
            if entry is None:
                return self._json(404, {"ok": False, "error": "issue is not on this page"})
            normalized = validate_submission(payload, entry)
        except (SignoffError, UnicodeDecodeError) as exc:
            status = 409 if "snapshot_hash" in str(exc) else 400
            return self._json(status, {"ok": False, "error": str(exc)})
        try:
            done = self.server.store(normalized)
        except (OSError, UsageError) as exc:
            return self._json(500, {"ok": False, "error": f"could not store answers: {exc.__class__.__name__}"})
        try:
            self._json(200, {"ok": True, "repo": normalized["repo"], "issue": normalized["issue"]})
        finally:
            if done:
                # After the 200 is written, or its write failed: `serve` exits on this event.
                self.server.all_done.set()


def cmd_serve(args: argparse.Namespace) -> dict[str, Any]:
    page, manifest = load_manifest(Path(args.dir))
    for entry in manifest["issues"]:
        answers_path(entry["repo"], entry["issue"])  # fail before listening if paths can't resolve
    server = SignoffServer(page, manifest, secrets.token_urlsafe(32), args.port)
    print(json.dumps({"url": server.url(), "port": server.port, "issues": len(server.entries)}), flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + args.timeout
    reason = "timeout"
    try:
        while time.monotonic() < deadline:
            if not args.keep_open and server.all_done.wait(0.5):
                reason = "complete"
                break
            if args.keep_open:
                time.sleep(0.5)
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        server.shutdown()
        server.server_close()
    received = sorted(f"{r}#{n}" for r, n in server.received)
    return {"stopped": reason, "received": received}


# --- main ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signoff.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="render the sign-off page")
    render.add_argument("refs", nargs="+", metavar="ref")
    render.add_argument("--repo")
    render.add_argument("--out-dir", required=True)
    render.add_argument("--body", action="append", metavar="REF=FILE")
    serve = sub.add_parser("serve", help="serve the page and accept answers on 127.0.0.1")
    serve.add_argument("--dir", required=True)
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--timeout", type=float, default=4 * 3600)
    serve.add_argument("--keep-open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = cmd_render(args) if args.command == "render" else cmd_serve(args)
    except SignoffError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    except (UsageError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
