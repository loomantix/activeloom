#!/usr/bin/env python3
"""One Codex relay pass, with the same exact-head preflight as other launchers.

Run only in a dedicated, trusted review worktree. Like the existing unattended
agent-loop launcher, the worker needs commit, push and PR-comment permissions.
Model and provider selection remain the user's configured choices.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def launch_state(phase: str, reason: str | None = None) -> None:
    spec = importlib.util.spec_from_file_location(
        "launch_state", Path(__file__).with_name("review-launch-state.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if phase == "verify":
        module.verify_installation()
    else:
        module.record(phase, reason)


def output(args: list[str]) -> str:
    return subprocess.check_output(args, text=True, timeout=120).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--round", required=True, type=int)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo)
        or args.pr < 1
        or args.round < 1
        or any(not re.fullmatch(r"[0-9a-f]{40}", v) for v in (args.head, args.base))
    ):
        parser.error("invalid repository, PR, SHA or round")
    launch_state("preflight", "missing_tool")
    cli = shutil.which("codex")
    timeout = shutil.which("timeout")
    if (
        not cli
        or not timeout
        or any(not shutil.which(name) for name in ("node", "git", "gh"))
    ):
        raise ValueError("codex, timeout, node, git and gh are required")
    launch_state("preflight", "pr_boundary")
    pr = json.loads(
        output(
            [
                "gh",
                "pr",
                "view",
                str(args.pr),
                "--repo",
                args.repo,
                "--json",
                "headRefOid,headRefName,headRepository,author",
            ]
        )
    )
    if (
        output(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
        )
        != args.repo
        or pr["headRepository"]["nameWithOwner"] != args.repo
        or pr["author"]["login"] != output(["gh", "api", "user", "--jq", ".login"])
        or output(["git", "rev-parse", "HEAD"]) != args.head
        or pr["headRefOid"] != args.head
        or output(
            [
                "git",
                "ls-remote",
                "--exit-code",
                "origin",
                "refs/heads/" + pr["headRefName"],
            ]
        ).split()[0]
        != args.head
        or output(["git", "status", "--porcelain"])
    ):
        raise ValueError("requires a clean self-authored same-repository exact PR head")
    if not args.preflight_only:
        launch_state("preflight", "authorization")
        subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("local-review-handoff.py")),
                "authorize-pass",
                "--repo",
                args.repo,
                "--pr",
                str(args.pr),
                "--base",
                args.base,
                "--head",
                args.head,
                "--engine",
                "codex",
                "--round",
                str(args.round),
                *(
                    ["--run-id", os.environ["ACTIVELOOM_RUN_ID"]]
                    if os.environ.get("ACTIVELOOM_RUN_ID")
                    else []
                ),
            ],
            check=True,
        )
    launch_state("preflight", "installation_integrity")
    launch_state("verify")
    surface = os.environ.get("ACTIVELOOM_REVIEW_SURFACE", ".codex")
    if (
        os.environ.get("ACTIVELOOM_REVIEW_SURFACE")
        and not (Path(surface) / "skills/deepcritique/SKILL.md").is_file()
    ):
        raise ValueError("review skill is missing")
    launch_state("ready")
    if args.preflight_only:
        return 0
    prompt = (
        f"Use {surface}/skills/deepcritique/SKILL.md for one Codex review pass on PR #{args.pr} "
        f"Use absolute paths under {surface} for its skills, references and helpers. "
        f"in {args.repo}, round {args.round}, pinned base {args.base}, exact head {args.head}. "
        "Resolve the recorded tier; use critique for Lean. Read prior non-telemetry ledger evidence. "
        "Post verified findings inline before edits; validate, commit with repository-required sign-off, "
        "push normally, reply and resolve. When AGENT_LOOP_REVIEW_RESULT_FILE is set, write the "
        "canonical result there and return without attesting; the runner owns attestation. "
        "Otherwise follow standalone attestation instructions. Do not launch another engine, "
        "restart or finish the run, mark ready, merge, or force-push. Return a concise pass summary."
    )
    os.environ.update(
        AGENT_LOOP_REVIEW_ENGINE="codex",
        AGENT_LOOP_REVIEW_BASE_SHA=args.base,
        AGENT_LOOP_REVIEW_ROUND=str(args.round),
    )
    launch_state("execution")
    os.execv(
        timeout,
        [
            timeout,
            "--signal=TERM",
            "--kill-after=30s",
            "2700s",
            cli,
            "exec",
            "--ephemeral",
            "--sandbox",
            "danger-full-access",
            "-c",
            'approval_policy="never"',
            prompt,
        ],
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"codex review preflight failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error
