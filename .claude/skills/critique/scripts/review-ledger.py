#!/usr/bin/env python3
"""Create, reconcile, and disposition deterministic local-review ledger entries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn, cast


PROTOCOL_VERSION = 3
HUNK_WITH_LEFT_RE = re.compile(
    r"^@@ -(?P<left>\d+)(?:,\d+)? \+(?P<right>\d+)(?:,\d+)? @@"
)
SHA_RE = re.compile(r"[0-9a-f]{40}")
TOKEN_RE = re.compile(r"[A-Za-z0-9._:/-]+")
FINDING_V1 = "<!-- local-review:v1 "
DISPOSITION_V1 = "<!-- local-review-disposition:v1 "
PR_V1_MARKERS = (
    "<!-- local-review-refactor:v1 ",
    "<!-- local-review-pass:v1 ",
    "<!-- local-review-complete:v1 ",
)
EXPECTED_ACTOR_ENV = "AGENT_LOOP_REVIEW_ACTOR"
FINDING_V3_RE = re.compile(
    r"^<!-- local-review:v3 "
    r"engine=(?P<engine>codex|claude) "
    r"round=(?P<round>[1-9][0-9]*) "
    r"head=(?P<head>[0-9a-f]{40}) "
    r"fingerprint=(?P<fingerprint>[A-Za-z0-9._:/-]+) "
    r"occurrence=(?P<occurrence>[1-9][0-9]*) "
    r"severity=(?P<severity>blocking|major|minor|nit) "
    r"lens=(?P<lens>[A-Za-z0-9._:/-]+) "
    r"content-sha256=(?P<content_sha>[0-9a-f]{64}) -->$",
    re.MULTILINE,
)
PSEUDO_V3_RE = re.compile(
    r"^<!-- local-review:v3 engine=claude "
    r"fingerprint=(?P<fingerprint>[A-Za-z0-9._:/-]+)"
    r"(?: outcome=deferred)? -->$",
    re.MULTILINE,
)
DISPOSITION_V3_RE = re.compile(
    r"^<!-- local-review-disposition:v3 "
    r"engine=(?P<engine>codex|claude) "
    r"round=(?P<round>[1-9][0-9]*) "
    r"head=(?P<head>[0-9a-f]{40}) "
    r"fingerprint=(?P<fingerprint>[A-Za-z0-9._:/-]+) "
    r"occurrence=(?P<occurrence>[1-9][0-9]*) "
    r"outcome=(?P<outcome>fixed|dismissed|deferred) "
    r"content-sha256=(?P<content_sha>[0-9a-f]{64}) -->$",
    re.MULTILINE,
)
PROTOCOL_THREAD_MARKER_RE = re.compile(r"^<!--[ \t]*local-review(?=[: \t-])")
LEGACY_THREAD_MARKER_RE = re.compile(
    r"^<!--[ \t]*local-review(?:-disposition)?:v1(?=[ \t]|-->)"
)
FINDING_V1_RE = re.compile(
    r"^<!-- local-review:v1 "
    r"engine=(?P<engine>codex|claude) "
    r"round=(?P<round>[1-9][0-9]*) "
    r"head=(?P<head>[0-9a-f]{40}) "
    r"fingerprint=(?P<fingerprint>[A-Za-z0-9._:/-]+) -->$",
    re.MULTILINE,
)
DISPOSITION_V1_RE = re.compile(
    r"^<!-- local-review-disposition:v1 "
    r"engine=(?P<engine>codex|claude) "
    r"round=(?P<round>[1-9][0-9]*) "
    r"head=(?P<head>[0-9a-f]{40}) "
    r"fingerprint=(?P<fingerprint>[A-Za-z0-9._:/-]+) "
    r"outcome=(?P<outcome>fixed|dismissed|deferred) -->$",
    re.MULTILINE,
)


class LedgerError(RuntimeError):
    """A fail-closed local-review ledger validation or mutation error."""


def _fail(message: str) -> NoReturn:
    raise LedgerError(message)


def _run_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
    command = ["gh", *args]
    if payload is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command,
        input=None if payload is None else json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "no diagnostic returned"
        _fail(f"GitHub operation failed: {detail}")
    return result.stdout


def _json_output(args: list[str], payload: dict[str, Any] | None = None) -> Any:
    raw = _run_gh(args, payload)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise LedgerError("GitHub returned invalid JSON") from error


def _run_git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or "no diagnostic returned"
        _fail(f"Git operation failed: {detail}")
    return result.stdout


def _current_login() -> str:
    response = _json_output(["api", "user"])
    if not isinstance(response, dict) or not isinstance(response.get("login"), str):
        _fail("GitHub returned an invalid authenticated-user response")
    login = cast(str, response["login"])
    if not login:
        _fail("GitHub returned an empty authenticated user")
    expected = os.environ.get(EXPECTED_ACTOR_ENV)
    if expected and login != expected:
        _fail(f"authenticated GitHub actor changed: expected {expected}, found {login}")
    return login


def _actor_rows(rows: list[dict[str, Any]], actor: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if isinstance(row.get("user"), dict) and row["user"].get("login") == actor
    ]


def _read_legacy_body(path: str, marker: str | tuple[str, ...]) -> str:
    body = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    if not body.strip():
        _fail("comment body is empty")
    markers = (marker,) if isinstance(marker, str) else marker
    if not any(candidate in body for candidate in markers):
        _fail("comment body lacks the required local-review marker")
    return body.rstrip()


def _read_content(path: str) -> str:
    if path == "-":
        _fail("v3 content must be a regular file; stdin and heredocs are not accepted")
    content_path = Path(path)
    if content_path.is_symlink() or not content_path.is_file():
        _fail("content file must be a regular non-symlink file")
    try:
        content = content_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise LedgerError("content file must be valid UTF-8") from error
    if not content.strip():
        _fail("comment content is empty")
    if "\x00" in content:
        _fail("comment content contains NUL")
    if "<!-- local-review" in content:
        _fail("comment content must not contain local-review markers")
    return content


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_token(value: str, label: str) -> str:
    if not TOKEN_RE.fullmatch(value):
        _fail(f"{label} must match {TOKEN_RE.pattern}")
    return value


def _verify_head(repo: str, pr: int, expected_head: str) -> None:
    actual = _run_gh(
        [
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "headRefOid",
            "--jq",
            ".headRefOid",
        ]
    ).strip()
    if actual != expected_head:
        _fail(
            f"PR head mismatch: expected {expected_head}, found {actual or '<empty>'}"
        )


def _diff_lines(patch: str) -> tuple[set[int], set[int]]:
    left_lines: set[int] = set()
    right_lines: set[int] = set()
    left = 0
    right = 0
    in_hunk = False
    for raw_line in patch.splitlines():
        match = HUNK_WITH_LEFT_RE.match(raw_line)
        if match is not None:
            left = int(match.group("left"))
            right = int(match.group("right"))
            in_hunk = True
            continue
        if not in_hunk or raw_line.startswith("\\ No newline"):
            continue
        prefix = raw_line[:1]
        if prefix == " ":
            left_lines.add(left)
            right_lines.add(right)
            left += 1
            right += 1
        elif prefix == "-":
            left_lines.add(left)
            left += 1
        elif prefix == "+":
            right_lines.add(right)
            right += 1
    return left_lines, right_lines


def _flatten_pages(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _fail(f"GitHub {label} response has an unexpected shape")
    rows: list[dict[str, Any]] = []
    for page in value:
        if not isinstance(page, list):
            _fail(f"GitHub {label} page has an unexpected shape")
        for item in page:
            if not isinstance(item, dict):
                _fail(f"GitHub {label} item has an unexpected shape")
            rows.append(item)
    return rows


def _pr_files(repo: str, pr: int) -> dict[str, str | None]:
    rows = _flatten_pages(
        _json_output(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/pulls/{pr}/files?per_page=100",
            ]
        ),
        "PR-files",
    )
    files: dict[str, str | None] = {}
    for item in rows:
        if not isinstance(item.get("filename"), str):
            _fail("GitHub PR-files item has an unexpected shape")
        patch = item.get("patch")
        files[cast(str, item["filename"])] = patch if isinstance(patch, str) else None
    return files


def _review_comments(repo: str, pr: int) -> list[dict[str, Any]]:
    return _flatten_pages(
        _json_output(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/pulls/{pr}/comments?per_page=100",
            ]
        ),
        "review-comments",
    )


def _issue_comments(repo: str, pr: int) -> list[dict[str, Any]]:
    return _flatten_pages(
        _json_output(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/issues/{pr}/comments?per_page=100",
            ]
        ),
        "PR-comments",
    )


def _validate_anchor(
    files: dict[str, str | None], path: str, line: int | None, side: str | None
) -> None:
    if path not in files:
        _fail(f"path is not part of the PR diff: {path}")
    if line is None:
        return
    patch = files[path]
    if patch is None:
        _fail("GitHub omitted the file patch; use --file-level only if defensible")
    left_lines, right_lines = _diff_lines(patch)
    valid = right_lines if side == "RIGHT" else left_lines
    if line in valid:
        return
    nearest = sorted(valid, key=lambda candidate: (abs(candidate - line), candidate))[
        :5
    ]
    candidates = ", ".join(str(candidate) for candidate in nearest) or "none"
    _fail(
        f"line {line} is not an exact {side} anchor in GitHub's PR patch; "
        f"nearest valid lines: {candidates}"
    )


def _verify_comment(
    repo: str, comment_id: int, expected_body: str, expected_actor: str | None = None
) -> None:
    response = _json_output(["api", f"repos/{repo}/pulls/comments/{comment_id}"])
    if not isinstance(response, dict) or response.get("body") != expected_body:
        _fail(f"could not verify review comment {comment_id} after posting")
    if expected_actor is not None:
        user = response.get("user")
        if not isinstance(user, dict) or user.get("login") != expected_actor:
            _fail(f"review comment {comment_id} was not authored by {expected_actor}")


def _verify_issue_comment(
    repo: str, comment_id: int, expected_body: str, expected_actor: str | None = None
) -> None:
    response = _json_output(["api", f"repos/{repo}/issues/comments/{comment_id}"])
    if not isinstance(response, dict) or response.get("body") != expected_body:
        _fail(f"could not verify PR comment {comment_id} after posting")
    if expected_actor is not None:
        user = response.get("user")
        if not isinstance(user, dict) or user.get("login") != expected_actor:
            _fail(f"PR comment {comment_id} was not authored by {expected_actor}")


def _issue_comment_exists(repo: str, pr: int, comment_id: int) -> bool:
    return any(row.get("id") == comment_id for row in _issue_comments(repo, pr))


def _delete_issue_comment(repo: str, pr: int, comment_id: int) -> None:
    try:
        _run_gh(["api", "-X", "DELETE", f"repos/{repo}/issues/comments/{comment_id}"])
    except LedgerError as error:
        if _issue_comment_exists(repo, pr, comment_id):
            raise error
        return
    if _issue_comment_exists(repo, pr, comment_id):
        _fail(f"could not verify rollback of PR comment {comment_id}")


def _posted_comment_id(response: Any) -> int:
    if not isinstance(response, dict) or not isinstance(response.get("id"), int):
        _fail("GitHub accepted the mutation but returned no comment ID")
    return cast(int, response["id"])


def _matching_body(rows: list[dict[str, Any]], marker: str, body: str) -> int | None:
    matches = [row for row in rows if marker in str(row.get("body", ""))]
    if not matches:
        return None
    if len(matches) != 1:
        _fail("ledger idempotency key is duplicated")
    row = matches[0]
    if row.get("body") != body or not isinstance(row.get("id"), int):
        _fail("ledger idempotency key already exists with conflicting content")
    return cast(int, row["id"])


def _matching_attestation(
    rows: list[dict[str, Any]], engine: str, round_number: int, body: str
) -> int | None:
    prefixes = (
        f"<!-- local-review-pass:v3 engine={engine} round={round_number} ",
        f"<!-- local-review-complete:v3 engine={engine} round={round_number} ",
    )
    matches = [
        row
        for row in rows
        if any(str(row.get("body", "")).startswith(prefix) for prefix in prefixes)
    ]
    if not matches:
        return None
    if len(matches) != 1:
        _fail("local-review attestation identity is duplicated")
    row = matches[0]
    if row.get("body") != body or not isinstance(row.get("id"), int):
        _fail("local-review attestation identity conflicts with existing evidence")
    return cast(int, row["id"])


def _finding_records(
    rows: list[dict[str, Any]], fingerprint: str
) -> list[tuple[dict[str, Any], re.Match[str]]]:
    records: list[tuple[dict[str, Any], re.Match[str]]] = []
    for row in rows:
        match = FINDING_V3_RE.search(str(row.get("body", "")))
        if match is not None and match.group("fingerprint") == fingerprint:
            records.append((row, match))
    return records


def _disposition_records(
    rows: list[dict[str, Any]], fingerprint: str
) -> list[tuple[dict[str, Any], re.Match[str]]]:
    records: list[tuple[dict[str, Any], re.Match[str]]] = []
    for row in rows:
        match = DISPOSITION_V3_RE.search(str(row.get("body", "")))
        if match is not None and match.group("fingerprint") == fingerprint:
            records.append((row, match))
    return records


def _rows_have_historical_markers(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        body = str(row.get("body", ""))
        if FINDING_V1 in body or DISPOSITION_V1 in body:
            return True
        if "<!-- local-review:v3" in body and FINDING_V3_RE.search(body) is None:
            return True
    return False


def _require_finding_occurrence(
    rows: list[dict[str, Any]], args: argparse.Namespace
) -> tuple[int, int, re.Match[str]]:
    records = _finding_records(rows, args.fingerprint)
    matches = [
        (row, match)
        for row, match in records
        if match.group("engine") == args.engine
        and int(match.group("round")) == args.round
        and int(match.group("occurrence")) == args.occurrence
    ]
    if len(matches) != 1:
        _fail("disposition does not identify exactly one existing finding occurrence")
    roots = [row for row, match in records if int(match.group("occurrence")) == 1]
    if (
        len(roots) != 1
        or not isinstance(roots[0].get("id"), int)
        or roots[0]["id"] != args.comment_id
    ):
        _fail("--comment-id does not identify the fingerprint root comment")
    occurrence_id = matches[0][0].get("id")
    if not isinstance(occurrence_id, int):
        _fail("finding occurrence has no comment ID")
    return cast(int, roots[0]["id"]), occurrence_id, matches[0][1]


def _finding_body(args: argparse.Namespace) -> tuple[str, str]:
    content = _read_content(args.content_file)
    marker = (
        f"<!-- local-review:v3 engine={args.engine} round={args.round} "
        f"head={args.head} fingerprint={_require_token(args.fingerprint, 'fingerprint')} "
        f"occurrence={args.occurrence} severity={args.severity} "
        f"lens={_require_token(args.lens, 'lens')} "
        f"content-sha256={_sha256_text(content)} -->"
    )
    return marker, f"{marker}\n{content}"


def _disposition_body(args: argparse.Namespace) -> tuple[str, str]:
    content = _read_content(args.content_file)
    marker = (
        f"<!-- local-review-disposition:v3 engine={args.engine} round={args.round} "
        f"head={args.head} fingerprint={_require_token(args.fingerprint, 'fingerprint')} "
        f"occurrence={args.occurrence} outcome={args.outcome} "
        f"content-sha256={_sha256_text(content)} -->"
    )
    return marker, f"{marker}\n{content}"


def _post_review_comment(
    args: argparse.Namespace, marker: str, body: str, *, reply_to: int | None = None
) -> tuple[int, bool]:
    actor = getattr(args, "actor", None)
    rows = _review_comments(args.repo, args.pr)
    if actor is not None:
        rows = _actor_rows(rows, actor)
    existing = _matching_body(rows, marker, body)
    if existing is not None:
        _verify_comment(args.repo, existing, body, actor)
        _verify_head(args.repo, args.pr, args.head)
        return existing, True
    if reply_to is None:
        payload: dict[str, Any] = {
            "body": body,
            "commit_id": args.head,
            "path": args.path,
        }
        if args.file_level:
            payload["subject_type"] = "file"
        else:
            payload.update({"line": args.line, "side": args.side})
        endpoint = f"repos/{args.repo}/pulls/{args.pr}/comments"
    else:
        payload = {"body": body}
        endpoint = f"repos/{args.repo}/pulls/{args.pr}/comments/{reply_to}/replies"
    try:
        response = _json_output(["api", "-X", "POST", endpoint], payload)
        comment_id = _posted_comment_id(response)
    except LedgerError:
        recovered_rows = _review_comments(args.repo, args.pr)
        if actor is not None:
            recovered_rows = _actor_rows(recovered_rows, actor)
        recovered = _matching_body(recovered_rows, marker, body)
        if recovered is None:
            raise
        comment_id = recovered
    try:
        _verify_comment(args.repo, comment_id, body, actor)
    except LedgerError as error:
        recovered_rows = _review_comments(args.repo, args.pr)
        if actor is not None:
            recovered_rows = _actor_rows(recovered_rows, actor)
        recovered = _matching_body(recovered_rows, marker, body)
        if recovered != comment_id:
            raise error
    _verify_head(args.repo, args.pr, args.head)
    return comment_id, False


def _thread_state(
    args: argparse.Namespace, expected_comment_ids: tuple[int, ...] = ()
) -> bool:
    query = """
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      repository { nameWithOwner }
      pullRequest { number }
      comments(first: 100) {
        nodes { databaseId body author { login } }
        pageInfo { hasNextPage }
      }
    }
  }
}
""".strip()
    response = _json_output(
        ["api", "graphql"],
        {"query": query, "variables": {"threadId": args.thread_id}},
    )
    try:
        thread = response["data"]["node"]
    except (KeyError, TypeError) as error:
        raise LedgerError("GitHub returned an invalid thread read-back") from error
    if not isinstance(thread, dict) or thread.get("id") != args.thread_id:
        _fail(f"could not verify review thread {args.thread_id}")
    repository = thread.get("repository")
    pull_request = thread.get("pullRequest")
    if (
        not isinstance(repository, dict)
        or repository.get("nameWithOwner") != args.repo
        or not isinstance(pull_request, dict)
        or pull_request.get("number") != args.pr
    ):
        _fail(
            f"review thread {args.thread_id} does not belong to {args.repo}#{args.pr}"
        )
    if not isinstance(thread.get("isResolved"), bool):
        _fail(f"review thread {args.thread_id} has invalid resolution state")
    comment_id = getattr(args, "comment_id", None)
    legacy_actor = getattr(args, "legacy_root_actor", None)
    if comment_id is not None or expected_comment_ids or legacy_actor is not None:
        comments = thread.get("comments")
        if not isinstance(comments, dict):
            _fail("review thread read-back omitted comments")
        page_info = comments.get("pageInfo")
        nodes = comments.get("nodes")
        if (
            not isinstance(page_info, dict)
            or page_info.get("hasNextPage") is not False
            or not isinstance(nodes, list)
        ):
            _fail("review thread comments are incomplete")
        ids = [row.get("databaseId") for row in nodes if isinstance(row, dict)]
        if comment_id is not None and (not ids or ids[0] != comment_id):
            _fail("--comment-id is not the root comment of --thread-id")
        if any(expected not in ids for expected in expected_comment_ids):
            _fail("finding occurrence does not belong to --thread-id")
    if legacy_actor is not None:
        if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
            _fail("legacy resolve target has no root comment")
        root = nodes[0]
        author = root.get("author")
        if (
            not isinstance(author, dict)
            or author.get("login") != legacy_actor
            or FINDING_V1 not in str(root.get("body", ""))
        ):
            _fail("legacy resolve target is not an actor-owned v1 finding thread")
    return cast(bool, thread["isResolved"])


def _set_thread_state(args: argparse.Namespace, resolved: bool) -> bool:
    if _thread_state(args) is resolved:
        return True
    field = "resolveReviewThread" if resolved else "unresolveReviewThread"
    mutation = f"""
mutation($threadId: ID!) {{
  {field}(input: {{threadId: $threadId}}) {{
    thread {{ id isResolved }}
  }}
}}
""".strip()
    try:
        response = _json_output(
            ["api", "graphql"],
            {"query": mutation, "variables": {"threadId": args.thread_id}},
        )
        thread = response["data"][field]["thread"]
        if not isinstance(thread, dict):
            _fail("GitHub returned an invalid thread mutation response")
        if (
            thread.get("id") != args.thread_id
            or thread.get("isResolved") is not resolved
        ):
            _fail(
                f"GitHub did not set review thread {args.thread_id} resolved={resolved}"
            )
    except (KeyError, TypeError) as error:
        mutation_error: LedgerError = LedgerError(
            "GitHub returned an invalid thread mutation response"
        )
        mutation_error.__cause__ = error
        try:
            if _thread_state(args) is resolved:
                return False
        except LedgerError:
            pass
        raise mutation_error
    except LedgerError as error:
        try:
            if _thread_state(args) is resolved:
                return False
        except LedgerError:
            pass
        raise error
    try:
        verified = _thread_state(args)
    except LedgerError as error:
        try:
            verified = _thread_state(args)
        except LedgerError:
            raise error
    if verified is not resolved:
        _fail(f"could not verify review thread {args.thread_id} resolved={resolved}")
    return False


def _review_threads(
    repo: str,
    pr: int,
    threads_file: str | None = None,
    expected_threads_sha256: str | None = None,
) -> list[dict[str, Any]]:
    try:
        owner, name = repo.split("/", 1)
    except ValueError as error:
        raise LedgerError("--repo must be OWNER/REPO") from error
    query = """
query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$endCursor) {
        nodes {
          id
          isResolved
          repository { nameWithOwner }
          pullRequest { number }
          comments(first:100) {
            nodes { databaseId body author { login } }
            pageInfo { hasNextPage }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()
    if threads_file is None:
        pages = _json_output(
            [
                "api",
                "graphql",
                "--paginate",
                "--slurp",
                "-f",
                f"query={query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={pr}",
            ]
        )
    else:
        path = Path(threads_file)
        if path.is_symlink() or not path.is_file():
            _fail("review-thread snapshot must be a regular non-symlink file")
        expected_digest = expected_threads_sha256 or os.environ.get(
            "AGENT_LOOP_REVIEW_THREADS_SHA256"
        )
        if expected_digest is None or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            _fail("review-thread snapshot requires a sealed SHA-256 digest")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            _fail("review-thread snapshot changed after it was sealed")
        try:
            pages = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LedgerError(
                "review-thread snapshot must contain valid UTF-8 JSON"
            ) from error
    if not isinstance(pages, list):
        _fail("GitHub review-thread response has an unexpected shape")
    threads: list[dict[str, Any]] = []
    if not pages:
        _fail("GitHub review-thread response is empty")
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get("errors"):
            _fail("GitHub review-thread response is incomplete")
        try:
            connection = page["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
        except (KeyError, TypeError) as error:
            raise LedgerError(
                "GitHub review-thread response has an unexpected shape"
            ) from error
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            _fail("GitHub review-thread nodes have an unexpected shape")
        expected_more = page_index < len(pages) - 1
        if page_info.get("hasNextPage") is not expected_more:
            _fail("GitHub review-thread pagination is incomplete")
        if expected_more and not isinstance(page_info.get("endCursor"), str):
            _fail("GitHub review-thread pagination omitted its cursor")
        for thread in nodes:
            if not isinstance(thread, dict):
                _fail("GitHub review thread has an unexpected shape")
            comments = thread.get("comments")
            if (
                not isinstance(comments, dict)
                or not isinstance(comments.get("nodes"), list)
                or not isinstance(comments.get("pageInfo"), dict)
                or comments["pageInfo"].get("hasNextPage") is not False
            ):
                _fail("GitHub review-thread comments are incomplete")
            threads.append(thread)
    return threads


def _pseudo_v3_match(body: str) -> re.Match[str] | None:
    matches = list(PSEUDO_V3_RE.finditer(body))
    if not matches:
        return None
    if (
        len(matches) != 1
        or body.count("<!-- local-review") != 1
        or matches[0].start() == 0
        or body[matches[0].start() - 1] != "\n"
        or matches[0].end() != len(body)
        or not body[: matches[0].start()].strip()
    ):
        _fail("actor-owned historical local-review:v3 marker is malformed")
    return matches[0]


def _verify_v1_marker(body: str, marker: re.Match[str], label: str) -> None:
    if marker.start() != 0 or body[marker.end() : marker.end() + 1] != "\n":
        _fail(f"actor-owned v1 {label} marker is malformed")
    if not body[marker.end() + 1 :].strip():
        _fail(f"actor-owned v1 {label} content is empty")


def _historical_comment_ids(args: argparse.Namespace) -> set[int] | None:
    path_value = getattr(args, "historical_comment_ids_file", None) or os.environ.get(
        "AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE"
    )
    if not path_value:
        return None
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        _fail("historical comment IDs must be a regular non-symlink file")
    try:
        values = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LedgerError(
            "historical comment IDs must contain valid UTF-8 JSON"
        ) from error
    if (
        not isinstance(values, list)
        or any(type(value) is not int or value < 1 for value in values)
        or len(set(values)) != len(values)
    ):
        _fail("historical comment IDs must be unique positive integers")
    return set(values)


def _thread_markers(
    thread: dict[str, Any],
    actor: str,
    historical_comment_ids: set[int] | None = None,
) -> tuple[list[tuple[int, re.Match[str]]], list[tuple[int, re.Match[str]]]]:
    comments = cast(dict[str, Any], thread["comments"])["nodes"]
    findings: list[tuple[int, re.Match[str]]] = []
    dispositions: list[tuple[int, re.Match[str]]] = []
    findings_v1: list[tuple[int, re.Match[str]]] = []
    dispositions_v1: list[tuple[int, re.Match[str]]] = []
    for index, comment in enumerate(comments):
        if not isinstance(comment, dict):
            _fail("GitHub review comment has an unexpected shape")
        body = str(comment.get("body", ""))
        author = comment.get("author")
        if not isinstance(author, dict) or not isinstance(author.get("login"), str):
            if "<!-- local-review" in body:
                _fail("could not establish local-review comment ownership")
            continue
        if author.get("login") != actor:
            continue
        first_line = body.partition("\n")[0].removesuffix("\r")
        finding = FINDING_V3_RE.search(body)
        disposition = DISPOSITION_V3_RE.search(body)
        pseudo = _pseudo_v3_match(body)
        if "<!-- local-review:v3" in body and finding is None and pseudo is None:
            _fail("actor-owned local-review:v3 marker is malformed or unsupported")
        if (
            PROTOCOL_THREAD_MARKER_RE.match(first_line) is not None
            and finding is None
            and disposition is None
            and FINDING_V1_RE.search(body) is None
            and DISPOSITION_V1_RE.search(body) is None
        ):
            _fail("actor-owned local-review marker is malformed or unsupported")
        if finding is not None:
            _verify_marker_content(body, finding, "finding")
            findings.append((index, finding))
        if disposition is not None:
            _verify_marker_content(body, disposition, "disposition")
            dispositions.append((index, disposition))
        if pseudo is not None:
            if (
                historical_comment_ids is not None
                and comment.get("databaseId") not in historical_comment_ids
            ):
                _fail(
                    "historical local-review:v3 finding was not captured before the current pass"
                )
            later_same_actor = any(
                isinstance(reply, dict)
                and isinstance(reply.get("author"), dict)
                and reply["author"].get("login") == actor
                and bool(str(reply.get("body", "")).strip())
                and "<!-- local-review" not in str(reply.get("body", ""))
                for reply in comments[index + 1 :]
            )
            if (
                index != 0
                or not isinstance(thread.get("id"), str)
                or thread.get("isResolved") is not True
                or not later_same_actor
            ):
                _fail(
                    "historical local-review:v3 finding is not settled actor-owned history"
                )
        finding_v1 = FINDING_V1_RE.search(body)
        disposition_v1 = DISPOSITION_V1_RE.search(body)
        legacy_marker = LEGACY_THREAD_MARKER_RE.match(first_line)
        if (
            (FINDING_V1 in body or DISPOSITION_V1 in body or legacy_marker is not None)
            and finding_v1 is None
            and disposition_v1 is None
        ):
            _fail("actor-owned legacy local-review marker is malformed")
        if finding_v1 is not None:
            _verify_v1_marker(body, finding_v1, "finding")
            findings_v1.append((index, finding_v1))
        if disposition_v1 is not None:
            _verify_v1_marker(body, disposition_v1, "disposition")
            dispositions_v1.append((index, disposition_v1))
    if findings_v1 or dispositions_v1:
        if thread.get("isResolved") is not True:
            _fail("legacy local-review finding thread is unresolved")
        used: set[int] = set()
        for finding_index, finding_v1 in findings_v1:
            matches = [
                (disposition_index, disposition_v1)
                for disposition_index, disposition_v1 in dispositions_v1
                if disposition_index > finding_index
                and disposition_v1.group("engine") == finding_v1.group("engine")
                and disposition_v1.group("round") == finding_v1.group("round")
                and disposition_v1.group("fingerprint")
                == finding_v1.group("fingerprint")
            ]
            if len(matches) != 1:
                _fail(
                    "legacy local-review finding lacks exactly one matching disposition"
                )
            if matches[0][0] in used:
                _fail("legacy local-review disposition matches multiple findings")
            used.add(matches[0][0])
        if len(used) != len(dispositions_v1):
            _fail("legacy local-review ledger contains an orphan disposition")
    return findings, dispositions


def _verify_marker_content(body: str, marker: re.Match[str], label: str) -> None:
    if marker.start() != 0 or body[marker.end() : marker.end() + 1] != "\n":
        _fail(f"local-review {label} marker is not the first complete line")
    content = body[marker.end() + 1 :]
    if not content.strip():
        _fail(f"local-review {label} content is empty")
    if _sha256_text(content) != marker.group("content_sha"):
        _fail(f"local-review {label} content hash mismatch")


def _matching_dispositions(
    findings: list[tuple[int, re.Match[str]]],
    dispositions: list[tuple[int, re.Match[str]]],
) -> list[tuple[re.Match[str], re.Match[str]]]:
    matched: list[tuple[re.Match[str], re.Match[str]]] = []
    used_dispositions: set[int] = set()
    for finding_index, finding in findings:
        candidates = [
            (disposition_index, disposition)
            for disposition_index, disposition in dispositions
            if disposition_index > finding_index
            and disposition.group("engine") == finding.group("engine")
            and disposition.group("round") == finding.group("round")
            and disposition.group("fingerprint") == finding.group("fingerprint")
            and disposition.group("occurrence") == finding.group("occurrence")
        ]
        if len(candidates) != 1:
            _fail("local-review finding lacks exactly one matching disposition")
        disposition_index, disposition = candidates[0]
        if (
            finding.group("severity") == "blocking"
            and disposition.group("outcome") == "deferred"
        ):
            _fail("blocking local-review findings cannot be deferred")
        if disposition_index in used_dispositions:
            _fail("local-review disposition matches multiple findings")
        used_dispositions.add(disposition_index)
        matched.append((finding, disposition))
    if len(used_dispositions) != len(dispositions):
        _fail("local-review ledger contains an orphan disposition")
    return matched


def _verify_complete_v3_threads(
    repo: str,
    pr: int,
    actor: str,
    historical_comment_ids: set[int] | None = None,
    threads_file: str | None = None,
    expected_threads_sha256: str | None = None,
) -> list[tuple[re.Match[str], re.Match[str]]]:
    matched: list[tuple[re.Match[str], re.Match[str]]] = []
    topology: dict[str, list[tuple[str, int, re.Match[str]]]] = {}
    for thread in _review_threads(repo, pr, threads_file, expected_threads_sha256):
        repository = thread.get("repository")
        pull_request = thread.get("pullRequest")
        if (
            not isinstance(repository, dict)
            or repository.get("nameWithOwner") != repo
            or not isinstance(pull_request, dict)
            or pull_request.get("number") != pr
        ):
            _fail("GitHub returned a review thread outside the requested PR")
        findings, dispositions = _thread_markers(thread, actor, historical_comment_ids)
        if not findings and not dispositions:
            continue
        if not findings:
            _fail("local-review ledger contains a disposition without a finding")
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            _fail("local-review finding thread has no stable identity")
        for finding_index, finding in findings:
            topology.setdefault(finding.group("fingerprint"), []).append(
                (thread_id, finding_index, finding)
            )
        if thread.get("isResolved") is not True:
            _fail(
                f"local-review finding thread {thread.get('id', '<unknown>')} is unresolved"
            )
        matched.extend(_matching_dispositions(findings, dispositions))
    for records in topology.values():
        thread_ids = {thread_id for thread_id, _, _ in records}
        occurrences = sorted(
            int(finding.group("occurrence")) for _, _, finding in records
        )
        roots = [record for record in records if record[2].group("occurrence") == "1"]
        if (
            len(thread_ids) != 1
            or occurrences != list(range(1, len(records) + 1))
            or len(roots) != 1
            or roots[0][1] != 0
        ):
            _fail("local-review fingerprint topology is invalid")
    return matched


def _verify_historical_threads(repo: str, pr: int, actor: str) -> None:
    for thread in _review_threads(repo, pr):
        repository = thread.get("repository")
        pull_request = thread.get("pullRequest")
        if (
            not isinstance(repository, dict)
            or repository.get("nameWithOwner") != repo
            or not isinstance(pull_request, dict)
            or pull_request.get("number") != pr
        ):
            _fail("GitHub returned a review thread outside the requested PR")
        _thread_markers(thread, actor)


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or "no diagnostic returned"
        _fail(f"Git ancestry check failed: {detail}")
    return result.returncode == 0


def _result_head(args: argparse.Namespace) -> str:
    return cast(str, getattr(args, "result_head", None) or args.head)


def _verify_git_transition(before: str, result_head: str, live_head: str) -> None:
    local_head = _run_git(["rev-parse", "HEAD"]).strip()
    if local_head != live_head:
        _fail(
            f"local HEAD mismatch: expected {live_head}, found {local_head or '<empty>'}"
        )
    if not _is_ancestor(before, result_head):
        _fail("review result rewrites or does not descend from beforeSha")
    if not _is_ancestor(result_head, live_head):
        _fail("review result head is not an ancestor of the live head")


def _verify_review_base(repo: str, pr: int, base: str, before: str) -> None:
    resolved = _run_git(["rev-parse", "--verify", f"{base}^{{commit}}"]).strip()
    if resolved != base:
        _fail("review base did not resolve to the supplied commit")
    if not _is_ancestor(base, before):
        _fail("review base is not an ancestor of beforeSha")
    pr_base = _run_gh(
        [
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "baseRefOid",
            "--jq",
            ".baseRefOid",
        ]
    ).strip()
    if pr_base != base:
        _fail(f"PR base mismatch: expected {base}, found {pr_base or '<empty>'}")


def _load_allowed_heads(args: argparse.Namespace) -> dict[str, int]:
    path = Path(args.allowed_heads_file)
    if path.is_symlink() or not path.is_file():
        _fail("allowed transition heads must be a regular non-symlink file")
    try:
        values = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LedgerError(
            "allowed transition heads must contain valid UTF-8 JSON"
        ) from error
    if (
        not isinstance(values, list)
        or not values
        or any(
            not isinstance(value, str) or not SHA_RE.fullmatch(value)
            for value in values
        )
        or len(set(values)) != len(values)
        or values[0] != args.before
        or values[-1] != _result_head(args)
    ):
        _fail("allowed transition heads do not match the observed review transition")
    for before, after in zip(values, values[1:]):
        comparison = _json_output(
            ["api", f"repos/{args.repo}/compare/{before}...{after}"]
        )
        merge_base = (
            comparison.get("merge_base_commit")
            if isinstance(comparison, dict)
            else None
        )
        if (
            not isinstance(comparison, dict)
            or comparison.get("status") != "ahead"
            or not isinstance(merge_base, dict)
            or merge_base.get("sha") != before
        ):
            _fail("allowed transition heads are not forward-only")
    return {value: index for index, value in enumerate(values)}


def _same_round_evidence(
    args: argparse.Namespace,
    matched: list[tuple[re.Match[str], re.Match[str]]],
    allowed_heads: dict[str, int],
) -> list[tuple[str, bool, bool]]:
    evidence: dict[str, tuple[bool, bool]] = {}
    for finding, disposition in matched:
        if (
            finding.group("engine") != args.engine
            or int(finding.group("round")) != args.round
        ):
            continue
        finding_head = finding.group("head")
        finding_position = allowed_heads.get(finding_head)
        if finding_position is None:
            if disposition.group("outcome") == "fixed":
                disposition_head = disposition.group("head")
                if disposition_head == finding_head:
                    _fail("historical fixed disposition is not a forward transition")
                comparison = _json_output(
                    [
                        "api",
                        f"repos/{args.repo}/compare/{finding_head}...{disposition_head}",
                    ]
                )
                merge_base = (
                    comparison.get("merge_base_commit")
                    if isinstance(comparison, dict)
                    else None
                )
                if (
                    not isinstance(comparison, dict)
                    or comparison.get("status") != "ahead"
                    or not isinstance(merge_base, dict)
                    or merge_base.get("sha") != finding_head
                ):
                    _fail("historical fixed disposition is not a forward transition")
                if disposition_head not in allowed_heads:
                    continue
                finding_position = -1
            else:
                continue
        disposition_head = disposition.group("head")
        if disposition.group("outcome") == "fixed" and disposition_head == finding_head:
            _fail("fixed finding was not posted before its disposition head")
        if (
            disposition_head not in allowed_heads
            or allowed_heads[disposition_head] < finding_position
            or (
                disposition.group("outcome") == "fixed"
                and allowed_heads[disposition_head] == finding_position
            )
        ):
            _fail("same-round finding disposition is outside the observed transition")
        if (
            finding.group("severity") == "blocking"
            and disposition.group("outcome") != "fixed"
        ):
            _fail("blocking local-review findings must be fixed")
        fingerprint = finding.group("fingerprint")
        fixed = disposition.group("outcome") == "fixed"
        fixed_major = fixed and finding.group("severity") in {"blocking", "major"}
        previous = evidence.get(fingerprint, (False, False))
        evidence[fingerprint] = (
            previous[0] or fixed,
            previous[1] or fixed_major,
        )
    return [(fingerprint, *evidence[fingerprint]) for fingerprint in sorted(evidence)]


def _transition_heads(
    args: argparse.Namespace,
    matched: list[tuple[re.Match[str], re.Match[str]]],
) -> dict[str, int]:
    if getattr(args, "allowed_heads_file", None) is not None:
        return _load_allowed_heads(args)
    result_head = _result_head(args)
    if args.before == result_head:
        return {args.before: 0}
    comparison = subprocess.run(
        [
            "git",
            "rev-list",
            "--reverse",
            "--ancestry-path",
            f"{args.before}..{result_head}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if comparison.returncode != 0:
        _fail("could not derive the forward review transition")
    values = [args.before, *comparison.stdout.splitlines()]
    if values[-1] != result_head or len(set(values)) != len(values):
        _fail("review result transition is not forward-only")
    return {value: index for index, value in enumerate(values)}


def _verify_result_evidence(
    args: argparse.Namespace, data: dict[str, Any], actor: str
) -> None:
    _verify_review_base(args.repo, args.pr, args.base, args.before)
    _verify_git_transition(args.before, _result_head(args), args.head)
    matched = _verify_complete_v3_threads(
        args.repo,
        args.pr,
        actor,
        _historical_comment_ids(args),
        getattr(args, "threads_file", None),
        getattr(args, "expected_threads_sha256", None),
    )
    allowed_heads = _transition_heads(args, matched)
    evidence = _same_round_evidence(args, matched, allowed_heads)
    expected = sorted(cast(list[str], data["findingFingerprints"]))
    if [row[0] for row in evidence] != expected:
        _fail(
            "review result fingerprints do not equal the complete same-round disposition set"
        )
    if data["status"] == "clean":
        if any(has_fix for _, has_fix, _ in evidence):
            _fail("clean review results cannot have same-round fixes")
        return
    if not evidence:
        _fail("changed review results require ledger evidence")
    if (
        any(has_major_fix for _, _, has_major_fix in evidence)
        and data["classification"] != "material"
    ):
        _fail("fixed blocking or major findings require material classification")
    if not any(has_fix for _, has_fix, _ in evidence):
        _fail("changed review results require a fixed ledger finding")


def _verify_ledger(args: argparse.Namespace) -> None:
    actor = _current_login()
    if getattr(args, "actor", None) is not None and args.actor != actor:
        _fail("authenticated GitHub actor changed before ledger verification")
    _verify_head(args.repo, args.pr, args.head)
    matched = _verify_complete_v3_threads(
        args.repo,
        args.pr,
        actor,
        _historical_comment_ids(args),
        getattr(args, "threads_file", None),
        getattr(args, "expected_threads_sha256", None),
    )
    if getattr(args, "result_file", None) is not None:
        data = _validate_result_data(args)
        _verify_result_evidence(args, data, actor)
    _verify_head(args.repo, args.pr, args.head)
    print(json.dumps({"actor": actor, "dispositions": len(matched), "verified": True}))


def _preflight_anchor(args: argparse.Namespace) -> None:
    _verify_head(args.repo, args.pr, args.head)
    files = _pr_files(args.repo, args.pr)
    line = None if args.file_level else args.line
    side = None if args.file_level else args.side
    _validate_anchor(files, args.path, line, side)
    print(
        json.dumps(
            {
                "anchor": "file" if args.file_level else f"{args.side}:{args.line}",
                "path": args.path,
                "verified": True,
            }
        )
    )


def _post_finding(args: argparse.Namespace) -> None:
    if args.content_file:
        marker, body = _finding_body(args)
        args.actor = _current_login()
    else:
        marker = FINDING_V1
        body = _read_legacy_body(args.body_file, marker)
    _verify_head(args.repo, args.pr, args.head)
    files = _pr_files(args.repo, args.pr)
    line = None if args.file_level else args.line
    side = None if args.file_level else args.side
    _validate_anchor(files, args.path, line, side)
    if args.content_file:
        rows = _actor_rows(_review_comments(args.repo, args.pr), args.actor)
        if _rows_have_historical_markers(rows):
            _verify_historical_threads(args.repo, args.pr, args.actor)
        existing = _matching_body(rows, marker, body)
        records = _finding_records(rows, args.fingerprint)
        if existing is None and records:
            _fail("fingerprint already has a root thread; use reopen-occurrence")
        if args.occurrence != 1:
            _fail("post-finding creates occurrence 1; use reopen-occurrence later")
        comment_id, replayed = _post_review_comment(args, marker, body)
    else:
        payload: dict[str, Any] = {
            "body": body,
            "commit_id": args.head,
            "path": args.path,
        }
        if args.file_level:
            payload["subject_type"] = "file"
        else:
            payload.update({"line": args.line, "side": args.side})
        response = _json_output(
            ["api", "-X", "POST", f"repos/{args.repo}/pulls/{args.pr}/comments"],
            payload,
        )
        comment_id = _posted_comment_id(response)
        _verify_comment(args.repo, comment_id, body)
        _verify_head(args.repo, args.pr, args.head)
        replayed = False
    output = {"comment_id": comment_id, "verified": True}
    if args.content_file:
        output["replayed"] = replayed
    print(json.dumps(output))


def _reopen_occurrence(args: argparse.Namespace) -> None:
    marker, body = _finding_body(args)
    args.actor = _current_login()
    _verify_head(args.repo, args.pr, args.head)
    rows = _actor_rows(_review_comments(args.repo, args.pr), args.actor)
    if _rows_have_historical_markers(rows):
        _verify_historical_threads(args.repo, args.pr, args.actor)
    records = _finding_records(rows, args.fingerprint)
    existing = _matching_body(rows, marker, body)
    if args.occurrence < 2:
        _fail("reopen-occurrence requires occurrence 2 or later")
    occurrences = sorted(int(match.group("occurrence")) for _, match in records)
    expected_occurrences = list(range(1, args.occurrence + (1 if existing else 0)))
    if occurrences != expected_occurrences:
        _fail("finding occurrences are missing, duplicated, or out of sequence")
    roots = [row for row, match in records if int(match.group("occurrence")) == 1]
    if len(roots) != 1 or roots[0].get("id") != args.comment_id:
        _fail("--comment-id does not identify the fingerprint root comment")
    known_ids = tuple(
        cast(int, row["id"]) for row, _ in records if isinstance(row.get("id"), int)
    )
    if len(known_ids) != len(records):
        _fail("finding occurrence has no comment ID")
    _thread_state(args, known_ids)
    comment_id, replayed = _post_review_comment(
        args, marker, body, reply_to=args.comment_id
    )
    thread_replayed = _set_thread_state(args, False)
    _verify_head(args.repo, args.pr, args.head)
    print(
        json.dumps(
            {
                "comment_id": comment_id,
                "replayed": replayed,
                "thread_replayed": thread_replayed,
                "resolved": False,
                "verified": True,
            }
        )
    )


def _dispose(args: argparse.Namespace) -> None:
    marker, body = _disposition_body(args)
    args.actor = _current_login()
    _verify_head(args.repo, args.pr, args.head)
    rows = _actor_rows(_review_comments(args.repo, args.pr), args.actor)
    if _rows_have_historical_markers(rows):
        _verify_historical_threads(args.repo, args.pr, args.actor)
    root_id, occurrence_id, finding = _require_finding_occurrence(rows, args)
    if finding.group("severity") == "blocking" and args.outcome == "deferred":
        _fail("blocking local-review findings cannot be deferred")
    prior_dispositions = [
        (row, match)
        for row, match in _disposition_records(rows, args.fingerprint)
        if match.group("engine") == args.engine
        and int(match.group("round")) == args.round
        and int(match.group("occurrence")) == args.occurrence
    ]
    if len(prior_dispositions) > 1:
        _fail("finding occurrence already has a conflicting disposition")
    resumed: tuple[int, str] | None = None
    if prior_dispositions:
        row, prior = prior_dispositions[0]
        prior_body = str(row.get("body", ""))
        if prior_body != body or prior.group(0) != marker:
            content = body.partition("\n")[2]
            prior_id = row.get("id")
            if (
                prior.group("outcome") != args.outcome
                or prior.group("content_sha") != _sha256_text(content)
                or prior_body != f"{prior.group(0)}\n{content}"
                or not isinstance(prior_id, int)
                or not _is_ancestor(finding.group("head"), prior.group("head"))
                or not _is_ancestor(prior.group("head"), args.head)
            ):
                _fail("finding occurrence already has a conflicting disposition")
            _verify_marker_content(prior_body, prior, "disposition")
            resumed = (prior_id, prior_body)
    _thread_state(args, (root_id, occurrence_id))
    if resumed is None:
        comment_id, replayed = _post_review_comment(
            args, marker, body, reply_to=args.comment_id
        )
    else:
        comment_id, prior_body = resumed
        _verify_comment(args.repo, comment_id, prior_body, args.actor)
        _verify_head(args.repo, args.pr, args.head)
        replayed = True
    thread_replayed = _set_thread_state(args, True)
    _verify_head(args.repo, args.pr, args.head)
    print(
        json.dumps(
            {
                "comment_id": comment_id,
                "replayed": replayed,
                "thread_replayed": thread_replayed,
                "resolved": True,
                "verified": True,
            }
        )
    )


def _reply(args: argparse.Namespace) -> None:
    body = _read_legacy_body(args.body_file, DISPOSITION_V1)
    _verify_head(args.repo, args.pr, args.head)
    response = _json_output(
        [
            "api",
            "-X",
            "POST",
            f"repos/{args.repo}/pulls/{args.pr}/comments/{args.comment_id}/replies",
        ],
        {"body": body},
    )
    comment_id = _posted_comment_id(response)
    _verify_comment(args.repo, comment_id, body)
    _verify_head(args.repo, args.pr, args.head)
    print(json.dumps({"comment_id": comment_id, "verified": True}))


def _post_pr_comment(args: argparse.Namespace) -> None:
    body = _read_legacy_body(args.body_file, PR_V1_MARKERS)
    _verify_head(args.repo, args.pr, args.head)
    response = _json_output(
        ["api", "-X", "POST", f"repos/{args.repo}/issues/{args.pr}/comments"],
        {"body": body},
    )
    comment_id = _posted_comment_id(response)
    _verify_issue_comment(args.repo, comment_id, body)
    _verify_head(args.repo, args.pr, args.head)
    print(json.dumps({"comment_id": comment_id, "verified": True}))


def _read_result_bytes(args: argparse.Namespace) -> bytes:
    path = Path(args.result_file)
    if path.is_symlink() or not path.is_file():
        _fail("review result must be a regular non-symlink file")
    return path.read_bytes()


def _validate_result_data(
    args: argparse.Namespace, result_bytes: bytes | None = None
) -> dict[str, Any]:
    raw = _read_result_bytes(args) if result_bytes is None else result_bytes
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LedgerError("review result must contain valid UTF-8 JSON") from error
    if not isinstance(data, dict):
        _fail("review result must be a JSON object")
    required = {
        "version",
        "status",
        "engine",
        "round",
        "baseSha",
        "beforeSha",
        "afterSha",
        "classification",
        "findingFingerprints",
        "finalLaneComplete",
    }
    allowed = required | {"blocker"}
    if set(data) != required and set(data) != allowed:
        _fail("review result has missing or unknown fields")
    expected = {
        "version": PROTOCOL_VERSION,
        "engine": args.engine,
        "round": args.round,
        "baseSha": args.base,
        "beforeSha": args.before,
        "afterSha": _result_head(args),
    }
    for key, value in expected.items():
        if data.get(key) != value:
            _fail(f"review result {key} mismatch")
    status = data.get("status")
    if status not in {"clean", "changed", "blocked"}:
        _fail("review result status must be clean, changed, or blocked")
    fingerprints = data.get("findingFingerprints")
    if (
        not isinstance(fingerprints, list)
        or any(
            not isinstance(value, str) or not TOKEN_RE.fullmatch(value)
            for value in fingerprints
        )
        or len(set(fingerprints)) != len(fingerprints)
    ):
        _fail("review result findingFingerprints must be unique protocol tokens")
    if not isinstance(data.get("finalLaneComplete"), bool):
        _fail("review result finalLaneComplete must be boolean")
    classification = data.get("classification")
    if status == "clean":
        if (
            args.before != _result_head(args)
            or classification is not None
            or data["finalLaneComplete"] is not True
            or "blocker" in data
        ):
            _fail("clean review result conflicts with the observed pass")
    elif status == "changed":
        if (
            args.before == _result_head(args)
            or classification not in {"minor", "material"}
            or (args.round >= 3 and classification != "material")
            or not fingerprints
            or data["finalLaneComplete"] is not True
            or "blocker" in data
        ):
            _fail("changed review result conflicts with the observed pass")
    else:
        blocker = data.get("blocker")
        if (
            classification is not None
            or data["finalLaneComplete"] is not False
            or not isinstance(blocker, str)
            or not blocker.strip()
            or "<!-- local-review" in blocker
        ):
            _fail("blocked review result lacks a safe blocker")
    return cast(dict[str, Any], data)


def _validate_result(args: argparse.Namespace) -> None:
    result_bytes = _read_result_bytes(args)
    data = _validate_result_data(args, result_bytes)
    output = dict(data)
    output["resultSha256"] = hashlib.sha256(result_bytes).hexdigest()
    output["verified"] = True
    print(json.dumps(output, sort_keys=True))


def _write_result(args: argparse.Namespace) -> None:
    actor = _current_login()
    _verify_review_base(args.repo, args.pr, args.base, args.before)
    _verify_git_transition(args.before, args.head, args.head)
    matched = _verify_complete_v3_threads(
        args.repo, args.pr, actor, _historical_comment_ids(args)
    )
    rows = _same_round_evidence(args, matched, _transition_heads(args, matched))
    fingerprints = [fingerprint for fingerprint, _, _ in rows]
    changed = args.before != args.head
    if not changed and any(has_fix for _, has_fix, _ in rows):
        _fail("clean review results cannot have same-round fixes")
    if changed and args.classification not in {"minor", "material"}:
        _fail("changed review result requires --classification")
    if not changed and args.classification is not None:
        _fail("clean review result cannot have a classification")
    if changed and args.round >= 3 and args.classification != "material":
        _fail("round 3+ changed review results require material classification")
    if changed and not rows:
        _fail("changed review results require ledger evidence")
    if changed and not any(has_fix for _, has_fix, _ in rows):
        _fail("changed review results require a fixed ledger finding")
    if (
        changed
        and any(has_major_fix for _, _, has_major_fix in rows)
        and args.classification != "material"
    ):
        _fail("fixed blocking or major findings require material classification")
    value = {
        "version": PROTOCOL_VERSION,
        "status": "changed" if changed else "clean",
        "engine": args.engine,
        "round": args.round,
        "baseSha": args.base,
        "beforeSha": args.before,
        "afterSha": args.head,
        "classification": args.classification if changed else None,
        "findingFingerprints": fingerprints,
        "finalLaneComplete": True,
    }
    _validate_result_data(args, (json.dumps(value) + "\n").encode())
    _verify_result_evidence(args, value, actor)
    _write_result_file(args.result_file, value)
    raw = _read_result_bytes(args)
    print(
        json.dumps(
            {"resultSha256": hashlib.sha256(raw).hexdigest(), **value},
            sort_keys=True,
        )
    )


def _write_result_file(path_value: str, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = Path(path_value)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        _fail("review result destination must be a regular non-symlink file")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_blocked_result(args: argparse.Namespace) -> None:
    blocker = _read_content(args.blocker_file).strip()
    value = {
        "version": PROTOCOL_VERSION,
        "status": "blocked",
        "engine": args.engine,
        "round": args.round,
        "baseSha": args.base,
        "beforeSha": args.before,
        "afterSha": args.head,
        "classification": None,
        "findingFingerprints": [],
        "finalLaneComplete": False,
        "blocker": blocker,
    }
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _validate_result_data(args, raw)
    _write_result_file(args.result_file, value)
    print(
        json.dumps(
            {"resultSha256": hashlib.sha256(raw).hexdigest(), **value},
            sort_keys=True,
        )
    )


def _attest(args: argparse.Namespace) -> None:
    result_bytes = _read_result_bytes(args)
    data = _validate_result_data(args, result_bytes)
    actor = _current_login()
    if getattr(args, "actor", None) is not None and args.actor != actor:
        _fail("authenticated GitHub actor changed before review result attestation")
    args.actor = actor
    _verify_result_evidence(args, data, actor)
    result_hash = hashlib.sha256(result_bytes).hexdigest()
    if (
        getattr(args, "expected_result_sha256", None) is not None
        and args.expected_result_sha256 != result_hash
    ):
        _fail("review result bytes changed before attestation")
    if data["status"] == "blocked":
        _fail("blocked review results cannot be attested as complete")
    content = (
        _read_content(args.content_file)
        if args.content_file
        else (
            "No new material findings."
            if data["status"] == "clean"
            else "Review fixes completed and ledger dispositions verified."
        )
    )
    if data["status"] == "clean":
        marker = f"<!-- local-review-pass:v3 engine={args.engine} round={args.round} base={args.base} head={args.head} result-sha256={result_hash} -->"
    else:
        fingerprints = ",".join(data["findingFingerprints"])
        marker = f"<!-- local-review-complete:v3 engine={args.engine} round={args.round} base={args.base} before={args.before} head={args.head} classification={data['classification']} fingerprints={fingerprints} result-sha256={result_hash} -->"
    body = f"{marker}\n{content}"
    _verify_head(args.repo, args.pr, args.head)
    comments = _actor_rows(_issue_comments(args.repo, args.pr), actor)
    existing = _matching_attestation(comments, args.engine, args.round, body)
    replayed = existing is not None
    created = existing is None
    if existing is None:
        try:
            response = _json_output(
                ["api", "-X", "POST", f"repos/{args.repo}/issues/{args.pr}/comments"],
                {"body": body},
            )
            comment_id = _posted_comment_id(response)
        except LedgerError:
            recovered = _matching_body(
                _actor_rows(_issue_comments(args.repo, args.pr), actor), marker, body
            )
            if recovered is None:
                raise
            comment_id = recovered
            replayed = True
    else:
        comment_id = existing
    try:
        _verify_issue_comment(args.repo, comment_id, body, actor)
        _verify_review_base(args.repo, args.pr, args.base, args.before)
        _verify_head(args.repo, args.pr, args.head)
    except LedgerError as error:
        if created:
            try:
                _delete_issue_comment(args.repo, args.pr, comment_id)
            except LedgerError as rollback_error:
                raise LedgerError(
                    "attestation verification failed and rollback could not be verified: "
                    f"{rollback_error}"
                ) from error
        raise
    print(
        json.dumps(
            {
                "comment_id": comment_id,
                "replayed": replayed,
                "result_sha256": result_hash,
                "status": data["status"],
                "classification": data["classification"],
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _resolve(args: argparse.Namespace) -> None:
    args.legacy_root_actor = _current_login()
    _verify_head(args.repo, args.pr, args.head)
    replayed = _set_thread_state(args, True)
    _verify_head(args.repo, args.pr, args.head)
    print(
        json.dumps(
            {
                "thread_id": args.thread_id,
                "thread_replayed": replayed,
                "resolved": True,
                "verified": True,
            }
        )
    )


def _reconcile(args: argparse.Namespace) -> None:
    actor = _current_login()
    _verify_head(args.repo, args.pr, args.head)
    comments = _actor_rows(_review_comments(args.repo, args.pr), actor)
    if _rows_have_historical_markers(comments):
        _verify_historical_threads(args.repo, args.pr, actor)
    finding_rows: list[dict[str, Any]] = []
    disposition_rows: list[dict[str, Any]] = []
    for row in comments:
        body = str(row.get("body", ""))
        finding = FINDING_V3_RE.search(body)
        disposition = DISPOSITION_V3_RE.search(body)
        if finding and finding.group("fingerprint") == args.fingerprint:
            finding_rows.append({"id": row.get("id"), **finding.groupdict()})
        if disposition and disposition.group("fingerprint") == args.fingerprint:
            disposition_rows.append({"id": row.get("id"), **disposition.groupdict()})
    occurrences = sorted(int(row["occurrence"]) for row in finding_rows)
    sequence_valid = occurrences == list(range(1, len(occurrences) + 1))
    finding_keys = {
        (row["engine"], row["round"], row["fingerprint"], row["occurrence"])
        for row in finding_rows
    }
    disposition_keys = [
        (row["engine"], row["round"], row["fingerprint"], row["occurrence"])
        for row in disposition_rows
    ]
    disposed = {int(row["occurrence"]) for row in disposition_rows}
    ledger_valid = (
        sequence_valid
        and len(disposition_keys) == len(set(disposition_keys))
        and set(disposition_keys).issubset(finding_keys)
    )
    undisposed = [value for value in occurrences if value not in disposed]
    root_ids = [
        row.get("id")
        for row in finding_rows
        if int(row["occurrence"]) == 1 and isinstance(row.get("id"), int)
    ]
    thread_id: str | None = None
    thread_resolved: bool | None = None
    if ledger_valid and len(root_ids) == 1:
        matching_threads = [
            thread
            for thread in _review_threads(args.repo, args.pr)
            if any(
                isinstance(comment, dict) and comment.get("databaseId") == root_ids[0]
                for comment in cast(dict[str, Any], thread["comments"])["nodes"]
            )
        ]
        if len(matching_threads) != 1:
            _fail("could not identify exactly one root review thread")
        candidate = matching_threads[0]
        if not isinstance(candidate.get("id"), str) or not isinstance(
            candidate.get("isResolved"), bool
        ):
            _fail("root review thread has an unexpected shape")
        thread_id = cast(str, candidate["id"])
        thread_resolved = cast(bool, candidate["isResolved"])
    next_action = (
        "repair-sequence"
        if not ledger_valid
        else "dispose"
        if undisposed
        else "dispose"
        if occurrences and thread_resolved is not True
        else "reopen-occurrence"
        if occurrences
        else "post-finding"
    )
    print(
        json.dumps(
            {
                "findings": finding_rows,
                "dispositions": disposition_rows,
                "sequenceValid": sequence_valid,
                "ledgerValid": ledger_valid,
                "nextOccurrence": len(occurrences) + 1 if sequence_valid else None,
                "undisposedOccurrences": undisposed,
                "threadId": thread_id,
                "threadResolved": thread_resolved,
                "nextAction": next_action,
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="GitHub OWNER/REPO")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True, help="Exact 40-character PR head SHA")


def _add_protocol_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--engine", required=True, choices=("codex", "claude"))
    parser.add_argument("--round", required=True, type=int)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--occurrence", type=int, default=1)


def _add_result_arguments(parser: argparse.ArgumentParser, *, github: bool) -> None:
    if github:
        _add_common(parser)
    else:
        parser.add_argument("--head", required=True)
    parser.add_argument("--engine", required=True, choices=("codex", "claude"))
    parser.add_argument("--round", required=True, type=int)
    parser.add_argument("--base", required=True)
    parser.add_argument("--before", required=True)
    parser.add_argument("--result-file", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol-version", action="version", version=str(PROTOCOL_VERSION)
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add_anchor_arguments(command: argparse.ArgumentParser) -> None:
        _add_common(command)
        command.add_argument("--path", required=True)
        anchor = command.add_mutually_exclusive_group(required=True)
        anchor.add_argument("--line", type=int)
        anchor.add_argument("--file-level", action="store_true")
        command.add_argument("--side", choices=("RIGHT", "LEFT"), default="RIGHT")

    preflight = commands.add_parser("preflight-anchor")
    add_anchor_arguments(preflight)
    preflight.set_defaults(handler=_preflight_anchor)

    finding = commands.add_parser("post-finding")
    add_anchor_arguments(finding)
    content = finding.add_mutually_exclusive_group(required=True)
    content.add_argument("--content-file")
    content.add_argument("--body-file", help="Legacy v1 path or - for stdin")
    finding.add_argument("--engine", choices=("codex", "claude"))
    finding.add_argument("--round", type=int)
    finding.add_argument("--fingerprint")
    finding.add_argument("--occurrence", type=int, default=1)
    finding.add_argument("--severity", choices=("blocking", "major", "minor", "nit"))
    finding.add_argument("--lens")
    finding.set_defaults(handler=_post_finding)

    recurrence = commands.add_parser("reopen-occurrence")
    _add_common(recurrence)
    _add_protocol_identity(recurrence)
    recurrence.add_argument(
        "--severity", required=True, choices=("blocking", "major", "minor", "nit")
    )
    recurrence.add_argument("--lens", required=True)
    recurrence.add_argument("--comment-id", required=True, type=int)
    recurrence.add_argument("--thread-id", required=True)
    recurrence.add_argument("--content-file", required=True)
    recurrence.set_defaults(handler=_reopen_occurrence)

    dispose = commands.add_parser("dispose")
    _add_common(dispose)
    _add_protocol_identity(dispose)
    dispose.add_argument(
        "--outcome", required=True, choices=("fixed", "dismissed", "deferred")
    )
    dispose.add_argument("--comment-id", required=True, type=int)
    dispose.add_argument("--thread-id", required=True)
    dispose.add_argument("--content-file", required=True)
    dispose.set_defaults(handler=_dispose)

    reply = commands.add_parser("reply")
    _add_common(reply)
    reply.add_argument("--comment-id", required=True, type=int)
    reply.add_argument(
        "--body-file", required=True, help="Legacy v1 path or - for stdin"
    )
    reply.set_defaults(handler=_reply)

    comment = commands.add_parser("post-pr-comment")
    _add_common(comment)
    comment.add_argument(
        "--body-file", required=True, help="Legacy v1 path or - for stdin"
    )
    comment.set_defaults(handler=_post_pr_comment)

    validate = commands.add_parser("validate-result")
    _add_result_arguments(validate, github=False)
    validate.set_defaults(handler=_validate_result)

    write_result = commands.add_parser("write-result")
    _add_result_arguments(write_result, github=True)
    write_result.add_argument("--historical-comment-ids-file")
    write_result.add_argument("--classification", choices=("minor", "material"))
    write_result.set_defaults(handler=_write_result)

    write_blocked = commands.add_parser("write-blocked-result")
    _add_result_arguments(write_blocked, github=False)
    write_blocked.add_argument("--blocker-file", required=True)
    write_blocked.set_defaults(handler=_write_blocked_result)

    attest = commands.add_parser("attest")
    _add_result_arguments(attest, github=True)
    attest.add_argument("--content-file")
    attest.add_argument("--threads-file")
    attest.add_argument("--expected-threads-sha256")
    attest.add_argument("--allowed-heads-file")
    attest.add_argument("--actor")
    attest.add_argument("--historical-comment-ids-file")
    attest.add_argument("--expected-result-sha256")
    attest.set_defaults(handler=_attest)

    verify_ledger = commands.add_parser("verify-ledger")
    _add_common(verify_ledger)
    verify_ledger.add_argument("--result-head")
    verify_ledger.add_argument("--threads-file")
    verify_ledger.add_argument("--expected-threads-sha256")
    verify_ledger.add_argument("--actor")
    verify_ledger.add_argument("--engine", choices=("codex", "claude"))
    verify_ledger.add_argument("--round", type=int)
    verify_ledger.add_argument("--base")
    verify_ledger.add_argument("--before")
    verify_ledger.add_argument("--result-file")
    verify_ledger.add_argument("--allowed-heads-file")
    verify_ledger.add_argument("--historical-comment-ids-file")
    verify_ledger.set_defaults(handler=_verify_ledger)

    resolve = commands.add_parser("resolve")
    _add_common(resolve)
    resolve.add_argument("--thread-id", required=True)
    resolve.set_defaults(handler=_resolve)

    reconcile = commands.add_parser("reconcile")
    _add_common(reconcile)
    reconcile.add_argument("--fingerprint", required=True)
    reconcile.set_defaults(handler=_reconcile)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    for name in ("head", "base", "before", "result_head"):
        value = getattr(args, name, None)
        if value is not None and not SHA_RE.fullmatch(value):
            _fail(f"--{name} must be a full 40-character lowercase commit SHA")
    round_number = getattr(args, "round", None)
    if round_number is not None and round_number < 1:
        _fail("--round must be a positive integer")
    if getattr(args, "occurrence", 1) < 1:
        _fail("--occurrence must be a positive integer")
    if getattr(args, "content_file", None) and args.command in {
        "post-finding",
        "reopen-occurrence",
        "dispose",
    }:
        required = ["engine", "round", "fingerprint"]
        if args.command == "post-finding":
            required.extend(("severity", "lens"))
        missing = [name for name in required if getattr(args, name, None) is None]
        if missing:
            _fail(
                "v3 content mode requires "
                + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
            )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _validate_args(args)
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LedgerError, OSError) as error:
        print(f"review-ledger: {error}", file=sys.stderr)
        raise SystemExit(1) from error
