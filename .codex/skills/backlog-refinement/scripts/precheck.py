#!/usr/bin/env python3
"""Gather the checkable facts about issues before an assessor reads them.

Most stale issues are caught by mechanical checks, not judgement: a merged PR
that names the issue but never closed it, child issues that are all closed, a
cited file that no longer exists, a listed blocker that has since closed. This
script runs those checks deterministically and emits JSON, so every assessor
starts from the same facts instead of re-deriving them, and the facts cost one
script run rather than agent tokens.

It never mutates anything. The hints are leads to verify, not verdicts: a
merged PR that mentions an issue may have shipped all of it, part of it, or
none of it.

    precheck.py 22 31 33                # specific issues
    precheck.py --unrefined --limit 25  # the next un-refined batch
    precheck.py 22 --out facts.json     # write to a file
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rubric import RubricConfig, load_config, repo_root, title_priority  # noqa: E402

# `#123`, but not `owner/repo#123` (another repository) or `#123abc`.
_ISSUE_REF = re.compile(r"(?<![\w/])#(\d+)\b")
# A backticked repository path with an extension, optionally `:line` or
# `:start-end`. Requiring a `/` keeps bare words and commands out.
_ANCHOR = re.compile(r"`([\w.@-]+(?:/[\w.@-]+)+\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?`")
_BLOCKED_BY = re.compile(r"(?i)blocked by[^\n]*")
GRAPHQL_CHUNK = 50
TIMEOUT = 60


def fail(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
    sys.exit(1)


def run(cmd: list[str], *, check: bool = True) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        fail(f"Timed out after {TIMEOUT}s: {' '.join(cmd[:3])}")
    except OSError as exc:
        fail(f"Could not run {cmd[0]}: {exc}")
    if check and result.returncode != 0:
        fail(result.stderr or f"{' '.join(cmd[:3])} exited {result.returncode}")
    return result.stdout if result.returncode == 0 else ""


def gh_json(args: list[str]) -> Any:
    out = run(["gh", *args])
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON from gh {' '.join(args[:2])}: {exc}")


# --- pure extraction -------------------------------------------------------


def extract_refs(text: str, self_number: int) -> list[int]:
    """Same-repository issue/PR numbers mentioned in ``text``, excluding itself."""
    return sorted({int(n) for n in _ISSUE_REF.findall(text)} - {self_number})


def extract_blockers(text: str, self_number: int) -> list[int]:
    """Numbers named on a "blocked by" line."""
    found: set[int] = set()
    for line in _BLOCKED_BY.findall(text):
        found.update(extract_refs(line, self_number))
    return sorted(found)


def extract_anchors(text: str) -> list[dict[str, Any]]:
    """Cited repository paths, with the highest line number cited for each."""
    anchors: dict[str, int | None] = {}
    for path, start, end in _ANCHOR.findall(text):
        line = int(end or start) if (end or start) else None
        current = anchors.get(path)
        if path not in anchors or (line is not None and (current is None or line > current)):
            anchors[path] = line
    return [{"path": p, "line": ln} for p, ln in sorted(anchors.items())]


def unclosed_pr_mentions(prs: list[dict[str, Any]], number: int) -> list[dict[str, Any]]:
    """Merged PRs that name ``#number`` without closing it."""
    pattern = re.compile(rf"(?<![\w/])#{number}\b")
    hits = []
    for pr in prs:
        text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        closes = {ref.get("number") for ref in pr.get("closingIssuesReferences") or []}
        if pattern.search(text) and number not in closes:
            hits.append({
                "number": pr["number"],
                "title": pr.get("title"),
                "merged_at": pr.get("mergedAt"),
            })
    return hits


def hints_for(facts: dict[str, Any]) -> list[str]:
    """Plain-language leads an assessor should verify first."""
    hints = []
    subs = facts["sub_issues"]
    if subs["total"] and not subs["open"]:
        hints.append(
            f"All {subs['total']} sub-issues are closed: verify the parent's own "
            "acceptance criteria against HEAD before calling it an epic (likely stale)."
        )
    elif subs["open"]:
        hints.append(f"{len(subs['open'])} of {subs['total']} sub-issues are open: a decomposed epic.")
    for pr in facts["unclosed_pr_mentions"]:
        hints.append(
            f"Merged PR #{pr['number']} names this issue without closing it: "
            "check whether it shipped the work (side-item closure)."
        )
    closed_blockers = [
        n for n in facts["blockers"] if facts["references"].get(str(n), {}).get("state") in ("CLOSED", "MERGED")
    ]
    if closed_blockers and len(closed_blockers) == len(facts["blockers"]):
        hints.append(
            "Every listed blocker is closed or merged ("
            + ", ".join(f"#{n}" for n in closed_blockers)
            + "): the dependency list may have drifted."
        )
    for anchor in facts["anchors"]:
        if not anchor.get("exists"):
            hints.append(f"Cited path `{anchor['path']}` does not exist on the integration branch.")
        elif anchor.get("candidates"):
            hints.append(
                f"Cited path `{anchor['path']}` is ambiguous: it matches "
                f"{len(anchor['candidates'])} files."
            )
        elif anchor.get("line") and anchor.get("line_count") is not None and anchor["line"] > anchor["line_count"]:
            hints.append(
                f"`{anchor.get('resolved') or anchor['path']}:{anchor['line']}` is past the file's end "
                f"({anchor['line_count']} lines): the anchor has moved."
            )
    if facts["assignees"]:
        hints.append(
            "Assigned to " + ", ".join(f"@{a}" for a in facts["assignees"])
            + ": human-reserved; record the category it would get if unassigned."
        )
    if facts["title_priority"]:
        hints.append(f"Title prefix records {facts['title_priority']}.")
    return hints


# --- I/O ------------------------------------------------------------------


def resolve_base(explicit: str | None, configured: str | None) -> str:
    if explicit:
        return explicit
    if configured:
        return configured
    head = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], check=False).strip()
    return head.removeprefix("origin/") or "main"


def repo_slug() -> tuple[str, str]:
    data = gh_json(["repo", "view", "--json", "owner,name"])
    return data["owner"]["login"], data["name"]


def reference_states(owner: str, name: str, numbers: list[int]) -> dict[str, dict[str, Any]]:
    """State of each referenced issue or PR, fetched in batched GraphQL queries."""
    states: dict[str, dict[str, Any]] = {}
    for start in range(0, len(numbers), GRAPHQL_CHUNK):
        chunk = numbers[start:start + GRAPHQL_CHUNK]
        fields = " ".join(
            f"n{n}: issueOrPullRequest(number: {n}) {{ __typename "
            "... on Issue { state title } ... on PullRequest { state title } }"
            for n in chunk
        )
        query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
        out = run(["gh", "api", "graphql", "-f", f"query={query}"], check=False)
        try:
            repo = (json.loads(out or "{}").get("data") or {}).get("repository") or {}
        except json.JSONDecodeError:
            repo = {}
        for n in chunk:
            node = repo.get(f"n{n}")
            if node:
                states[str(n)] = {"type": node["__typename"], "state": node["state"], "title": node["title"]}
    return states


def sub_issue_states(number: int) -> dict[str, Any]:
    items = gh_json(["api", f"repos/{{owner}}/{{repo}}/issues/{number}/sub_issues", "--paginate"])
    open_ = sorted(i["number"] for i in items if i.get("state") == "open")
    closed = sorted(i["number"] for i in items if i.get("state") != "open")
    return {"total": len(items), "open": open_, "closed": closed}


def merged_prs_mentioning(number: int) -> list[dict[str, Any]]:
    prs = gh_json([
        "pr", "list", "--state", "merged", "--search", str(number), "--limit", "30",
        "--json", "number,title,body,mergedAt,closingIssuesReferences",
    ])
    return unclosed_pr_mentions(prs, number)


def resolve_path(path: str, tree: list[str]) -> list[str]:
    """Repository files a cited path could mean: itself, or files it is a suffix of.

    Issues often cite paths relative to a package (`auth/guard.ts` for
    `apps/api/src/auth/guard.ts`); a bare existence check would call those missing.
    """
    if path in tree:
        return [path]
    return [f for f in tree if f.endswith("/" + path)]


def check_anchors(anchors: list[dict[str, Any]], base: str, tree: list[str]) -> list[dict[str, Any]]:
    checked = []
    for anchor in anchors:
        matches = resolve_path(anchor["path"], tree)
        resolved = matches[0] if len(matches) == 1 else None
        line_count = None
        if resolved and anchor["line"]:
            line_count = run(["git", "show", f"origin/{base}:{resolved}"], check=False).count("\n")
        checked.append({
            **anchor,
            "exists": bool(matches),
            "resolved": resolved,
            "candidates": matches if len(matches) > 1 else [],
            "line_count": line_count,
        })
    return checked


def unrefined_numbers(limit: int) -> list[int]:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candidates.py")
    out = run([sys.executable, script, "--json", "--limit", str(limit)])
    return [issue["number"] for issue in json.loads(out)["unrefined"]]


def precheck(
    number: int, base: str, owner: str, name: str, config: RubricConfig, tree: list[str]
) -> dict[str, Any]:
    issue = gh_json([
        "issue", "view", str(number),
        "--json", "number,title,state,body,labels,assignees,comments,url",
    ])
    text = "\n".join([issue.get("body") or ""] + [c.get("body") or "" for c in issue.get("comments") or []])
    refs = extract_refs(text, number)
    facts: dict[str, Any] = {
        "number": number,
        "title": issue["title"],
        "state": issue["state"],
        "url": issue["url"],
        "labels": [label["name"] for label in issue.get("labels") or []],
        "assignees": [a["login"] for a in issue.get("assignees") or []],
        "title_priority": title_priority(issue["title"], config),
        "sub_issues": sub_issue_states(number),
        "blockers": extract_blockers(text, number),
        "references": reference_states(owner, name, refs),
        "unclosed_pr_mentions": merged_prs_mentioning(number),
        "anchors": check_anchors(extract_anchors(text), base, tree),
    }
    facts["hints"] = hints_for(facts)
    return facts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("numbers", nargs="*", type=int, help="issue numbers to check")
    parser.add_argument("--unrefined", action="store_true", help="check the next un-refined issues")
    parser.add_argument("--limit", type=int, default=25, help="how many un-refined issues (default 25)")
    parser.add_argument("--base", help="integration branch (default: the local rubric's, then origin/HEAD)")
    parser.add_argument("--out", help="write JSON here instead of stdout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    numbers = list(args.numbers)
    if args.unrefined:
        numbers += unrefined_numbers(args.limit)
    if not numbers:
        fail("Name issue numbers or pass --unrefined.")
    config = load_config(repo_root())
    base = resolve_base(args.base, config.integration_branch)
    run(["git", "fetch", "--quiet", "origin", base])
    head = run(["git", "rev-parse", f"origin/{base}"]).strip()
    owner, name = repo_slug()
    tree = run(["git", "ls-tree", "-r", "--name-only", f"origin/{base}"]).splitlines()
    report = {
        "base": base,
        "head": head,
        "issues": [precheck(n, base, owner, name, config, tree) for n in dict.fromkeys(numbers)],
    }
    payload = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
