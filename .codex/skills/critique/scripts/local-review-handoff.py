#!/usr/bin/env python3
"""Post and verify deterministic cross-engine local-review handoffs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn, cast


CURRENT_ACTOR: str | None = None
# Engine identities this handoff surface accepts. Must stay in step with the
# marker grammar below and with the engines the review-ledger helper accepts.
ENGINES = ("codex", "claude", "gemini")
ENGINE_LABELS = {"codex": "Codex", "claude": "Claude", "gemini": "Gemini"}
HANDOFF_V1_RE = re.compile(
    r"^<!-- local-review-handoff:v1 "
    r"from=(?P<from_engine>codex|claude|gemini) "
    r"to=(?P<to_engine>codex|claude|gemini) "
    r"round=(?P<round>[1-9][0-9]*) "
    r"base=(?P<base>[0-9a-f]{40}) "
    r"head=(?P<head>[0-9a-f]{40}) "
    r"outcome=(?P<outcome>clean|minor|material|blocked) "
    r"content-sha256=(?P<content_sha>[0-9a-f]{64}) -->$",
    re.MULTILINE,
)


class HandoffError(RuntimeError):
    """A fail-closed handoff validation or mutation error."""


def _fail(message: str) -> NoReturn:
    raise HandoffError(message)


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
    try:
        return json.loads(_run_gh(args, payload))
    except json.JSONDecodeError as error:
        raise HandoffError("GitHub returned invalid JSON") from error


def _current_actor() -> str:
    global CURRENT_ACTOR
    if CURRENT_ACTOR is None:
        actor = _run_gh(["api", "user", "--jq", ".login"]).strip()
        if not actor:
            _fail("could not resolve the authenticated GitHub actor")
        CURRENT_ACTOR = actor
    return CURRENT_ACTOR


def _flatten_pages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(page, list) for page in value):
        _fail("GitHub returned malformed PR-comment pagination")
    rows: list[dict[str, Any]] = []
    for page in value:
        if any(not isinstance(row, dict) for row in page):
            _fail("GitHub returned malformed PR-comment rows")
        rows.extend(cast(list[dict[str, Any]], page))
    return rows


def _issue_comments(repo: str, pr: int) -> list[dict[str, Any]]:
    rows = _flatten_pages(
        _json_output(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/issues/{pr}/comments?per_page=100",
            ]
        )
    )
    actor = _current_actor()
    return [
        row
        for row in rows
        if isinstance(row.get("user"), dict) and row["user"].get("login") == actor
    ]


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


def _read_context(path_value: str | None) -> str:
    if path_value is None:
        return ""
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        _fail("handoff context must be a regular non-symlink file")
    try:
        context = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise HandoffError("handoff context must be valid UTF-8") from error
    if "\x00" in context:
        _fail("handoff context contains NUL")
    if "<!-- local-review" in context:
        _fail("handoff context must not contain a local-review marker")
    return context


def _handoff_content(args: argparse.Namespace, context: str) -> str:
    next_engine = cast(str, args.to_engine)
    label = ENGINE_LABELS[next_engine]
    context = context.strip() or "No additional context. Reconstruct the pass from the PR ledger."
    return f"""## Local review handoff: {args.from_engine} to {next_engine}

Start a fresh {label} terminal session in an isolated worktree, then give it this prompt:

```text
Continue review on PR #{args.pr}.

Find and follow the latest authenticated local-review-handoff:v1 comment before
reviewing. Verify that its exact head is still current, load the complete PR
ledger including resolved threads and prior attestations, and continue as the
{next_engine} reviewer against the pinned base. Do not invoke the other review
engine from this session. When this pass ends, inspect every declared
engine's authenticated outcome for the round. If they satisfy the repository's convergence
rule, publish the terminal review result and follow its configured finalization
step without another handoff. Otherwise publish the next authenticated handoff
comment and stop so the user can start the following session.
```

Pinned review state:

- repository: `{args.repo}`
- PR: `#{args.pr}`
- base: `{args.base}`
- head: `{args.head}`
- completed pass: `{args.from_engine}` round `{args.round}` (`{args.outcome}`)
- next reviewer: `{next_engine}`

Pass context: {context}
"""


def _handoff_digest(
    *,
    from_engine: str,
    to_engine: str,
    round_number: int,
    base: str,
    head: str,
    outcome: str,
    content: str,
) -> str:
    payload = {
        "base": base,
        "content": content,
        "from_engine": from_engine,
        "head": head,
        "outcome": outcome,
        "round": round_number,
        "to_engine": to_engine,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_issue_comment(repo: str, comment_id: int, expected_body: str) -> None:
    response = _json_output(["api", f"repos/{repo}/issues/comments/{comment_id}"])
    if (
        not isinstance(response, dict)
        or response.get("body") != expected_body
        or not isinstance(response.get("user"), dict)
        or response["user"].get("login") != _current_actor()
    ):
        _fail(f"could not verify PR comment {comment_id} after posting")


def _matching_body(rows: list[dict[str, Any]], marker: str, body: str) -> int | None:
    matches = [row for row in rows if marker in str(row.get("body", ""))]
    if not matches:
        return None
    if len(matches) != 1:
        _fail("handoff idempotency key is duplicated")
    row = matches[0]
    if row.get("body") != body or not isinstance(row.get("id"), int):
        _fail("handoff idempotency key already exists with conflicting content")
    return cast(int, row["id"])


def _post_handoff(args: argparse.Namespace) -> None:
    if args.from_engine == args.to_engine:
        _fail("review handoff engines must be different")
    content = _handoff_content(args, _read_context(args.context_file))
    digest = _handoff_digest(
        from_engine=args.from_engine,
        to_engine=args.to_engine,
        round_number=args.round,
        base=args.base,
        head=args.head,
        outcome=args.outcome,
        content=content,
    )
    marker = (
        f"<!-- local-review-handoff:v1 from={args.from_engine} "
        f"to={args.to_engine} round={args.round} base={args.base} "
        f"head={args.head} outcome={args.outcome} content-sha256={digest} -->"
    )
    body = f"{marker}\n{content}"
    _verify_head(args.repo, args.pr, args.head)
    comment_id = _matching_body(_issue_comments(args.repo, args.pr), marker, body)
    replayed = comment_id is not None
    if comment_id is None:
        try:
            response = _json_output(
                ["api", "-X", "POST", f"repos/{args.repo}/issues/{args.pr}/comments"],
                {"body": body},
            )
            if not isinstance(response, dict) or not isinstance(response.get("id"), int):
                _fail("GitHub accepted the mutation but returned no comment ID")
            comment_id = cast(int, response["id"])
        except HandoffError:
            comment_id = _matching_body(_issue_comments(args.repo, args.pr), marker, body)
            if comment_id is None:
                raise
            replayed = True
    _verify_issue_comment(args.repo, comment_id, body)
    verified_id = _matching_body(_issue_comments(args.repo, args.pr), marker, body)
    if verified_id != comment_id:
        _fail("handoff idempotency key did not resolve to the posted comment")
    _verify_head(args.repo, args.pr, args.head)
    print(
        json.dumps(
            {
                "comment_id": comment_id,
                "from_engine": args.from_engine,
                "head": args.head,
                "replayed": replayed,
                "to_engine": args.to_engine,
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _show_handoff(args: argparse.Namespace) -> None:
    candidates: list[tuple[int, str]] = []
    for row in _issue_comments(args.repo, args.pr):
        body = row.get("body")
        comment_id = row.get("id")
        if not isinstance(body, str) or not isinstance(comment_id, int):
            continue
        if body.startswith("<!-- local-review-handoff:v1"):
            candidates.append((comment_id, body))
    if not candidates:
        _fail("no authenticated local-review handoff comment was found")
    comment_id, body = max(candidates, key=lambda candidate: candidate[0])
    matches = list(HANDOFF_V1_RE.finditer(body))
    if len(matches) != 1:
        _fail("latest local-review handoff marker is malformed")
    marker = matches[0]
    if marker.start() != 0 or not body[marker.end() :].startswith("\n"):
        _fail("a local-review handoff marker must start the PR comment")
    content = body[marker.end() + 1 :]
    digest = _handoff_digest(
        from_engine=marker.group("from_engine"),
        to_engine=marker.group("to_engine"),
        round_number=int(marker.group("round")),
        base=marker.group("base"),
        head=marker.group("head"),
        outcome=marker.group("outcome"),
        content=content,
    )
    if digest != marker.group("content_sha"):
        _fail("latest local-review handoff content digest is invalid")
    if marker.group("to_engine") != args.engine:
        _fail(f"latest local-review handoff targets {marker.group('to_engine')}, not {args.engine}")
    _verify_head(args.repo, args.pr, marker.group("head"))
    print(
        json.dumps(
            {
                "base": marker.group("base"),
                "body": body,
                "comment_id": comment_id,
                "from_engine": marker.group("from_engine"),
                "head": marker.group("head"),
                "outcome": marker.group("outcome"),
                "round": int(marker.group("round")),
                "to_engine": marker.group("to_engine"),
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError("must be a full lowercase 40-character commit SHA")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)
    post = commands.add_parser("post-handoff")
    post.add_argument("--repo", required=True)
    post.add_argument("--pr", required=True, type=int)
    post.add_argument("--head", required=True, type=_sha)
    post.add_argument("--base", required=True, type=_sha)
    post.add_argument("--from-engine", required=True, choices=ENGINES)
    post.add_argument("--to-engine", required=True, choices=ENGINES)
    post.add_argument("--round", required=True, type=int)
    post.add_argument("--outcome", required=True, choices=("clean", "minor", "material", "blocked"))
    post.add_argument("--context-file")
    post.set_defaults(handler=_post_handoff)

    show = commands.add_parser("show-handoff")
    show.add_argument("--repo", required=True)
    show.add_argument("--pr", required=True, type=int)
    show.add_argument("--engine", required=True, choices=ENGINES)
    show.set_defaults(handler=_show_handoff)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "round", 1) < 1:
        _fail("round must be positive")
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HandoffError as error:
        print(f"local-review-handoff: {error}", file=sys.stderr)
        raise SystemExit(1) from error
