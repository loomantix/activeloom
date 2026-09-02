#!/usr/bin/env python3
"""Add a dependency reference between two GitHub issues.

Writes bidirectional refs under a `## Dependencies` section in each issue's
body so that `ready.py` sees the relationship from either side.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import NoReturn

SECTION_HEADER = "## Dependencies"
GH_TIMEOUT_SECONDS = 60


def fail_gh_timeout(action: str) -> NoReturn:
    sys.stderr.write(
        f"Timed out after {GH_TIMEOUT_SECONDS}s while running `gh {action}`. "
        "Check GitHub auth/network connectivity and retry.\n"
    )
    sys.exit(1)


def fetch_body(num: int) -> str:
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(num), "--json", "body"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        fail_gh_timeout(f"issue view {num}")
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout).get("body") or ""


def has_ref(body: str, phrase: str, target: int) -> bool:
    pattern = rf"(?im)^\s*[-*]?\s*{re.escape(phrase)}[:\s]+#{target}\b"
    return bool(re.search(pattern, body))


def add_ref(body: str, line: str) -> str:
    lines = body.splitlines()
    # Find an actual header line, not a prose mention like "see the ## Dependencies section".
    header_idx = next(
        (i for i, raw in enumerate(lines) if raw.strip() == SECTION_HEADER),
        None,
    )
    if header_idx is None:
        prefix = body.rstrip() + "\n\n" if body.strip() else ""
        return f"{prefix}{SECTION_HEADER}\n{line}\n"
    end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    while end > header_idx + 1 and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, line)
    return "\n".join(lines)


def set_body(num: int, body: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as fp:
        fp.write(body)
        path = fp.name
    try:
        try:
            result = subprocess.run(
                ["gh", "issue", "edit", str(num), "--body-file", path],
                capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            fail_gh_timeout(f"issue edit {num}")
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            sys.exit(result.returncode)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def link_issues(source: int, relation: str, target: int) -> int:
    if source == target:
        sys.stderr.write("Cannot link an issue to itself.\n")
        return 1

    if relation == "blocks":
        blocking_num = source
        blocked_num = target
    else:
        blocked_num = source
        blocking_num = target

    # Fetch both issue bodies before making any mutations.
    blocked_body = fetch_body(blocked_num)
    blocking_body = fetch_body(blocking_num)

    blocked_has = has_ref(blocked_body, "Blocked by", blocking_num)
    blocking_has = has_ref(blocking_body, "Blocks", blocked_num)

    if blocked_has and blocking_has:
        print(
            f"#{blocked_num} already has 'Blocked by #{blocking_num}' and "
            f"#{blocking_num} already has 'Blocks #{blocked_num}' — skipping"
        )
        return 0

    # Write the blocked issue first so that ready.py immediately recognizes
    # the dependency and prevents premature execution.
    if not blocked_has:
        current_blocked_body = fetch_body(blocked_num)
        if has_ref(current_blocked_body, "Blocked by", blocking_num):
            print(f"#{blocked_num} already has 'Blocked by #{blocking_num}' — skipping")
        else:
            new_blocked_body = add_ref(
                current_blocked_body, f"- Blocked by #{blocking_num}"
            )
            set_body(blocked_num, new_blocked_body)
            print(f"#{blocked_num}: added 'Blocked by #{blocking_num}'")
    else:
        print(f"#{blocked_num} already has 'Blocked by #{blocking_num}' — skipping")

    # Write the reciprocal blocking reference second.
    if not blocking_has:
        current_blocking_body = fetch_body(blocking_num)
        if has_ref(current_blocking_body, "Blocks", blocked_num):
            print(f"#{blocking_num} already has 'Blocks #{blocked_num}' — skipping")
        else:
            new_blocking_body = add_ref(current_blocking_body, f"- Blocks #{blocked_num}")
            try:
                set_body(blocking_num, new_blocking_body)
                print(f"#{blocking_num}: added 'Blocks #{blocked_num}'")
            except SystemExit as exc:
                if exc.code != 0:
                    sys.stderr.write(
                        f"\nERROR: #{blocked_num} was updated with 'Blocked by #{blocking_num}', "
                        f"but updating #{blocking_num} with 'Blocks #{blocked_num}' failed.\n"
                        f"To repair the reciprocal link manually, run:\n"
                        f"  python3 {sys.argv[0]} {blocking_num} blocks {blocked_num}\n"
                    )
                raise
    else:
        print(f"#{blocking_num} already has 'Blocks #{blocked_num}' — skipping")

    # Verify both final bodies to ensure the link state is consistent.
    final_blocked = fetch_body(blocked_num)
    final_blocking = fetch_body(blocking_num)
    if not has_ref(final_blocked, "Blocked by", blocking_num) or not has_ref(
        final_blocking, "Blocks", blocked_num
    ):
        sys.stderr.write(
            f"ERROR: Dependency link verification failed after update. "
            f"Expected #{blocked_num} to have 'Blocked by #{blocking_num}' and "
            f"#{blocking_num} to have 'Blocks #{blocked_num}'.\n"
            f"To repair, run:\n"
            f"  python3 {sys.argv[0]} {blocking_num} blocks {blocked_num}\n"
        )
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=int, help="issue number")
    parser.add_argument("relation", choices=["blocks", "blocked-by"])
    parser.add_argument("target", type=int, help="other issue number")
    args = parser.parse_args()

    return link_issues(args.source, args.relation, args.target)


if __name__ == "__main__":
    sys.exit(main())
