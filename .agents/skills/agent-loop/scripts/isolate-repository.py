#!/usr/bin/env python3
"""Start a loop from private Git metadata while retaining its recovery checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


class IsolationError(RuntimeError):
    """A bootstrap invariant failed."""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument(
        "--harness", choices=(".codex", ".claude", ".agents"), required=True
    )
    parser.add_argument("child_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    source = Path(args.project_dir).resolve(strict=True)
    destination = Path(os.path.abspath(args.destination))
    git = os.environ.get("AGENT_LOOP_REAL_GIT") or shutil.which("git")
    if not git or not Path(git).is_absolute():
        raise IsolationError("trusted Git executable is unavailable")
    env = os.environ.copy()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"

    def run(directory: Path, *arguments: str) -> bytes:
        result = subprocess.run(
            [
                git,
                "--no-replace-objects",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(directory),
                *arguments,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            # Git transport/config errors can contain credentials or URLs.
            raise IsolationError("Git operation failed during isolated bootstrap")
        return result.stdout

    base = (
        run(
            source,
            "rev-parse",
            "--verify",
            "--end-of-options",
            args.base_ref + "^{commit}",
        )
        .decode()
        .strip()
    )
    base_ref = (
        run(
            source,
            "rev-parse",
            "--symbolic-full-name",
            "--verify",
            "--end-of-options",
            args.base_ref,
        )
        .decode()
        .strip()
    )
    if not base_ref.startswith("refs/remotes/origin/"):
        raise IsolationError("isolated bootstrap requires an origin tracking base ref")
    relative_scripts = f"{args.harness}/skills/agent-loop/scripts"
    helper_relative = f"{relative_scripts}/isolate-repository.py"
    runner_relative = f"{relative_scripts}/agent-loop.sh"
    pinned_files: dict[str, bytes] = {}

    def pinned(
        relative: str, optional: bool = False, executable: bool = False
    ) -> bytes | None:
        entry = run(source, "ls-tree", base, "--", relative).decode().strip()
        path = source / relative
        if not entry:
            if optional and not path.exists() and not path.is_symlink():
                return None
            raise IsolationError(
                f"isolated bootstrap requires a committed base file: {relative}"
            )
        fields = entry.split(None, 3)
        if fields[0] not in ("100644", "100755") or fields[1] != "blob":
            raise IsolationError(
                f"isolated bootstrap requires a regular base file: {relative}"
            )
        if executable and fields[0] != "100755":
            raise IsolationError(
                f"isolated bootstrap requires an executable base file: {relative}"
            )
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise IsolationError(
                f"isolated bootstrap requires a regular source file: {relative}"
            )
        content = run(source, "cat-file", "blob", fields[2])
        if path.read_bytes() != content:
            raise IsolationError(
                f"isolated bootstrap rejects uncommitted configuration or tools: {relative}"
            )
        if bool(path.stat().st_mode & 0o111) != (fields[0] == "100755"):
            raise IsolationError(
                f"isolated bootstrap rejects changed executable mode: {relative}"
            )
        pinned_files[relative] = content
        return content

    pinned(helper_relative, executable=True)
    pinned(runner_relative, executable=True)
    pinned(f"{args.harness}/skills/agent-loop/agent-loop.config")
    pinned(f"{args.harness}/skills/agent-loop/prompt.txt", optional=True)
    pinned("agent-loop-instructions.md")
    config_before = run(source, "config", "--null", "--list")
    fetch_urls = run(source, "remote", "get-url", "--all", "origin")
    push_urls = run(source, "remote", "get-url", "--push", "--all", "origin")
    scoped_config = run(
        source, "config", "--includes", "--show-scope", "--null", "--list"
    )
    if run(source, "config", "--null", "--list") != config_before:
        raise IsolationError(
            "source Git configuration changed during isolated snapshot"
        )
    fields = scoped_config.split(b"\0")
    if fields[-1:] != [b""] or len(fields) % 2 != 1:
        raise IsolationError("Git configuration snapshot has an invalid structure")
    config_entries = []
    for scope, record in zip(fields[0:-1:2], fields[1:-1:2]):
        if scope not in (b"local", b"worktree"):
            continue
        key, separator, value = record.partition(b"\n")
        config_entries.append((key.decode(), value.decode() if separator else "true"))
    if destination.exists() or destination.is_symlink():
        raise IsolationError("isolated destination must not already exist")
    os.umask(0o077)
    destination.mkdir(mode=0o700)
    repository = destination / "repository"
    controller = destination / "controller"
    print(f"Isolated controller (retained for recovery): {controller}", flush=True)
    run(
        source,
        "clone",
        "--no-local",
        "--no-checkout",
        "--",
        str(source),
        str(repository),
    )
    # Fetch by the captured object, not a source ref that may move concurrently.
    run(repository, "fetch", "--no-tags", "--", str(source), base)
    run(repository, "update-ref", base_ref, base)
    run(repository, "config", "--remove-section", "remote.origin")
    for name, value in config_entries:
        if name.lower() in {"remote.origin.url", "remote.origin.pushurl"}:
            run(repository, "config", "--local", "--add", name, value)
    run(
        repository,
        "config",
        "--local",
        "--add",
        "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/*",
    )
    excluded = {"core.bare", "core.worktree", "core.repositoryformatversion"}
    for name, value in config_entries:
        normalized = name.lower()
        if normalized in excluded or normalized.split(".", 1)[0] in {
            "extensions",
            "remote",
            "branch",
            "include",
            "includeif",
        }:
            continue
        run(repository, "config", "--local", "--add", name, value)
    if (
        run(repository, "remote", "get-url", "--all", "origin") != fetch_urls
        or run(repository, "remote", "get-url", "--push", "--all", "origin")
        != push_urls
    ):
        raise IsolationError(
            "isolated origin identity differs from its verified source"
        )
    run(repository, "worktree", "add", "--detach", "--", str(controller), base)
    for relative, expected_bytes in pinned_files.items():
        copied_file = controller / relative
        if (
            copied_file.is_symlink()
            or copied_file.resolve() != copied_file
            or copied_file.read_bytes() != expected_bytes
        ):
            raise IsolationError(
                "isolated bootstrap file differs from its verified source"
            )
    child_args = args.child_args
    if child_args[:1] == ["--"]:
        child_args = child_args[1:]
    env["AGENT_LOOP_PROJECT_DIR"] = str(controller)
    os.chdir(controller)
    os.execve(
        str(controller / runner_relative),
        [str(controller / runner_relative), *child_args],
        env,
    )


if __name__ == "__main__":
    try:
        main()
    except (IsolationError, OSError, UnicodeError) as exc:
        if isinstance(exc, IsolationError):
            print(f"isolation: {exc}", file=sys.stderr)
        else:
            print(
                "isolation: filesystem or configuration operation failed",
                file=sys.stderr,
            )
        sys.exit(1)
