#!/usr/bin/env python3
"""Post and disposition local-review ledger entries without ad hoc API payloads."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn, cast


HUNK_WITH_LEFT_RE = re.compile(
    r"^@@ -(?P<left>\d+)(?:,\d+)? \+(?P<right>\d+)(?:,\d+)? @@"
)
FINDING_MARKER = "<!-- local-review:v1 "
DISPOSITION_MARKER = "<!-- local-review-disposition:v1 "
PR_MARKERS = (
    "<!-- local-review-refactor:v1 ",
    "<!-- local-review-pass:v1 ",
    "<!-- local-review-complete:v1 ",
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


def _read_body(path: str, marker: str | tuple[str, ...]) -> str:
    body = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    if not body.strip():
        _fail("comment body is empty")
    markers = (marker,) if isinstance(marker, str) else marker
    if not any(candidate in body for candidate in markers):
        _fail("comment body lacks the required local-review marker")
    return body.rstrip()


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


def _pr_files(repo: str, pr: int) -> dict[str, str | None]:
    pages = _json_output(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/pulls/{pr}/files?per_page=100",
        ]
    )
    if not isinstance(pages, list):
        _fail("GitHub PR-files response has an unexpected shape")
    files: dict[str, str | None] = {}
    for page in pages:
        if not isinstance(page, list):
            _fail("GitHub PR-files page has an unexpected shape")
        for item in page:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                _fail("GitHub PR-files item has an unexpected shape")
            patch = item.get("patch")
            files[cast(str, item["filename"])] = patch if isinstance(patch, str) else None
    return files


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
    response = _json_output(
        ["api", f"repos/{repo}/pulls/comments/{comment_id}"]
    )
    if not isinstance(response, dict) or response.get("body") != expected_body:
        _fail(f"could not verify review comment {comment_id} after posting")


def _verify_issue_comment(repo: str, comment_id: int, expected_body: str) -> None:
    response = _json_output(
        ["api", f"repos/{repo}/issues/comments/{comment_id}"]
    )
    if not isinstance(response, dict) or response.get("body") != expected_body:
        _fail(f"could not verify PR comment {comment_id} after posting")


def _posted_comment_id(response: Any) -> int:
    if not isinstance(response, dict) or not isinstance(response.get("id"), int):
        _fail("GitHub accepted the mutation but returned no comment ID")
    return cast(int, response["id"])


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
    body = _read_body(args.body_file, FINDING_MARKER)
    _verify_head(args.repo, args.pr, args.head)
    files = _pr_files(args.repo, args.pr)
    line = None if args.file_level else args.line
    side = None if args.file_level else args.side
    _validate_anchor(files, args.path, line, side)
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
    print(json.dumps({"comment_id": comment_id, "verified": True}))


def _reply(args: argparse.Namespace) -> None:
    body = _read_body(args.body_file, DISPOSITION_MARKER)
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
    print(json.dumps({"comment_id": comment_id, "verified": True}))


def _post_pr_comment(args: argparse.Namespace) -> None:
    body = _read_body(args.body_file, PR_MARKERS)
    _verify_head(args.repo, args.pr, args.head)
    response = _json_output(
        ["api", "-X", "POST", f"repos/{args.repo}/issues/{args.pr}/comments"],
        {"body": body},
    )
    comment_id = _posted_comment_id(response)
    _verify_issue_comment(args.repo, comment_id, body)
    print(json.dumps({"comment_id": comment_id, "verified": True}))


def _resolve(args: argparse.Namespace) -> None:
    _verify_head(args.repo, args.pr, args.head)
    mutation = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()
    mutation_response = _json_output(
        ["api", "graphql"],
        {"query": mutation, "variables": {"threadId": args.thread_id}},
    )
    try:
        mutation_thread = mutation_response["data"]["resolveReviewThread"]["thread"]
    except (KeyError, TypeError) as error:
        raise LedgerError("GitHub returned an invalid thread-resolution response") from error
    if (
        mutation_thread.get("id") != args.thread_id
        or mutation_thread.get("isResolved") is not True
    ):
        _fail(f"GitHub did not resolve review thread {args.thread_id}")
    query = """
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread { id isResolved }
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
    if thread.get("id") != args.thread_id or thread.get("isResolved") is not True:
        _fail(f"could not verify review thread {args.thread_id} as resolved")
    print(json.dumps({"thread_id": args.thread_id, "resolved": True}))


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="GitHub OWNER/REPO")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True, help="Exact 40-character PR head SHA")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
    finding.add_argument("--body-file", required=True, help="Path or - for stdin")
    finding.set_defaults(handler=_post_finding)

    reply = commands.add_parser("reply")
    _add_common(reply)
    reply.add_argument("--comment-id", required=True, type=int)
    reply.add_argument("--body-file", required=True, help="Path or - for stdin")
    reply.set_defaults(handler=_reply)

    comment = commands.add_parser("post-pr-comment")
    _add_common(comment)
    comment.add_argument("--body-file", required=True, help="Path or - for stdin")
    comment.set_defaults(handler=_post_pr_comment)

    resolve = commands.add_parser("resolve")
    _add_common(resolve)
    resolve.add_argument("--thread-id", required=True)
    resolve.set_defaults(handler=_resolve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.head):
        _fail("--head must be a full 40-character lowercase commit SHA")
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LedgerError, OSError) as error:
        print(f"review-ledger: {error}", file=sys.stderr)
        raise SystemExit(1) from error
