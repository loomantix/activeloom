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


def doctor(project: Path, claude_effort: str | None) -> None:
    root = project.resolve()
    skill = root / ".claude/skills/agent-loop"
    config_path = skill / "agent-loop.config"
    prompt_path = skill / "prompt.txt"
    instructions_path = root / "agent-loop-instructions.md"
    ledger = root / ".claude/skills/critique/scripts/review-ledger.js"
    state = skill / "scripts/agent-loop-state.py"
    review_push = skill / "scripts/review-push.sh"
    for path in (config_path, prompt_path, instructions_path, ledger, state, review_push):
        if not path.is_file():
            raise DoctorError(f"required agent-loop file is missing: {path.relative_to(root)}")
    values = _config(config_path)
    if values.get("review_contract_version") != "3":
        raise DoctorError("review_contract_version must be 3")
    if _version(["node", str(ledger), "--protocol-version"], "review ledger") != "3":
        raise DoctorError("review-ledger protocol is incompatible with contract v3")
    if _version([sys.executable, str(state), "--state-version"], "run state") != "1":
        raise DoctorError("agent-loop state protocol is incompatible")
    if _version([str(review_push), "--protocol-version"], "review push") != "1":
        raise DoctorError("review-push protocol is incompatible")

    prompt = prompt_path.read_text(encoding="utf-8")
    instructions = instructions_path.read_text(encoding="utf-8")
    for token in ("AGENT_LOOP_ISSUE_TITLE", "AGENT_LOOP_ISSUE_BODY"):
        if token not in prompt:
            raise DoctorError(f"worker prompt must read {token}")
    if re.search(r"\bgh\s+(?:api|issue|pr|repo)\b", prompt + "\n" + instructions):
        raise DoctorError("worker prompt or instructions require masked gh")
    if "local commit" not in prompt.lower() or "do not push" not in prompt.lower():
        raise DoctorError("worker prompt must require a local commit and forbid push")
    if "AGENT_LOOP_ISSUE_TITLE" not in instructions or "AGENT_LOOP_ISSUE_BODY" not in instructions:
        raise DoctorError("worker instructions must describe wrapper-provided issue context")

    hooks = {
        "codex": values.get("codex_review_hook", ""),
        "claude": values.get("claude_review_hook", ""),
    }
    for engine, hook in hooks.items():
        if not hook:
            raise DoctorError(f"{engine}_review_hook is missing")
        obsolete = (
            "AGENT_LOOP_REVIEW_OUTCOME_FILE",
            "local-review-pass:v1",
            "local-review-complete:v1",
            "local-review-disposition:v1",
            "review-ledger.py",
        )
        if any(token in hook for token in obsolete):
            raise DoctorError(f"{engine}_review_hook contains obsolete review ownership")
        if "AGENT_LOOP_REVIEW_RESULT_FILE" not in hook or "write-result" not in hook:
            raise DoctorError(f"{engine}_review_hook must use helper-owned contract-v3 results")
        if "AGENT_LOOP_REVIEW_PUSH_HELPER" not in hook or re.search(r"\bgit\s+push\b", hook):
            raise DoctorError(f"{engine}_review_hook must use the wrapper-owned review push helper")
    if not re.search(r"(?:^|[ /])deepcritique(?:[ $\"']|$)", hooks["codex"]):
        raise DoctorError("codex_review_hook must invoke deepcritique")
    if not re.search(r"(?:^|[ /])deepcritique(?:[ $\"']|$)", hooks["claude"]):
        raise DoctorError("claude_review_hook must invoke deepcritique")
    if claude_effort and not re.search(
        rf"(?:^|\s)--effort(?:=|\s+){re.escape(claude_effort)}(?:\s|$)", hooks["claude"]
    ):
        raise DoctorError(f"claude_review_hook must use literal --effort {claude_effort}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--claude-effort")
    args = parser.parse_args()
    doctor(Path(args.project_dir), args.claude_effort)
    print("agent-loop config doctor: compatible")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DoctorError, OSError) as error:
        print(f"agent-loop config doctor: {error}", file=sys.stderr)
        raise SystemExit(1) from error
