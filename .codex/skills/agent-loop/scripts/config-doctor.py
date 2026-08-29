#!/usr/bin/env python3
"""Non-mutating compatibility preflight for consumer agent-loop configuration."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


class DoctorError(RuntimeError):
    """An incompatible consumer configuration."""


def _config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-z_]+)\s*=\s*(.*)", line)
        if match is None:
            raise DoctorError(f"invalid config line: {raw}")
        key, value = match.groups()
        if key in values:
            raise DoctorError(f"duplicate config key: {key}")
        values[key] = value.rstrip()
    return values


def _version(command: list[str], label: str) -> str:
    # `command[0]` may be a PATH lookup (`node`) rather than an interpreter we
    # know exists, so a missing runtime must read as a doctor failure instead
    # of an uncaught OSError traceback.
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise DoctorError(f"{label} could not be executed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or "<no stderr>"
        raise DoctorError(
            f"{label} compatibility query failed (exit {result.returncode}): {detail}"
        )
    return result.stdout.strip()


def _require_base_blob(project: Path, base_ref: str, relative: str) -> tuple[str, str]:
    result = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(project),
            "ls-tree",
            "-z",
            base_ref,
            "--",
            relative,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (
            result.stderr.decode("utf-8", errors="replace").strip() or "<no stderr>"
        )
        raise DoctorError(f"could not inspect pinned base review surface: {detail}")
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1:
        raise DoctorError(f"pinned base review surface is missing: {relative}")
    metadata, separator, path = records[0].partition(b"\t")
    fields = metadata.split()
    if (
        separator != b"\t"
        or path != relative.encode()
        or len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
    ):
        raise DoctorError(
            f"pinned base review surface is not a regular blob: {relative}"
        )
    return fields[0].decode(), fields[2].decode()


def _verify_protocols(ledger: Path, state: Path, review_push: Path) -> None:
    if _version(["node", str(ledger), "--protocol-version"], "review ledger") != "3":
        raise DoctorError("review-ledger protocol is incompatible with contract v3")
    if _version([sys.executable, "-I", str(state), "--state-version"], "run state") != "1":
        raise DoctorError("agent-loop state protocol is incompatible")
    if _version([str(review_push), "--protocol-version"], "review push") != "1":
        raise DoctorError("review-push protocol is incompatible")


def doctor(project: Path, claude_effort: str | None, base_ref: str | None) -> None:
    root = project.resolve()
    skill = root / ".codex/skills/agent-loop"
    config_path = skill / "agent-loop.config"
    prompt_path = skill / "prompt.txt"
    instructions_path = root / "agent-loop-instructions.md"
    ledger = root / ".codex/skills/critique/scripts/review-ledger.js"
    state = skill / "scripts/agent-loop-state.py"
    review_push = skill / "scripts/review-push.sh"
    review_launcher = skill / "scripts/run-codex-review.sh"
    for path in (
        config_path,
        prompt_path,
        instructions_path,
        ledger,
        state,
        review_push,
        review_launcher,
    ):
        if not path.is_file() or path.is_symlink():
            raise DoctorError(
                f"required agent-loop file is missing: {path.relative_to(root)}"
            )
    values = _config(config_path)
    contract = values.get("review_contract_version")
    if contract not in {"3", "4"}:
        raise DoctorError("review_contract_version must be 3 or 4")
    prompt = prompt_path.read_text(encoding="utf-8")
    instructions = instructions_path.read_text(encoding="utf-8")
    for token in ("AGENT_LOOP_ISSUE_TITLE", "AGENT_LOOP_ISSUE_BODY"):
        if token not in prompt:
            raise DoctorError(f"worker prompt must read {token}")
    if re.search(r"\bgh\s+(?:api|issue|pr|repo)\b", prompt + "\n" + instructions):
        raise DoctorError("worker prompt or instructions require masked gh")
    if "local commit" not in prompt.lower() or "do not push" not in prompt.lower():
        raise DoctorError("worker prompt must require a local commit and forbid push")
    if (
        "AGENT_LOOP_ISSUE_TITLE" not in instructions
        or "AGENT_LOOP_ISSUE_BODY" not in instructions
    ):
        raise DoctorError(
            "worker instructions must describe wrapper-provided issue context"
        )

    hooks = {
        "codex": values.get("codex_review_hook", ""),
        "claude": values.get("claude_review_hook", ""),
    }
    if contract == "4":
        if not base_ref:
            raise DoctorError("contract v4 compatibility requires --base-ref")
        launcher_relative = ".codex/skills/agent-loop/scripts/run-codex-review.sh"
        launcher_mode, launcher_oid = _require_base_blob(
            root, base_ref, launcher_relative
        )
        if launcher_mode != "100755":
            raise DoctorError("pinned review launcher is not executable")
        local_launcher_oid = _version(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(root),
                "hash-object",
                "--no-filters",
                str(review_launcher),
            ],
            "review launcher",
        )
        if local_launcher_oid != launcher_oid:
            raise DoctorError("review launcher differs from the pinned base blob")
        expected_hooks = {
            "codex": '"$AGENT_LOOP_CODEX_REVIEW_LAUNCHER" --engine codex',
            "claude": '"$AGENT_LOOP_CODEX_REVIEW_LAUNCHER" --engine claude',
        }
        for engine, expected in expected_hooks.items():
            if hooks[engine] != expected:
                raise DoctorError(
                    f"{engine}_review_hook must use the dedicated contract-v4 review launcher"
                )
        if (
            _version([str(review_launcher), "--contract-version"], "review launcher")
            != "4"
        ):
            raise DoctorError("review launcher is incompatible with contract v4")
        launcher_effort = _version(
            [str(review_launcher), "--claude-effort-policy"], "review launcher"
        )
        if claude_effort and launcher_effort != claude_effort:
            raise DoctorError(f"review launcher must pin Claude effort {claude_effort}")
        wrapper_paths = _version(
            [str(review_launcher), "--wrapper-paths"], "wrapper review tools"
        ).splitlines()
        if not wrapper_paths or len(wrapper_paths) != len(set(wrapper_paths)):
            raise DoctorError("wrapper required review tools are invalid")
        for relative in wrapper_paths:
            if not (
                relative.startswith(".codex/skills/agent-loop/scripts/")
                or relative == ".codex/skills/issues/scripts/ready.py"
                or relative == ".codex/skills/critique/scripts/review-ledger.js"
            ):
                raise DoctorError(
                    f"wrapper required review path is invalid: {relative}"
                )
            local_path = root / relative
            if not local_path.is_file() or local_path.is_symlink():
                raise DoctorError(f"required review tool is missing: {relative}")
            mode, oid = _require_base_blob(root, base_ref, relative)
            expected_mode = (
                "100644"
                if relative == ".codex/skills/critique/scripts/review-ledger.js"
                else "100755"
            )
            if mode != expected_mode:
                raise DoctorError(f"pinned review tool has an unsafe mode: {relative}")
            local_oid = _version(
                [
                    "git",
                    "--no-replace-objects",
                    "-C",
                    str(root),
                    "hash-object",
                    "--no-filters",
                    str(local_path),
                ],
                f"review tool {relative}",
            )
            if local_oid != oid:
                raise DoctorError(
                    f"review tool differs from the pinned base blob: {relative}"
                )
        _verify_protocols(ledger, state, review_push)
        supervisor = root / ".codex/skills/agent-loop/scripts/process-supervisor.py"
        if _version(
            [sys.executable, "-I", str(supervisor), "--self-test"],
            "hook process supervisor",
        ) != (
            "linux-subreaper-v1"
        ):
            raise DoctorError("hook process supervisor self-test failed")
        for engine, prefix in (("codex", ".codex/"), ("claude", ".claude/")):
            required_paths = _version(
                [str(review_launcher), "--required-paths", engine],
                f"{engine} review surface",
            ).splitlines()
            if not required_paths or len(required_paths) != len(set(required_paths)):
                raise DoctorError(f"{engine} required review surface is invalid")
            for relative in required_paths:
                if not relative.startswith(prefix):
                    raise DoctorError(
                        f"{engine} required review path is invalid: {relative}"
                    )
                _require_base_blob(root, base_ref, relative)
    else:
        _verify_protocols(ledger, state, review_push)
        obsolete = (
            "AGENT_LOOP_REVIEW_OUTCOME_FILE",
            "local-review-pass:v1",
            "local-review-complete:v1",
            "local-review-disposition:v1",
            "review-ledger.py",
        )
        for engine, hook in hooks.items():
            if any(token in hook for token in obsolete):
                raise DoctorError(
                    f"{engine}_review_hook contains obsolete review ownership"
                )
            if (
                "AGENT_LOOP_REVIEW_RESULT_FILE" not in hook
                or "write-result" not in hook
            ):
                raise DoctorError(
                    f"{engine}_review_hook must preserve the contract-v3 result and push helper contract"
                )
            # v4 pins both hook strings byte-for-byte, so this stays the only
            # thing standing between a v3 consumer and a hook that pushes
            # directly instead of through the wrapper-owned helper.
            if "AGENT_LOOP_REVIEW_PUSH_HELPER" not in hook or re.search(
                r"\bgit\s+push\b", hook
            ):
                raise DoctorError(
                    f"{engine}_review_hook must preserve the contract-v3 result and push helper contract"
                )
            # Accept any shell delimiter after the skill name; the point is to
            # reject a different skill (a retired `grill` name, or a
            # `deepcritique`-prefixed word), not to enumerate separators.
            if not re.search(r"(?:^|[ /])deepcritique(?![\w-])", hook):
                raise DoctorError(f"{engine}_review_hook must invoke deepcritique")
        if claude_effort:
            efforts = re.findall(r"(?:^|\s)--effort(?:=|\s+)([^\s;]+)", hooks["claude"])
            if efforts != [claude_effort]:
                raise DoctorError(
                    f"claude_review_hook must use exactly one literal --effort {claude_effort}"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--claude-effort")
    parser.add_argument("--base-ref")
    args = parser.parse_args()
    doctor(Path(args.project_dir), args.claude_effort, args.base_ref)
    print("agent-loop config doctor: compatible")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DoctorError, OSError) as error:
        print(f"agent-loop config doctor: {error}", file=sys.stderr)
        raise SystemExit(1) from error
