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
CURRENT_ACTOR: str | None = None
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


def _current_actor() -> str:
    global CURRENT_ACTOR
    if CURRENT_ACTOR is None:
        actor = _run_gh(["api", "user", "--jq", ".login"]).strip()
        if not actor:
            _fail("could not resolve the authenticated GitHub actor")
        CURRENT_ACTOR = actor
    return CURRENT_ACTOR


def _authenticated_rows(
    rows: list[dict[str, Any]], *, graphql: bool = False
) -> list[dict[str, Any]]:
    actor = _current_actor()
    identity_key = "author" if graphql else "user"
    return [
        row
        for row in rows
        if isinstance(row.get(identity_key), dict)
        and row[identity_key].get("login") == actor
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
        _fail(f"PR head mismatch: expected {expected_head}, found {actual or '<empty>'}")


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
            ["api", "--paginate", "--slurp", f"repos/{repo}/pulls/{pr}/files?per_page=100"]
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
    return _authenticated_rows(_flatten_pages(
        _json_output(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/pulls/{pr}/comments?per_page=100",
            ]
        ),
        "review-comments",
    ))


def _issue_comments(repo: str, pr: int) -> list[dict[str, Any]]:
    return _authenticated_rows(_flatten_pages(
        _json_output(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/issues/{pr}/comments?per_page=100",
            ]
        ),
        "PR-comments",
    ))


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
    nearest = sorted(valid, key=lambda candidate: (abs(candidate - line), candidate))[:5]
    candidates = ", ".join(str(candidate) for candidate in nearest) or "none"
    _fail(
        f"line {line} is not an exact {side} anchor in GitHub's PR patch; "
        f"nearest valid lines: {candidates}"
    )


def _verify_comment(repo: str, comment_id: int, expected_body: str) -> None:
    response = _json_output(["api", f"repos/{repo}/pulls/comments/{comment_id}"])
    if (
        not isinstance(response, dict)
        or response.get("body") != expected_body
        or not isinstance(response.get("user"), dict)
        or response["user"].get("login") != _current_actor()
    ):
        _fail(f"could not verify review comment {comment_id} after posting")


def _verify_issue_comment(repo: str, comment_id: int, expected_body: str) -> None:
    response = _json_output(["api", f"repos/{repo}/issues/comments/{comment_id}"])
    if (
        not isinstance(response, dict)
        or response.get("body") != expected_body
        or not isinstance(response.get("user"), dict)
        or response["user"].get("login") != _current_actor()
    ):
        _fail(f"could not verify PR comment {comment_id} after posting")


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


def _protocol_match(
    body: str, pattern: re.Pattern[str], marker: str
) -> re.Match[str] | None:
    if marker not in body:
        return None
    match = pattern.match(body)
    if match is None or not body[match.end():].startswith("\n"):
        _fail(f"authenticated {marker} record is malformed")
    content = body[match.end() + 1:]
    if _sha256_text(content) != match.group("content_sha"):
        _fail(f"authenticated {marker} record has an invalid content hash")
    return match


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
        _fail("authenticated historical local-review:v3 record is malformed")
    return matches[0]


def _finding_match(body: str) -> re.Match[str] | None:
    if _pseudo_v3_match(body) is not None:
        return None
    return _protocol_match(body, FINDING_V3_RE, "<!-- local-review:v3")


def _disposition_match(body: str) -> re.Match[str] | None:
    return _protocol_match(
        body, DISPOSITION_V3_RE, "<!-- local-review-disposition:v3"
    )


def _finding_records(
    rows: list[dict[str, Any]], fingerprint: str
) -> list[tuple[dict[str, Any], re.Match[str]]]:
    records: list[tuple[dict[str, Any], re.Match[str]]] = []
    for row in rows:
        match = _finding_match(str(row.get("body", "")))
        if match is not None and match.group("fingerprint") == fingerprint:
            records.append((row, match))
    return records


def _rows_have_pseudo_v3(rows: list[dict[str, Any]]) -> bool:
    return any(
        PSEUDO_V3_RE.search(str(row.get("body", ""))) is not None for row in rows
    )


def _require_disposition_consistency(
    rows: list[dict[str, Any]], args: argparse.Namespace, body: str
) -> None:
    matches: list[dict[str, Any]] = []
    for row in rows:
        match = _disposition_match(str(row.get("body", "")))
        if (
            match is not None
            and match.group("engine") == args.engine
            and int(match.group("round")) == args.round
            and match.group("fingerprint") == args.fingerprint
            and int(match.group("occurrence")) == args.occurrence
        ):
            matches.append(row)
    if len(matches) > 1:
        _fail("disposition identity is duplicated")
    if matches and matches[0].get("body") != body:
        _fail("disposition identity already exists with conflicting content or outcome")


def _require_finding_root(
    rows: list[dict[str, Any]], args: argparse.Namespace
) -> list[tuple[dict[str, Any], re.Match[str]]]:
    records = _finding_records(rows, args.fingerprint)
    roots = [
        row
        for row, match in records
        if int(match.group("occurrence")) == 1
    ]
    if len(roots) != 1 or roots[0].get("id") != args.comment_id:
        _fail("--comment-id does not identify the fingerprint root comment")
    return records


def _require_finding_occurrence(
    rows: list[dict[str, Any]], args: argparse.Namespace
) -> re.Match[str]:
    records = _require_finding_root(rows, args)
    matches = [
        match
        for _, match in records
        if match.group("engine") == args.engine
        and int(match.group("round")) == args.round
        and int(match.group("occurrence")) == args.occurrence
    ]
    if len(matches) != 1:
        _fail("disposition does not identify exactly one existing finding occurrence")
    return matches[0]


def _require_prior_occurrences_disposed(
    rows: list[dict[str, Any]],
    records: list[tuple[dict[str, Any], re.Match[str]]],
    next_occurrence: int,
) -> None:
    dispositions = [
        match
        for row in rows
        if (match := _disposition_match(str(row.get("body", "")))) is not None
    ]
    for _, finding in records:
        if int(finding.group("occurrence")) >= next_occurrence:
            continue
        matches = [
            disposition
            for disposition in dispositions
            if disposition.group("engine") == finding.group("engine")
            and disposition.group("round") == finding.group("round")
            and disposition.group("fingerprint") == finding.group("fingerprint")
            and disposition.group("occurrence") == finding.group("occurrence")
        ]
        if len(matches) != 1:
            _fail("every prior finding occurrence must have exactly one disposition")


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
    existing = _matching_body(_review_comments(args.repo, args.pr), marker, body)
    if existing is not None:
        _verify_comment(args.repo, existing, body)
        _verify_head(args.repo, args.pr, args.head)
        return existing, True
    if reply_to is None:
        payload: dict[str, Any] = {"body": body, "commit_id": args.head, "path": args.path}
        if args.file_level:
            payload["subject_type"] = "file"
        else:
            payload.update({"line": args.line, "side": args.side})
        endpoint = f"repos/{args.repo}/pulls/{args.pr}/comments"
    else:
        payload = {"body": body}
        endpoint = f"repos/{args.repo}/pulls/{args.pr}/comments/{reply_to}/replies"
    replayed = False
    try:
        response = _json_output(["api", "-X", "POST", endpoint], payload)
        comment_id = _posted_comment_id(response)
    except LedgerError:
        recovered = _matching_body(_review_comments(args.repo, args.pr), marker, body)
        if recovered is None:
            raise
        comment_id = recovered
        replayed = True
    _verify_comment(args.repo, comment_id, body)
    _verify_head(args.repo, args.pr, args.head)
    return comment_id, replayed


def _thread_state(args: argparse.Namespace) -> bool:
    query = """
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      comments(first: 100) {
        nodes { databaseId }
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
    if not isinstance(thread.get("isResolved"), bool):
        _fail(f"review thread {args.thread_id} has invalid resolution state")
    comment_id = getattr(args, "comment_id", None)
    if comment_id is not None:
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
        ids = [
            row.get("databaseId")
            for row in nodes
            if isinstance(row, dict)
        ]
        if comment_id not in ids:
            _fail("--comment-id does not belong to --thread-id")
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
        if thread.get("id") != args.thread_id or thread.get("isResolved") is not resolved:
            _fail(f"GitHub did not set review thread {args.thread_id} resolved={resolved}")
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


def _preflight_anchor(args: argparse.Namespace) -> None:
    _verify_head(args.repo, args.pr, args.head)
    files = _pr_files(args.repo, args.pr)
    line = None if args.file_level else args.line
    side = None if args.file_level else args.side
    _validate_anchor(files, args.path, line, side)
    print(json.dumps({"anchor": "file" if args.file_level else f"{args.side}:{args.line}", "path": args.path, "verified": True}))


def _post_finding(args: argparse.Namespace) -> None:
    if args.content_file:
        marker, body = _finding_body(args)
    else:
        marker = FINDING_V1
        body = _read_legacy_body(args.body_file, marker)
    _verify_head(args.repo, args.pr, args.head)
    files = _pr_files(args.repo, args.pr)
    line = None if args.file_level else args.line
    side = None if args.file_level else args.side
    _validate_anchor(files, args.path, line, side)
    if args.content_file:
        rows = _review_comments(args.repo, args.pr)
        if _rows_have_pseudo_v3(rows):
            _verify_pseudo_v3_history(_review_threads(args.repo, args.pr))
        existing = _matching_body(rows, marker, body)
        records = _finding_records(rows, args.fingerprint)
        if existing is None and records:
            _fail("fingerprint already has a root thread; use reopen-occurrence")
        if args.occurrence != 1:
            _fail("post-finding creates occurrence 1; use reopen-occurrence later")
        comment_id, replayed = _post_review_comment(args, marker, body)
    else:
        payload: dict[str, Any] = {"body": body, "commit_id": args.head, "path": args.path}
        if args.file_level:
            payload["subject_type"] = "file"
        else:
            payload.update({"line": args.line, "side": args.side})
        response = _json_output(["api", "-X", "POST", f"repos/{args.repo}/pulls/{args.pr}/comments"], payload)
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
    _verify_head(args.repo, args.pr, args.head)
    rows = _review_comments(args.repo, args.pr)
    if _rows_have_pseudo_v3(rows):
        _verify_pseudo_v3_history(_review_threads(args.repo, args.pr))
    records = _finding_records(rows, args.fingerprint)
    existing = _matching_body(rows, marker, body)
    if args.occurrence < 2:
        _fail("reopen-occurrence requires occurrence 2 or later")
    _require_finding_root(rows, args)
    _require_prior_occurrences_disposed(rows, records, args.occurrence)
    if existing is None:
        occurrences = sorted(int(match.group("occurrence")) for _, match in records)
        if occurrences != list(range(1, args.occurrence)):
            _fail("finding occurrences are missing, duplicated, or out of sequence")
    _thread_state(args)
    comment_id, replayed = _post_review_comment(
        args, marker, body, reply_to=args.comment_id
    )
    thread_replayed = _set_thread_state(args, False)
    _verify_head(args.repo, args.pr, args.head)
    print(json.dumps({"comment_id": comment_id, "replayed": replayed, "thread_replayed": thread_replayed, "resolved": False, "verified": True}))


def _dispose(args: argparse.Namespace) -> None:
    marker, body = _disposition_body(args)
    _verify_head(args.repo, args.pr, args.head)
    rows = _review_comments(args.repo, args.pr)
    if _rows_have_pseudo_v3(rows):
        _verify_pseudo_v3_history(_review_threads(args.repo, args.pr))
    finding = _require_finding_occurrence(rows, args)
    if finding.group("severity") == "blocking" and args.outcome == "deferred":
        _fail("blocking local-review findings cannot be deferred")
    _require_disposition_consistency(rows, args, body)
    _thread_state(args)
    comment_id, replayed = _post_review_comment(
        args, marker, body, reply_to=args.comment_id
    )
    thread_replayed = _set_thread_state(args, True)
    _verify_head(args.repo, args.pr, args.head)
    print(json.dumps({"comment_id": comment_id, "replayed": replayed, "thread_replayed": thread_replayed, "resolved": True, "verified": True}))


def _reply(args: argparse.Namespace) -> None:
    body = _read_legacy_body(args.body_file, DISPOSITION_V1)
    _verify_head(args.repo, args.pr, args.head)
    response = _json_output(
        ["api", "-X", "POST", f"repos/{args.repo}/pulls/{args.pr}/comments/{args.comment_id}/replies"],
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


def _read_result_bytes(path_value: str) -> bytes:
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        _fail("review result must be a regular non-symlink file")
    return path.read_bytes()


def _result_head(args: argparse.Namespace) -> str:
    return cast(str, getattr(args, "result_head", None) or args.head)


def _validate_result_data(
    args: argparse.Namespace, raw: bytes | None = None
) -> dict[str, Any]:
    if raw is None:
        raw = _read_result_bytes(args.result_file)
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LedgerError("review result must contain valid UTF-8 JSON") from error
    if not isinstance(data, dict):
        _fail("review result must be a JSON object")
    required = {"version", "status", "engine", "round", "baseSha", "beforeSha", "afterSha", "classification", "findingFingerprints", "finalLaneComplete"}
    allowed = required | {"blocker"}
    if set(data) != required and set(data) != allowed:
        _fail("review result has missing or unknown fields")
    if type(data.get("version")) is not int or type(data.get("round")) is not int:
        _fail("review result version and round must be integers")
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
    if not isinstance(fingerprints, list) or any(not isinstance(value, str) or not TOKEN_RE.fullmatch(value) for value in fingerprints) or len(set(fingerprints)) != len(fingerprints):
        _fail("review result findingFingerprints must be unique protocol tokens")
    if not isinstance(data.get("finalLaneComplete"), bool):
        _fail("review result finalLaneComplete must be boolean")
    classification = data.get("classification")
    if status == "clean":
        if args.before != _result_head(args) or classification is not None or data["finalLaneComplete"] is not True or "blocker" in data:
            _fail("clean review result conflicts with the observed pass")
    elif status == "changed":
        if args.before == _result_head(args) or classification not in {"minor", "material"} or (args.round >= 3 and classification != "material") or not fingerprints or data["finalLaneComplete"] is not True or "blocker" in data:
            _fail("changed review result conflicts with the observed pass")
    else:
        blocker = data.get("blocker")
        if classification is not None or data["finalLaneComplete"] is not False or not isinstance(blocker, str) or not blocker.strip() or "<!-- local-review" in blocker:
            _fail("blocked review result lacks a safe blocker")
    return cast(dict[str, Any], data)


def _validate_result(args: argparse.Namespace) -> None:
    raw = _read_result_bytes(args.result_file)
    data = _validate_result_data(args, raw)
    output = dict(data)
    output["resultSha256"] = hashlib.sha256(raw).hexdigest()
    output["verified"] = True
    print(json.dumps(output, sort_keys=True))


def _same_round_dispositions(
    args: argparse.Namespace,
    threads: list[dict[str, Any]],
    allowed_heads: dict[str, int],
    historical_comment_ids: set[int] | None = None,
) -> list[tuple[str, bool, bool, bool]]:
    """Return unique fingerprint, fix, major-fix, and nonblocking-fix evidence."""
    evidence: dict[str, tuple[bool, bool, bool]] = {}
    fingerprint_threads: dict[str, int] = {}
    for thread_index, thread in enumerate(threads):
        findings, dispositions, _, _ = _thread_protocol_records(
            thread, historical_comment_ids
        )
        for finding_index, finding in findings:
            if (
                finding.group("engine") != args.engine
                or int(finding.group("round")) != args.round
            ):
                continue
            fingerprint = finding.group("fingerprint")
            finding_head = finding.group("head")
            prior_thread = fingerprint_threads.setdefault(fingerprint, thread_index)
            if prior_thread != thread_index:
                _fail("same-round finding fingerprint has duplicate root threads")
            if finding_head not in allowed_heads:
                historical_matches = _matching_dispositions(
                    finding_index, finding, dispositions
                )
                if thread.get("isResolved") is not True or len(historical_matches) != 1:
                    _fail(
                        "same-round finding outside the observed transition is not settled"
                    )
                if (
                    finding.group("severity") == "blocking"
                    and historical_matches[0].group("outcome") != "fixed"
                ):
                    _fail("blocking local-review findings must be fixed")
                historical_disposition = historical_matches[0]
                if historical_disposition.group("outcome") == "fixed":
                    disposition_head = historical_disposition.group("head")
                    if disposition_head == finding_head:
                        _fail(
                            "historical fixed disposition is not a forward transition"
                        )
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
                        _fail(
                            "historical fixed disposition is not a forward transition"
                        )
                # Settled historical occurrences are not evidence for this
                # before-to-after transition.
                continue
            matches = [
                disposition
                for disposition in _matching_dispositions(
                    finding_index, finding, dispositions
                )
                if disposition.group("head") in allowed_heads
                and allowed_heads[disposition.group("head")]
                >= allowed_heads[finding_head]
                and (
                    disposition.group("outcome") != "fixed"
                    or allowed_heads[disposition.group("head")]
                    > allowed_heads[finding_head]
                )
            ]
            if thread.get("isResolved") is not True or len(matches) != 1:
                _fail("same-round finding lacks one resolved matching disposition")
            disposition = matches[0]
            if (
                finding.group("severity") == "blocking"
                and disposition.group("outcome") != "fixed"
            ):
                _fail("blocking local-review findings must be fixed")
            fixed = disposition.group("outcome") == "fixed"
            fixed_major = fixed and finding.group("severity") in {
                "blocking",
                "major",
            }
            fixed_nonblocking = fixed and finding.group("severity") != "blocking"
            previous = evidence.get(fingerprint, (False, False, False))
            evidence[fingerprint] = (
                previous[0] or fixed,
                previous[1] or fixed_major,
                previous[2] or fixed_nonblocking,
            )
    return [
        (fingerprint, *evidence[fingerprint]) for fingerprint in sorted(evidence)
    ]


def _write_result(args: argparse.Namespace) -> None:
    threads = (
        _load_review_threads(args.threads_file)
        if args.threads_file is not None
        else _review_threads(args.repo, args.pr)
    )
    historical_comment_ids = _historical_comment_ids(args)
    _verify_thread_dispositions(threads, historical_comment_ids, repo=args.repo)
    if args.allowed_heads_file is None:
        comparison = subprocess.run(
            ["git", "rev-list", "--reverse", "--ancestry-path", f"{args.before}..{args.head}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if comparison.returncode != 0:
            _fail("could not derive the forward review transition")
        values = [args.before, *comparison.stdout.splitlines()]
        if values[-1] != args.head or len(set(values)) != len(values):
            _fail("review result transition is not forward-only")
        allowed_heads = {value: index for index, value in enumerate(values)}
    else:
        allowed_heads = _load_allowed_heads(args)
    dispositions = _same_round_dispositions(
        args, threads, allowed_heads, historical_comment_ids
    )
    changed = args.before != args.head
    if not changed and any(has_fix for _, has_fix, _, _ in dispositions):
        _fail("clean review results cannot have same-round fixes")
    if changed and args.classification not in {"minor", "material"}:
        _fail("changed review result requires --classification")
    if not changed and args.classification is not None:
        _fail("clean review result cannot have a classification")
    if changed and args.round >= 3 and args.classification != "material":
        _fail("round 3+ changed review results require material classification")
    if changed and not dispositions:
        _fail("changed review results require ledger evidence")
    if changed and not any(has_fix for _, has_fix, _, _ in dispositions):
        _fail("changed review results require a fixed ledger finding")
    if changed and args.round >= 3 and any(
        has_nonblocking_fix for _, _, _, has_nonblocking_fix in dispositions
    ):
        _fail("convergence review results cannot fix non-blocking findings")
    if (
        changed
        and args.classification != "material"
        and any(has_major_fix for _, _, has_major_fix, _ in dispositions)
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
        "findingFingerprints": [row[0] for row in dispositions],
        "finalLaneComplete": True,
    }
    _write_result_file(args.result_file, value)
    raw = _read_result_bytes(args.result_file)
    print(json.dumps({"resultSha256": hashlib.sha256(raw).hexdigest(), **value}, sort_keys=True))


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
    print(json.dumps({"resultSha256": hashlib.sha256(raw).hexdigest(), **value}, sort_keys=True))


def _attest(args: argparse.Namespace) -> None:
    raw = _read_result_bytes(args.result_file)
    data = _validate_result_data(args, raw)
    result_hash = hashlib.sha256(raw).hexdigest()
    if result_hash != args.expected_result_sha256:
        _fail("review result changed before attestation")
    if data["status"] == "blocked":
        _fail("blocked review results cannot be attested as complete")
    threads = _load_review_threads(args.threads_file)
    historical_comment_ids = _historical_comment_ids(args)
    _verify_thread_dispositions(threads, historical_comment_ids, repo=args.repo)
    _verify_result_evidence(
        args, threads, data=data, historical_comment_ids=historical_comment_ids
    )
    content = _read_content(args.content_file) if args.content_file else (
        "No new material findings." if data["status"] == "clean" else "Review fixes completed and ledger dispositions verified."
    )
    if data["status"] == "clean":
        marker = f"<!-- local-review-pass:v3 engine={args.engine} round={args.round} base={args.base} head={args.head} result-sha256={result_hash} -->"
    else:
        fingerprints = ",".join(data["findingFingerprints"])
        marker = f"<!-- local-review-complete:v3 engine={args.engine} round={args.round} base={args.base} before={args.before} head={args.head} classification={data['classification']} fingerprints={fingerprints} result-sha256={result_hash} -->"
    body = f"{marker}\n{content}"
    _verify_head(args.repo, args.pr, args.head)
    existing = _matching_body(_issue_comments(args.repo, args.pr), marker, body)
    replayed = existing is not None
    if existing is None:
        try:
            response = _json_output(["api", "-X", "POST", f"repos/{args.repo}/issues/{args.pr}/comments"], {"body": body})
            comment_id = _posted_comment_id(response)
        except LedgerError:
            recovered = _matching_body(_issue_comments(args.repo, args.pr), marker, body)
            if recovered is None:
                raise
            comment_id = recovered
            replayed = True
    else:
        comment_id = existing
    _verify_issue_comment(args.repo, comment_id, body)
    _verify_head(args.repo, args.pr, args.head)
    print(json.dumps({"comment_id": comment_id, "replayed": replayed, "result_sha256": result_hash, "verified": True}))


def _resolve(args: argparse.Namespace) -> None:
    _verify_head(args.repo, args.pr, args.head)
    _set_thread_state(args, True)
    _verify_head(args.repo, args.pr, args.head)
    print(json.dumps({"thread_id": args.thread_id, "resolved": True}))


def _reconcile(args: argparse.Namespace) -> None:
    _verify_head(args.repo, args.pr, args.head)
    comments = _review_comments(args.repo, args.pr)
    if _rows_have_pseudo_v3(comments):
        _verify_pseudo_v3_history(_review_threads(args.repo, args.pr))
    finding_rows: list[dict[str, Any]] = []
    disposition_rows: list[dict[str, Any]] = []
    for row in comments:
        body = str(row.get("body", ""))
        finding = _finding_match(body)
        disposition = _disposition_match(body)
        if finding and finding.group("fingerprint") == args.fingerprint:
            finding_rows.append({"id": row.get("id"), **finding.groupdict()})
        if disposition and disposition.group("fingerprint") == args.fingerprint:
            disposition_rows.append({"id": row.get("id"), **disposition.groupdict()})
    occurrences = sorted(int(row["occurrence"]) for row in finding_rows)
    sequence_valid = occurrences == list(range(1, len(occurrences) + 1))
    disposed = {int(row["occurrence"]) for row in disposition_rows}
    ledger_valid = (
        sequence_valid
        and len(disposed) == len(disposition_rows)
        and disposed.issubset(set(occurrences))
    )
    undisposed = [value for value in occurrences if value not in disposed]
    next_action = (
        "repair-sequence"
        if not ledger_valid
        else "dispose"
        if undisposed
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
                "nextAction": next_action,
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _load_review_threads(path_value: str) -> list[dict[str, Any]]:
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        _fail("review threads must be a regular non-symlink file")
    try:
        pages = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LedgerError("review threads must contain valid UTF-8 JSON") from error
    if not isinstance(pages, list) or not pages:
        _fail("review threads response has an unexpected shape")
    threads: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict) or page.get("errors"):
            _fail("GitHub review threads response contains errors")
        try:
            connection = page["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
        except (KeyError, TypeError) as error:
            raise LedgerError("GitHub review threads response has an unexpected shape") from error
        if (
            not isinstance(nodes, list)
            or not isinstance(page_info, dict)
            or not isinstance(page_info.get("hasNextPage"), bool)
        ):
            _fail("GitHub review threads response has an unexpected shape")
        for thread in nodes:
            if not isinstance(thread, dict):
                _fail("GitHub review thread has an unexpected shape")
            try:
                comments = thread["comments"]
                comment_nodes = comments["nodes"]
                comments_page_info = comments["pageInfo"]
            except (KeyError, TypeError) as error:
                raise LedgerError("GitHub review thread comments have an unexpected shape") from error
            if (
                not isinstance(comment_nodes, list)
                or not isinstance(comments_page_info, dict)
                or comments_page_info.get("hasNextPage") is not False
            ):
                _fail("GitHub review thread comments are incomplete")
            threads.append(thread)
    if pages[-1]["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"].get("hasNextPage") is not False:
        _fail("GitHub review thread pages are incomplete")
    return threads


def _review_threads(repo: str, pr: int) -> list[dict[str, Any]]:
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
    if not isinstance(pages, list) or not pages:
        _fail("GitHub review-thread response has an unexpected shape")
    threads: list[dict[str, Any]] = []
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


def _historical_comment_ids(args: argparse.Namespace) -> set[int]:
    path_value = getattr(args, "historical_comment_ids_file", None) or os.environ.get(
        "AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE"
    )
    if not path_value:
        return set()
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


def _verify_pseudo_v3_history(
    threads: list[dict[str, Any]], historical_comment_ids: set[int] | None = None
) -> None:
    actor = _current_actor()
    for thread in threads:
        comments = thread.get("comments")
        if not isinstance(comments, dict):
            _fail("GitHub review thread omitted comments")
        nodes = comments.get("nodes")
        page_info = comments.get("pageInfo")
        if (
            not isinstance(nodes, list)
            or not isinstance(page_info, dict)
            or page_info.get("hasNextPage") is not False
        ):
            _fail("GitHub review thread comments are incomplete")
        for index, row in enumerate(nodes):
            if not isinstance(row, dict):
                _fail("GitHub review comment has an unexpected shape")
            body = str(row.get("body", ""))
            author = row.get("author")
            if not isinstance(author, dict) or not isinstance(author.get("login"), str):
                if "<!-- local-review" in body:
                    _fail("could not establish local-review comment ownership")
                continue
            if author.get("login") != actor:
                continue
            if "<!-- local-review:v3" not in body:
                continue
            pseudo = _pseudo_v3_match(body)
            if pseudo is None:
                _protocol_match(body, FINDING_V3_RE, "<!-- local-review:v3")
                continue
            if (
                historical_comment_ids is not None
                and row.get("databaseId") not in historical_comment_ids
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
                for reply in nodes[index + 1 :]
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
        or any(not isinstance(value, str) or not SHA_RE.fullmatch(value) for value in values)
        or len(set(values)) != len(values)
        or values[0] != args.before
        or values[-1] != _result_head(args)
    ):
        _fail("allowed transition heads do not match the observed review transition")
    for before, after in zip(values, values[1:]):
        comparison = _json_output(
            ["api", f"repos/{args.repo}/compare/{before}...{after}"]
        )
        merge_base = comparison.get("merge_base_commit") if isinstance(comparison, dict) else None
        if (
            not isinstance(comparison, dict)
            or comparison.get("status") != "ahead"
            or not isinstance(merge_base, dict)
            or merge_base.get("sha") != before
        ):
            _fail("allowed transition heads are not forward-only")
    return {value: index for index, value in enumerate(values)}


def _thread_protocol_records(
    thread: dict[str, Any], historical_comment_ids: set[int] | None = None
) -> tuple[
    list[tuple[int, re.Match[str]]],
    list[tuple[int, re.Match[str]]],
    list[tuple[int, re.Match[str]]],
    list[tuple[int, re.Match[str]]],
]:
    _verify_pseudo_v3_history([thread], historical_comment_ids)
    comments = cast(dict[str, Any], thread["comments"])["nodes"]
    findings_v3: list[tuple[int, re.Match[str]]] = []
    dispositions_v3: list[tuple[int, re.Match[str]]] = []
    findings_v1: list[tuple[int, re.Match[str]]] = []
    dispositions_v1: list[tuple[int, re.Match[str]]] = []
    for index, row in enumerate(comments):
        if not isinstance(row, dict):
            _fail("GitHub review comment has an unexpected shape")
        body = str(row.get("body", ""))
        author = row.get("author")
        if not isinstance(author, dict) or author.get("login") != _current_actor():
            continue
        if _pseudo_v3_match(body) is not None:
            continue
        finding_v3 = _finding_match(body)
        disposition_v3 = _disposition_match(body)
        if finding_v3 is not None:
            findings_v3.append((index, finding_v3))
        if disposition_v3 is not None:
            dispositions_v3.append((index, disposition_v3))
        finding_v1 = FINDING_V1_RE.search(body)
        disposition_v1 = DISPOSITION_V1_RE.search(body)
        if finding_v1 is not None:
            findings_v1.append((index, finding_v1))
        if disposition_v1 is not None:
            dispositions_v1.append((index, disposition_v1))
    return findings_v3, dispositions_v3, findings_v1, dispositions_v1


def _matching_dispositions(
    finding_index: int,
    finding: re.Match[str],
    dispositions: list[tuple[int, re.Match[str]]],
    *,
    expected_head: str | None = None,
) -> list[re.Match[str]]:
    fields = ("engine", "round", "fingerprint")
    matches = [
        disposition
        for index, disposition in dispositions
        if index > finding_index
        and all(disposition.group(field) == finding.group(field) for field in fields)
        and (
            "occurrence" not in finding.groupdict()
            or disposition.group("occurrence") == finding.group("occurrence")
        )
        and (expected_head is None or disposition.group("head") == expected_head)
    ]
    return matches


def _verify_thread_dispositions(
    threads: list[dict[str, Any]],
    historical_comment_ids: set[int] | None = None,
    *,
    repo: str,
) -> int:
    verified = 0
    fingerprint_threads: dict[str, int] = {}
    for thread_index, thread in enumerate(threads):
        findings_v3, dispositions_v3, findings_v1, dispositions_v1 = (
            _thread_protocol_records(thread, historical_comment_ids)
        )
        grouped_v3: dict[str, list[tuple[int, re.Match[str]]]] = {}
        for finding_index, finding in findings_v3:
            fingerprint = finding.group("fingerprint")
            prior_thread = fingerprint_threads.setdefault(fingerprint, thread_index)
            if prior_thread != thread_index:
                _fail("local-review fingerprint has duplicated root threads")
            grouped_v3.setdefault(fingerprint, []).append((finding_index, finding))
        for fingerprint, occurrences in grouped_v3.items():
            numbers = [int(finding.group("occurrence")) for _, finding in occurrences]
            if numbers != list(range(1, len(numbers) + 1)):
                _fail(
                    f"local-review fingerprint {fingerprint} occurrences are not sequential"
                )
            for position, (finding_index, finding) in enumerate(occurrences[:-1]):
                matches = [
                    (index, disposition)
                    for index, disposition in dispositions_v3
                    if index > finding_index
                    and all(
                        disposition.group(field) == finding.group(field)
                        for field in ("engine", "round", "fingerprint", "occurrence")
                    )
                ]
                next_finding_index = occurrences[position + 1][0]
                if len(matches) != 1 or matches[0][0] >= next_finding_index:
                    _fail(
                        f"local-review fingerprint {fingerprint} recurrence is not sequentially disposed"
                    )
            matched_occurrences: list[tuple[re.Match[str], re.Match[str]]] = []
            for finding_index, finding in occurrences:
                occurrence_matches = _matching_dispositions(
                    finding_index, finding, dispositions_v3
                )
                if len(occurrence_matches) != 1:
                    _fail("local-review finding lacks exactly one matching disposition")
                matched_occurrences.append((finding, occurrence_matches[0]))
            latest_finding, latest_disposition = matched_occurrences[-1]
            if (
                latest_finding.group("severity") == "blocking"
                and latest_disposition.group("outcome") == "deferred"
            ):
                _fail("blocking local-review findings cannot be deferred")
            prior_blocking_deferrals = [
                position
                for position, (finding, disposition) in enumerate(
                    matched_occurrences[:-1]
                )
                if finding.group("severity") == "blocking"
                and disposition.group("outcome") == "deferred"
            ]
            if prior_blocking_deferrals:
                if latest_disposition.group("outcome") != "fixed":
                    _fail(
                        "a blocking deferral must be cleared by a later fixed occurrence"
                    )
                start = prior_blocking_deferrals[-1]
                for prior, current in zip(
                    matched_occurrences[start:], matched_occurrences[start + 1 :]
                ):
                    _verify_forward_transition(
                        repo,
                        prior[1].group("head"),
                        current[0].group("head"),
                    )
                _verify_forward_transition(
                    repo,
                    latest_finding.group("head"),
                    latest_disposition.group("head"),
                )
        findings: list[tuple[int, re.Match[str], list[tuple[int, re.Match[str]]]]] = [
            (index, finding, dispositions_v3) for index, finding in findings_v3
        ] + [(index, finding, dispositions_v1) for index, finding in findings_v1]
        if not findings:
            continue
        if thread.get("isResolved") is not True:
            _fail("local-review thread is not resolved")
        for finding_index, finding, dispositions in findings:
            finding_matches = _matching_dispositions(
                finding_index, finding, dispositions
            )
            if len(finding_matches) != 1:
                _fail("local-review finding lacks exactly one matching disposition")
        verified += 1
    return verified


def _verify_forward_transition(repo: str, before: str, after: str) -> None:
    if before == after:
        _fail("superseding fixed occurrence is not a forward transition")
    comparison = _json_output(["api", f"repos/{repo}/compare/{before}...{after}"])
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
        _fail("superseding local-review occurrence is not forward-only")


def _verify_result_evidence(
    args: argparse.Namespace,
    threads: list[dict[str, Any]],
    *,
    data: dict[str, Any] | None = None,
    allowed_heads: dict[str, int] | None = None,
    historical_comment_ids: set[int] | None = None,
) -> dict[str, Any]:
    if data is None:
        data = _validate_result_data(args)
    if data["status"] not in {"clean", "changed"}:
        _fail("ledger result evidence requires a changed review result")
    if allowed_heads is None:
        allowed_heads = _load_allowed_heads(args)
    evidence = _same_round_dispositions(
        args, threads, allowed_heads, historical_comment_ids
    )
    if [row[0] for row in evidence] != sorted(data["findingFingerprints"]):
        _fail("review result fingerprints do not exactly match same-round ledger evidence")
    if data["status"] == "clean":
        if any(has_fix for _, has_fix, _, _ in evidence):
            _fail("clean review results cannot have same-round fixes")
        return data
    if args.round >= 3 and data["classification"] != "material":
        _fail("round 3+ changed review results require material classification")
    if not evidence:
        _fail("changed review results require ledger evidence")
    if not any(has_fix for _, has_fix, _, _ in evidence):
        _fail("changed review results require a fixed ledger finding")
    if args.round >= 3 and any(
        has_nonblocking_fix for _, _, _, has_nonblocking_fix in evidence
    ):
        _fail("convergence review results cannot fix non-blocking findings")
    fixed_major = any(has_major_fix for _, _, has_major_fix, _ in evidence)
    if fixed_major and data["classification"] != "material":
        _fail("fixed blocking or major findings require material classification")
    return data


def _verify_ledger(args: argparse.Namespace) -> None:
    _verify_head(args.repo, args.pr, args.head)
    threads = _load_review_threads(args.threads_file)
    historical_comment_ids = _historical_comment_ids(args)
    thread_count = _verify_thread_dispositions(
        threads, historical_comment_ids, repo=args.repo
    )
    data = None
    if args.result_file is not None:
        data = _verify_result_evidence(
            args, threads, historical_comment_ids=historical_comment_ids
        )
    print(
        json.dumps(
            {
                "resultStatus": None if data is None else data["status"],
                "threadsVerified": thread_count,
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
    parser.add_argument("--protocol-version", action="version", version=str(PROTOCOL_VERSION))
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
    recurrence.add_argument("--severity", required=True, choices=("blocking", "major", "minor", "nit"))
    recurrence.add_argument("--lens", required=True)
    recurrence.add_argument("--comment-id", required=True, type=int)
    recurrence.add_argument("--thread-id", required=True)
    recurrence.add_argument("--content-file", required=True)
    recurrence.set_defaults(handler=_reopen_occurrence)

    dispose = commands.add_parser("dispose")
    _add_common(dispose)
    _add_protocol_identity(dispose)
    dispose.add_argument("--outcome", required=True, choices=("fixed", "dismissed", "deferred"))
    dispose.add_argument("--comment-id", required=True, type=int)
    dispose.add_argument("--thread-id", required=True)
    dispose.add_argument("--content-file", required=True)
    dispose.set_defaults(handler=_dispose)

    reply = commands.add_parser("reply")
    _add_common(reply)
    reply.add_argument("--comment-id", required=True, type=int)
    reply.add_argument("--body-file", required=True, help="Legacy v1 path or - for stdin")
    reply.set_defaults(handler=_reply)

    comment = commands.add_parser("post-pr-comment")
    _add_common(comment)
    comment.add_argument("--body-file", required=True, help="Legacy v1 path or - for stdin")
    comment.set_defaults(handler=_post_pr_comment)

    validate = commands.add_parser("validate-result")
    _add_result_arguments(validate, github=False)
    validate.set_defaults(handler=_validate_result)

    write_result = commands.add_parser("write-result")
    _add_result_arguments(write_result, github=False)
    write_result.add_argument("--repo", required=True)
    write_result.add_argument("--pr", required=True, type=int)
    write_result.add_argument("--threads-file")
    write_result.add_argument("--allowed-heads-file")
    write_result.add_argument("--actor")
    write_result.add_argument("--historical-comment-ids-file")
    write_result.add_argument("--classification", choices=("minor", "material"))
    write_result.set_defaults(handler=_write_result)

    write_blocked = commands.add_parser("write-blocked-result")
    _add_result_arguments(write_blocked, github=False)
    write_blocked.add_argument("--blocker-file", required=True)
    write_blocked.set_defaults(handler=_write_blocked_result)

    attest = commands.add_parser("attest")
    _add_result_arguments(attest, github=True)
    attest.add_argument("--threads-file", required=True)
    attest.add_argument("--allowed-heads-file", required=True)
    attest.add_argument("--actor")
    attest.add_argument("--historical-comment-ids-file")
    attest.add_argument("--expected-result-sha256", required=True)
    attest.add_argument("--content-file")
    attest.set_defaults(handler=_attest)

    resolve = commands.add_parser("resolve")
    _add_common(resolve)
    resolve.add_argument("--thread-id", required=True)
    resolve.set_defaults(handler=_resolve)

    reconcile = commands.add_parser("reconcile")
    _add_common(reconcile)
    reconcile.add_argument("--fingerprint", required=True)
    reconcile.set_defaults(handler=_reconcile)

    verify_ledger = commands.add_parser("verify-ledger")
    _add_common(verify_ledger)
    verify_ledger.add_argument("--threads-file", required=True)
    verify_ledger.add_argument("--actor")
    verify_ledger.add_argument("--historical-comment-ids-file")
    verify_ledger.add_argument("--engine", choices=("codex", "claude"))
    verify_ledger.add_argument("--round", type=int)
    verify_ledger.add_argument("--base")
    verify_ledger.add_argument("--before")
    verify_ledger.add_argument(
        "--result-head",
        help="Historical result afterSha when --head names a later live PR head",
    )
    verify_ledger.add_argument("--result-file")
    verify_ledger.add_argument("--allowed-heads-file")
    verify_ledger.set_defaults(handler=_verify_ledger)
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
            _fail("v3 content mode requires " + ", ".join(f"--{name.replace('_', '-')}" for name in missing))
    if args.command == "verify-ledger":
        result_fields = (
            "engine",
            "round",
            "base",
            "before",
            "result_file",
            "allowed_heads_file",
        )
        present = [getattr(args, name, None) is not None for name in result_fields]
        if any(present) and not all(present):
            _fail(
                "verify-ledger result evidence requires --engine, --round, "
                "--base, --before, --result-file, and --allowed-heads-file"
            )


def main(argv: list[str] | None = None) -> int:
    global CURRENT_ACTOR
    CURRENT_ACTOR = None
    args = _parser().parse_args(argv)
    _validate_args(args)
    if getattr(args, "actor", None) is not None:
        CURRENT_ACTOR = _require_token(args.actor, "actor")
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LedgerError, OSError) as error:
        print(f"review-ledger: {error}", file=sys.stderr)
        raise SystemExit(1) from error
