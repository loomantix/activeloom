#!/usr/bin/env python3
"""Compatibility preflight for consumer agent-loop configuration.

The check is non-mutating: it reports what to change and never edits the config.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


class DoctorError(RuntimeError):
    """An incompatible consumer configuration."""


# Keys whose settings now come from the per-user review profile.
RETIRED_KEYS = ("claude_effort_policy", "worker_model", "worker_fallback_model", "worker_effort")


def _warn(message: str) -> None:
    print(f"agent-loop config doctor: warning: {message}", file=sys.stderr)


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


def _check_retired_keys(values: dict[str, str]) -> None:
    for key in RETIRED_KEYS:
        if key not in values:
            continue
        if values[key]:
            raise DoctorError(
                f"{key} is retired: worker settings come from the per-user review profile. "
                "Remove it from the config"
            )
        _warn(f"{key} is retired and has no effect; remove it from the config")


def doctor(project: Path) -> None:
    root = project.resolve()
    skill = root / ".agents/skills/agent-loop"
    config_path = skill / "agent-loop.config"
    prompt_path = skill / "prompt.txt"
    instructions_path = root / "agent-loop-instructions.md"
    ledger = root / ".agents/skills/critique/scripts/review-ledger.js"
    state = skill / "scripts/agent-loop-state.py"
    review_push = skill / "scripts/review-push.sh"
    worker_launcher = skill / "scripts/run-agy-worker.sh"
    review_launcher = skill / "scripts/run-agy-review.sh"
    launch_helper = skill / "scripts/run-agy-launch.sh"
    for path in (
        config_path,
        prompt_path,
        instructions_path,
        ledger,
        state,
        review_push,
        worker_launcher,
        review_launcher,
        launch_helper,
    ):
        if not path.is_file() or path.is_symlink():
            raise DoctorError(f"required agent-loop file is missing: {path.relative_to(root)}")
    values = _config(config_path)
    _check_retired_keys(values)
    if values.get("review_contract_version") != "3":
        raise DoctorError("review_contract_version must be 3")
    if _version(["node", str(ledger), "--protocol-version"], "review ledger") != "3":
        raise DoctorError("review-ledger protocol is incompatible with contract v3")
    if _version([sys.executable, str(state), "--state-version"], "run state") != "2":
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
        "gemini": values.get("gemini_review_hook", ""),
        "claude": values.get("claude_review_hook", ""),
    }
    expected_hooks = {
        "gemini": '"$AGENT_LOOP_AGY_REVIEW_LAUNCHER" --engine gemini',
        "claude": '"$AGENT_LOOP_AGY_REVIEW_LAUNCHER" --engine claude',
    }
    # The review hooks are the fixed launcher commands, so they carry no model
    # or effort literal that could disagree with the run's pinned settings.
    for engine, expected in expected_hooks.items():
        if hooks[engine] != expected:
            raise DoctorError(f"{engine}_review_hook must use the dedicated Agy launcher")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    args = parser.parse_args()
    doctor(Path(args.project_dir))
    print("agent-loop config doctor: compatible")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DoctorError, OSError) as error:
        print(f"agent-loop config doctor: {error}", file=sys.stderr)
        raise SystemExit(1) from error
