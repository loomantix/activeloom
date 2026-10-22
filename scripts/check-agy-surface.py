#!/usr/bin/env python3
"""Check the real pinned Agy surface without launching a reviewer."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOTS = (".agents", ".claude", ".codex")
LEDGER = "skills/critique/scripts/review-ledger.version"
REPAIR = (
    "Commit A must bump and vendor the ledger; commit B must pin A in both "
    "pinned launchers and AGY_SURFACE_SHA in tests/test_agy_review_launcher.py. "
    "Use a merge commit to preserve A. With squash merges, land the ledger first "
    "and move the pin in a separate PR before distributing. See CONTRIBUTING.md."
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return result.stdout.strip()


def check(repo: Path, ref: str | None = None) -> str:
    def read(path: str) -> str:
        return git(repo, "show", f"{ref}:{path}") if ref else (repo / path).read_text().strip()

    pins = set()
    versions = set()
    for root in ROOTS:
        # The native .agents agent-loop launcher trusts the fetched base, not
        # a companion pin. Its ledger must still agree with the relay surface.
        if root != ".agents":
            path = f"{root}/skills/critique/scripts/run-agy-review.sh"
            matches = re.findall(r'^agy_surface_sha="([0-9a-f]{40})"$', read(path), re.MULTILINE)
            if len(matches) != 1:
                raise ValueError(f"{path}: expected exactly one full commit pin")
            pins.add(matches[0])
        version = read(f"{root}/{LEDGER}")
        if not version:
            raise ValueError(f"{root}/{LEDGER}: empty ledger version")
        versions.add(version)
    if len(pins) != 1:
        raise ValueError("Agy launcher pins differ across harnesses")
    if len(versions) != 1:
        raise ValueError("Engine ledger versions differ across harnesses")
    pin = pins.pop()
    try:
        surface = git(repo, "show", f"{pin}:.agents/{LEDGER}")
    except ValueError as error:
        raise ValueError(
            f"Cannot read pinned surface {pin}; fetch full history (fetch-depth: 0) "
            f"or fetch that SHA. {error}"
        ) from error
    current = versions.pop()
    if surface != current:
        raise ValueError(f"Pinned surface {pin} ledger is {surface}; engine ledger is {current}")
    return f"Agy surface {pin}: all three engines match ledger {current}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--ref", help="Check a release commit instead of working files")
    args = parser.parse_args()
    try:
        print(check(args.repo, args.ref))
    except (OSError, ValueError) as error:
        print(f"Agy surface check failed: {error}\n{REPAIR}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
