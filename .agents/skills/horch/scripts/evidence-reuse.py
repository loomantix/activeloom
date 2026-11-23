#!/usr/bin/env python3
"""Decide whether a test receipt still covers a new head: `reuse` or `rerun`.

Compares a receipt from an earlier test run with the identity expected at the
new head and prints one JSON object:

    {"decision": "reuse" | "rerun", "reasons": [...], ...}

`reuse` only when every identity field is present, well formed and equal, the
run passed, and every skipped test is allowed by the current gate. Anything
missing, malformed or unknown is `rerun`. The helper neither authenticates the
receipt nor grants review, merge or deployment authority.

Exit codes: 0 reuse, 3 rerun, 2 rerun because an input could not be read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

VERSION = 1
GIT_OBJECT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SHA256 = re.compile(r"[0-9a-f]{64}")
TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

# Identity: every field must match exactly for evidence to carry over.
IDENTITY: dict[str, re.Pattern[str]] = {
    "evidence_class": TOKEN,
    "tree": GIT_OBJECT,
    "command": SHA256,
    "inputs": SHA256,
    "toolchain": SHA256,
    "environment": TOKEN,
}
RECEIPT_ONLY = ("head", "outcome", "skipped")
EXPECTED_ONLY = ("head", "allowed_skips")
RECEIPT_FIELDS = frozenset(("version", *IDENTITY, *RECEIPT_ONLY))
EXPECTED_FIELDS = frozenset(("version", *IDENTITY, *EXPECTED_ONLY))


def _inventory(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and len(set(value)) == len(value)
    )


def problems(document: Any, role: str) -> list[str]:
    """Return `invalid-<role>:<field>` reasons; empty means well formed."""
    if not isinstance(document, dict):
        return [f"invalid-{role}:document"]
    allowed = RECEIPT_FIELDS if role == "receipt" else EXPECTED_FIELDS
    found = [f"unknown-{role}-field:{name}" for name in sorted(set(document) - allowed)]
    version = document.get("version")
    if type(version) is not int or version != VERSION:
        found.append(f"invalid-{role}:version")
    if not (isinstance(document.get("head"), str) and GIT_OBJECT.fullmatch(document["head"])):
        found.append(f"invalid-{role}:head")
    for field, pattern in IDENTITY.items():
        value = document.get(field)
        if not (isinstance(value, str) and pattern.fullmatch(value)):
            found.append(f"invalid-{role}:{field}")
    if role == "receipt":
        if not isinstance(document.get("outcome"), str):
            found.append("invalid-receipt:outcome")
        if not _inventory(document.get("skipped")):
            found.append("invalid-receipt:skipped")
    elif not _inventory(document.get("allowed_skips")):
        found.append("invalid-expected:allowed_skips")
    return found


def compare(receipt: Any, expected: Any) -> dict[str, Any]:
    """Return the decision; any doubt is `rerun` with every reason found."""
    reasons = problems(receipt, "receipt") + problems(expected, "expected")
    if not reasons:
        if receipt["outcome"] != "pass":
            reasons.append("outcome-not-pass")
        for field in IDENTITY:
            if receipt[field] != expected[field]:
                reasons.append(f"{field.replace('_', '-')}-mismatch")
        if set(receipt["skipped"]) - set(expected["allowed_skips"]):
            reasons.append("unapproved-skip")
    result: dict[str, Any] = {
        "decision": "rerun" if reasons else "reuse",
        "reasons": reasons,
    }
    if not reasons:
        result["receipt_head"] = receipt["head"]
        result["expected_head"] = expected["head"]
    return result


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("receipt", type=Path, help="receipt JSON from the earlier run")
    parser.add_argument("expected", type=Path, help="identity JSON built at the new head")
    args = parser.parse_args(argv)
    try:
        receipt, expected = _load(args.receipt), _load(args.expected)
    except (OSError, UnicodeDecodeError, ValueError):
        print(json.dumps({"decision": "rerun", "reasons": ["unreadable-input"]}))
        return 2
    result = compare(receipt, expected)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["decision"] == "reuse" else 3


if __name__ == "__main__":
    sys.exit(main())
