#!/usr/bin/env python3
"""Create a verified commit via the GitHub Contents API.

Replaces `git commit` + `git push` in the upstream-sync workflow.
Commits created via the API endpoints (`git/blobs`, `git/trees`,
`git/commits`, `git/refs`) are auto-signed by GitHub when invoked with
a GitHub App installation token — the resulting commit shows
`committer: GitHub` and `verified: true`, satisfying SOC 2 (and similar)
controls that require human-or-attested-actor sign-off on every change.

Why this exists:
- `git commit` from inside a workflow runner produces unsigned commits
  attributed to `github-actions[bot]`. Audit frameworks (SOC 2, ISO 27001,
  etc.) flag these because the commit lacks cryptographic attestation
  tied to an attested identity.
- The same workflow using the GitHub Contents API + a GitHub App's
  installation token produces commits that are audit-clean: signed,
  attributed to a known App identity, and audit-traceable.

Usage (called from sync-from-upstream.yml after the sync engine writes
files to the consumer working tree):

    python3 create-signed-commit.py \\
        --owner <owner> --repo <repo> \\
        --base-branch <branch> \\
        --new-branch <branch> \\
        --message "<commit message>" \\
        --consumer-dir <path> \\
        --token-env GH_APP_TOKEN

Inputs:
- The consumer working directory has the modifications already on disk
  (the sync engine already wrote them).
- The token-env var holds an App installation token (generated upstream
  via actions/create-github-app-token).

Outputs:
- A new branch ref pointing at a signed commit. The workflow then opens
  a PR against that branch.

Exit codes: 0 on success, 1 on API error, 2 on bad invocation.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple, cast


class StatusChanges(NamedTuple):
    """Result of `parse_status`: paths to upsert + paths to delete."""

    upserts: list[str]
    deletes: list[str]


def run(*args: str, cwd: Path | None = None) -> str:
    """Run a shell command and return stdout. Exit on failure."""
    res = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(f"command failed ({res.returncode}): {' '.join(args)}\n{res.stderr}")
        sys.exit(1)
    return res.stdout


def _github_request(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    """Internal: issue a GitHub REST request. Returns parsed JSON, or raises HTTPError.

    Callers should use `github_api` (errors are fatal) or `github_api_optional`
    (404 returns None, other errors fatal) — both surface a clear contract at
    the call site. The Contents API endpoints this script hits always return
    JSON objects; a non-object response is treated as a hard error here so
    callers can rely on a `dict[str, Any]` shape downstream.
    """
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    # Bound network wait — a hung connection on the runner shouldn't
    # consume the entire workflow timeout (5 min default).
    with urllib.request.urlopen(req, timeout=30) as resp:
        parsed = json.loads(resp.read())
    if not isinstance(parsed, dict):
        sys.stderr.write(
            f"GitHub API {method} {path}: expected JSON object, got {type(parsed).__name__}\n"
        )
        sys.exit(1)
    return cast(dict[str, Any], parsed)


def _exit_on_http_error(method: str, path: str, e: urllib.error.HTTPError) -> None:
    sys.stderr.write(f"GitHub API {method} {path}: {e.code} {e.reason}\n")
    try:
        sys.stderr.write(e.read().decode() + "\n")
    except Exception:
        pass
    sys.exit(1)


def github_api(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Issue a GitHub REST request. Returns parsed JSON. Exits on any error."""
    try:
        return _github_request(method, path, token, body)
    except urllib.error.HTTPError as e:
        _exit_on_http_error(method, path, e)
        raise  # unreachable; satisfies the type checker


def github_api_optional(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Issue a GitHub REST request. Returns None on 404. Exits on other errors."""
    try:
        return _github_request(method, path, token, body)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        _exit_on_http_error(method, path, e)
        raise  # unreachable; satisfies the type checker


def parse_status_bytes(raw: str) -> StatusChanges:
    """Return (modified_or_added, deleted) file paths parsed from porcelain -z output."""
    if not raw:
        return StatusChanges(upserts=[], deletes=[])

    upserts: list[str] = []
    deletes: list[str] = []

    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        # Format: "XY path" — XY are the 2-char status codes; path is at
        # column 3. With -z, paths are never quoted.
        code = entry[:2]
        path = entry[3:]

        # Renames (R) and copies (C) are followed by a separate
        # NUL-terminated string carrying the source path. Consume it.
        if "R" in code or "C" in code:
            old_path = parts[i] if i < len(parts) else ""
            i += 1
            upserts.append(path)
            if "R" in code and old_path:
                # Pure rename: source path is removed from the new tree.
                deletes.append(old_path)
            # For copies (C), the source stays in place — no delete.
            continue

        # Trust the git status code: `D` is a delete regardless of whether
        # the file currently exists on disk. Re-checking `.exists()` here
        # introduced a TOCTOU window where a recreated file would be
        # misclassified as an upsert and re-uploaded to the tree instead
        # of removed from it.
        if "D" in code:
            deletes.append(path)
        else:
            upserts.append(path)

    return StatusChanges(upserts=upserts, deletes=deletes)


def parse_status(consumer_dir: Path) -> StatusChanges:
    """Return (modified_or_added, deleted) file paths relative to consumer_dir."""
    raw = run("git", "status", "--porcelain=v1", "-z", "-uall", cwd=consumer_dir)
    return parse_status_bytes(raw)


def derive_signoff_trailer(app_slug: str) -> str:
    """Build a `Signed-off-by:` trailer for the App's identity."""
    name = f"{app_slug}[bot]"
    return f"Signed-off-by: {name} <{name}@users.noreply.github.com>"


def with_signoff(message: str, trailer: str) -> str:
    """Append a Signed-off-by trailer if not already present."""
    if "Signed-off-by:" in message:
        return message
    return f"{message.rstrip()}\n\n{trailer}\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--base-branch", required=True, help="branch to fork the sync commit from")
    p.add_argument("--new-branch", required=True, help="branch to create with the new commit")
    p.add_argument("--message", required=True, help="commit message")
    p.add_argument("--payload-dir", default=None, type=Path, help="path to extracted payload tree directory")
    p.add_argument("--manifest", default=None, type=Path, help="path to status manifest file")
    p.add_argument("--base-sha-file", default=None, type=Path, help="path to file containing expected base SHA")
    p.add_argument("--expected-base-sha", default=None, help="expected base commit SHA")
    p.add_argument("--consumer-dir", default=None, type=Path, help="path to consumer git working directory")
    p.add_argument("--token-env", default="GH_APP_TOKEN", help="env var holding the App installation token")
    p.add_argument(
        "--app-slug",
        default=None,
        help=(
            "App slug (e.g. 'loomantix') for the Signed-off-by trailer. "
            "Pass `${{ steps.app-token.outputs.app-slug }}` from the workflow. "
            "If omitted, no DCO trailer is appended (consumers that enforce "
            "DCO will then need a per-repo bot exemption)."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        sys.stderr.write(f"missing token in env var {args.token_env}\n")
        return 2

    # Refuse to force-update the base branch onto itself.
    if args.new_branch == args.base_branch:
        sys.stderr.write(
            f"refusing to operate: --new-branch and --base-branch are the same ({args.new_branch})\n"
        )
        return 2

    if not args.payload_dir and not args.consumer_dir:
        sys.stderr.write("either --payload-dir or --consumer-dir is required\n")
        return 2

    tree_dir = args.payload_dir.resolve() if args.payload_dir else args.consumer_dir.resolve()
    owner_repo = f"{args.owner}/{args.repo}"

    if args.manifest:
        if not args.manifest.is_file():
            sys.stderr.write(f"manifest file not found: {args.manifest}\n")
            return 1
        raw_manifest = args.manifest.read_text(encoding="utf-8", errors="surrogateescape")
        changes = parse_status_bytes(raw_manifest)
    elif args.consumer_dir:
        consumer_dir = args.consumer_dir.resolve()
        changes = parse_status(consumer_dir)
    else:
        sys.stderr.write("either --manifest or --consumer-dir is required\n")
        return 2

    if not changes.upserts and not changes.deletes:
        print("No changes to commit.")
        return 0
    print(f"Changes detected: {len(changes.upserts)} upsert, {len(changes.deletes)} delete")

    expected_base_sha: str | None = None
    if args.base_sha_file:
        if not args.base_sha_file.is_file():
            sys.stderr.write(f"base-sha file not found: {args.base_sha_file}\n")
            return 1
        expected_base_sha = args.base_sha_file.read_text(encoding="utf-8").strip()
    elif args.expected_base_sha:
        expected_base_sha = args.expected_base_sha.strip()

    # 1. Resolve the base branch's HEAD commit + tree.
    base_ref = github_api("GET", f"/repos/{owner_repo}/git/ref/heads/{args.base_branch}", token)
    base_sha = base_ref["object"]["sha"]

    if expected_base_sha and base_sha != expected_base_sha:
        sys.stderr.write(
            f"base branch {args.base_branch} HEAD {base_sha} has diverged from expected base {expected_base_sha}\n"
        )
        return 1

    base_commit = github_api("GET", f"/repos/{owner_repo}/git/commits/{base_sha}", token)
    base_tree_sha = base_commit["tree"]["sha"]

    # 2. Build the tree-entry list:
    tree: list[dict[str, Any]] = []

    for path in changes.upserts:
        full = tree_dir / path
        if not full.is_file():
            sys.stderr.write(f"  ❌ upsert path is not a regular file: {path}\n")
            return 1
        content = full.read_bytes()
        blob = github_api(
            "POST",
            f"/repos/{owner_repo}/git/blobs",
            token,
            {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        mode = "100755" if os.access(full, os.X_OK) else "100644"
        tree.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})

    for path in changes.deletes:
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    # 3. Create the new tree
    new_tree = github_api(
        "POST",
        f"/repos/{owner_repo}/git/trees",
        token,
        {"base_tree": base_tree_sha, "tree": tree},
    )

    # 4. Create the commit
    full_message = (
        with_signoff(args.message, derive_signoff_trailer(args.app_slug))
        if args.app_slug
        else args.message
    )
    new_commit = github_api(
        "POST",
        f"/repos/{owner_repo}/git/commits",
        token,
        {"message": full_message, "tree": new_tree["sha"], "parents": [base_sha]},
    )

    # 5. Create or force-update the new-branch ref
    existing = github_api_optional(
        "GET", f"/repos/{owner_repo}/git/ref/heads/{args.new_branch}", token
    )
    if existing is None:
        github_api(
            "POST",
            f"/repos/{owner_repo}/git/refs",
            token,
            {"ref": f"refs/heads/{args.new_branch}", "sha": new_commit["sha"]},
        )
    else:
        github_api(
            "PATCH",
            f"/repos/{owner_repo}/git/refs/heads/{args.new_branch}",
            token,
            {"sha": new_commit["sha"], "force": True},
        )

    print(f"✓ signed commit {new_commit['sha']} on branch {args.new_branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
