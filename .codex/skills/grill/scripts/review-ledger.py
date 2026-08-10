#!/usr/bin/env python3
"""Create, reconcile, and disposition deterministic local-review ledger entries."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
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
    nearest = sorted(valid, key=lambda candidate: (abs(candidate - line), candidate))[:5]
    candidates = ", ".join(str(candidate) for candidate in nearest) or "none"
    _fail(
        f"line {line} is not an exact {side} anchor in GitHub's PR patch; "
        f"nearest valid lines: {candidates}"
    )


def _verify_comment(repo: str, comment_id: int, expected_body: str) -> None:
    response = _json_output(["api", f"repos/{repo}/pulls/comments/{comment_id}"])
    if not isinstance(response, dict) or response.get("body") != expected_body:
        _fail(f"could not verify review comment {comment_id} after posting")


def _verify_issue_comment(repo: str, comment_id: int, expected_body: str) -> None:
    response = _json_output(["api", f"repos/{repo}/issues/comments/{comment_id}"])
    if not isinstance(response, dict) or response.get("body") != expected_body:
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


def _finding_records(
    rows: list[dict[str, Any]], fingerprint: str
) -> list[tuple[dict[str, Any], re.Match[str]]]:
    records: list[tuple[dict[str, Any], re.Match[str]]] = []
    for row in rows:
        match = FINDING_V3_RE.search(str(row.get("body", "")))
        if match is not None and match.group("fingerprint") == fingerprint:
            records.append((row, match))
    return records


def _require_finding_occurrence(
    rows: list[dict[str, Any]], args: argparse.Namespace
) -> None:
    matches = [
        match
        for _, match in _finding_records(rows, args.fingerprint)
        if match.group("engine") == args.engine
        and int(match.group("round")) == args.round
        and int(match.group("occurrence")) == args.occurrence
    ]
    if len(matches) != 1:
        _fail("disposition does not identify exactly one existing finding occurrence")


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
    try:
        response = _json_output(["api", "-X", "POST", endpoint], payload)
        comment_id = _posted_comment_id(response)
    except LedgerError:
        recovered = _matching_body(_review_comments(args.repo, args.pr), marker, body)
        if recovered is None:
            raise
        comment_id = recovered
    _verify_comment(args.repo, comment_id, body)
    _verify_head(args.repo, args.pr, args.head)
    return comment_id, False


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
    response = _json_output(
        ["api", "graphql"],
        {"query": mutation, "variables": {"threadId": args.thread_id}},
    )
    try:
        thread = response["data"][field]["thread"]
    except (KeyError, TypeError) as error:
        raise LedgerError("GitHub returned an invalid thread mutation response") from error
    if thread.get("id") != args.thread_id or thread.get("isResolved") is not resolved:
        _fail(f"GitHub did not set review thread {args.thread_id} resolved={resolved}")
    if _thread_state(args) is not resolved:
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
    records = _finding_records(rows, args.fingerprint)
    existing = _matching_body(rows, marker, body)
    if args.occurrence < 2:
        _fail("reopen-occurrence requires occurrence 2 or later")
    if existing is None:
        occurrences = sorted(int(match.group("occurrence")) for _, match in records)
        if occurrences != list(range(1, args.occurrence)):
            _fail("finding occurrences are missing, duplicated, or out of sequence")
        roots = [
            row
            for row, match in records
            if int(match.group("occurrence")) == 1
        ]
        if len(roots) != 1 or roots[0].get("id") != args.comment_id:
            _fail("--comment-id does not identify the fingerprint root comment")
    comment_id, replayed = _post_review_comment(
        args, marker, body, reply_to=args.comment_id
    )
    thread_replayed = _set_thread_state(args, False)
    _verify_head(args.repo, args.pr, args.head)
    print(json.dumps({"comment_id": comment_id, "replayed": replayed, "thread_replayed": thread_replayed, "resolved": False, "verified": True}))


def _dispose(args: argparse.Namespace) -> None:
    marker, body = _disposition_body(args)
    _verify_head(args.repo, args.pr, args.head)
    _require_finding_occurrence(
        _review_comments(args.repo, args.pr), args
    )
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


def _validate_result_data(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.result_file)
    if path.is_symlink() or not path.is_file():
        _fail("review result must be a regular non-symlink file")
    try:
        data = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LedgerError("review result must contain valid UTF-8 JSON") from error
    if not isinstance(data, dict):
        _fail("review result must be a JSON object")
    required = {"version", "status", "engine", "round", "baseSha", "beforeSha", "afterSha", "classification", "findingFingerprints", "finalLaneComplete"}
    allowed = required | {"blocker"}
    if set(data) != required and set(data) != allowed:
        _fail("review result has missing or unknown fields")
    expected = {
        "version": PROTOCOL_VERSION,
        "engine": args.engine,
        "round": args.round,
        "baseSha": args.base,
        "beforeSha": args.before,
        "afterSha": args.head,
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
        if args.before != args.head or classification is not None or fingerprints or data["finalLaneComplete"] is not True or "blocker" in data:
            _fail("clean review result conflicts with the observed pass")
    elif status == "changed":
        if args.before == args.head or classification not in {"minor", "material"} or not fingerprints or data["finalLaneComplete"] is not True or "blocker" in data:
            _fail("changed review result conflicts with the observed pass")
    else:
        blocker = data.get("blocker")
        if classification is not None or data["finalLaneComplete"] is not False or not isinstance(blocker, str) or not blocker.strip() or "<!-- local-review" in blocker:
            _fail("blocked review result lacks a safe blocker")
    return cast(dict[str, Any], data)


def _validate_result(args: argparse.Namespace) -> None:
    data = _validate_result_data(args)
    output = dict(data)
    output["resultSha256"] = hashlib.sha256(Path(args.result_file).read_bytes()).hexdigest()
    output["verified"] = True
    print(json.dumps(output, sort_keys=True))


def _attest(args: argparse.Namespace) -> None:
    data = _validate_result_data(args)
    result_hash = hashlib.sha256(Path(args.result_file).read_bytes()).hexdigest()
    if data["status"] == "blocked":
        _fail("blocked review results cannot be attested as complete")
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
    mutation = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()
    response = _json_output(
        ["api", "graphql"],
        {"query": mutation, "variables": {"threadId": args.thread_id}},
    )
    try:
        thread = response["data"]["resolveReviewThread"]["thread"]
    except (KeyError, TypeError) as error:
        raise LedgerError("GitHub returned an invalid thread-resolution response") from error
    if thread.get("id") != args.thread_id or thread.get("isResolved") is not True:
        _fail(f"GitHub did not resolve review thread {args.thread_id}")
    if not _thread_state(args):
        _fail(f"could not verify review thread {args.thread_id} as resolved")
    _verify_head(args.repo, args.pr, args.head)
    print(json.dumps({"thread_id": args.thread_id, "resolved": True}))


def _reconcile(args: argparse.Namespace) -> None:
    _verify_head(args.repo, args.pr, args.head)
    comments = _review_comments(args.repo, args.pr)
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

    attest = commands.add_parser("attest")
    _add_result_arguments(attest, github=True)
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
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    for name in ("head", "base", "before"):
        value = getattr(args, name, None)
        if value is not None and not SHA_RE.fullmatch(value):
            _fail(f"--{name} must be a full 40-character lowercase commit SHA")
    round_number = getattr(args, "round", None)
    if round_number is not None and round_number < 1:
        _fail("--round must be a positive integer")
    if getattr(args, "occurrence", 1) < 1:
        _fail("--occurrence must be a positive integer")
    if getattr(args, "content_file", None):
        required = ("engine", "round", "fingerprint")
        if args.command == "post-finding":
            required += ("severity", "lens")
        missing = [name for name in required if getattr(args, name, None) is None]
        if missing:
            _fail("v3 content mode requires " + ", ".join(f"--{name.replace('_', '-')}" for name in missing))


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
