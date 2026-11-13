#!/usr/bin/env python3
"""Read-only CI gate for a staging PR, including repositories without required checks."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=60, check=False,
    )


def pr_identity(repo: str, pr: str) -> dict[str, str]:
    fields = ("headRefOid", "headRefName", "mergeable", "mergeStateStatus")
    result = gh("pr", "view", pr, "--repo", repo, "--json", ",".join(fields))
    if result.returncode:
        raise ValueError("Could not read PR identity")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) and data[key] for key in fields
    ):
        raise ValueError("Invalid PR identity response")
    return {key: data[key] for key in fields}


def checks(repo: str, pr: str, branch: str, *, required: bool) -> list[dict[str, str]]:
    args = ["pr", "checks", pr, "--repo", repo, "--json", "bucket,name,state"]
    if required:
        args.append("--required")
    result = gh(*args)
    # gh reports the empty required subset as an error, even when CI exists.
    # Match only that complete diagnostic; other errors remain failures.
    if (
        required
        and result.returncode == 1
        and not result.stdout.strip()
        and result.stderr.strip() == f"no required checks reported on the '{branch}' branch"
    ):
        return []
    if result.returncode not in (0, 8):
        raise ValueError("Could not read checks or a check failed")
    if result.returncode == 8:
        raise ValueError("Checks are still pending")
    data = json.loads(result.stdout)
    if not isinstance(data, list) or not all(
        isinstance(item, dict)
        and all(isinstance(item.get(key), str) for key in ("bucket", "name", "state"))
        for item in data
    ):
        raise ValueError("Invalid checks response")
    # Skips remain provisional until verify reads same-head mergeability after CI.
    allowed = {"pass", "skipping"}
    if any(item["bucket"] not in allowed for item in data):
        raise ValueError("Checks are not acceptable for merge")
    return data


def verify(repo: str, pr: str, head: str) -> dict[str, object]:
    identity = pr_identity(repo, pr)
    if identity["headRefOid"] != head:
        raise ValueError("PR head changed; refresh the ship preflight")
    required = checks(repo, pr, identity["headRefName"], required=True)
    available = checks(repo, pr, identity["headRefName"], required=False)
    if not available:
        raise ValueError("No available CI evidence; cannot verify checks")
    final = pr_identity(repo, pr)
    if any(final[key] != identity[key] for key in ("headRefOid", "headRefName")):
        raise ValueError("PR head changed while checking CI")
    if final["mergeable"] != "MERGEABLE" or final["mergeStateStatus"] not in {"CLEAN", "HAS_HOOKS"}:
        raise ValueError("PR is not mergeable with CLEAN or HAS_HOOKS state")
    return {"head": head, "required_count": len(required), "available_count": len(available)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.repo, args.pr, args.head)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"CI gate refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
