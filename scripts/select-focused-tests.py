#!/usr/bin/env python3
"""Map changed paths to the regression-marked test modules that exercise them.

Reads one repository path per line on stdin and prints the selected test
modules, one per line. Routine CI runs that selection in the focused lane, so a
change to an integration surface is tested before merge without running the
complete regression suite for every change.
"""

from __future__ import annotations

import re
import sys


ROOTS = (".agents", ".claude", ".codex")


def _skill(name: str) -> tuple[str, ...]:
    return (*(f"{root}/skills/{name}/**" for root in ROOTS), f"prompts/skills/{name}/**")


# Source globs -> the integration modules that cover them. `**` crosses
# directories; `*` does not.
GROUPS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "agent_loop": (
        _skill("agent-loop"),
        (
            "tests/test_agent_loop.py",
            "tests/test_agent_loop_doctor.py",
            "tests/test_agent_loop_isolation.py",
            "tests/test_agent_loop_state.py",
            "tests/test_agy_agent_loop.py",
            "tests/test_codex_agent_loop.py",
            "tests/test_codex_agent_loop_doctor.py",
            "tests/test_process_supervisor.py",
            "tests/test_review_push.py",
        ),
    ),
    "review_runner": (
        (
            *_skill("critique"),
            *_skill("review-setup"),
            *_skill("reviewit"),
            ".codex/references/review-chain-runner.md",
            ".activeloom-review.json",
        ),
        (
            "tests/test_review_chain_runner.py",
            "tests/test_review_restart_rounds.py",
            "tests/test_review_settings.py",
            "tests/test_agy_review_launcher.py",
            "tests/test_claude_review_launcher.py",
        ),
    ),
    "telemetry": (
        (
            *(f"{root}/skills/critique/scripts/usage-snapshot.js" for root in ROOTS),
            *(f"{root}/skills/critique/scripts/review-telemetry-gates.js" for root in ROOTS),
            *(f"{root}/skills/critique/scripts/telemetry-pass-key.js" for root in ROOTS),
            "scripts/review-telemetry.json.template",
        ),
        (
            "tests/test_telemetry_gates.py",
            "tests/test_telemetry_pass_key.py",
            "tests/test_usage_snapshot.py",
        ),
    ),
    "prompt_stack": (
        (
            *(f"{root}/skills/critique/scripts/prompt-stack-hash.js" for root in ROOTS),
            *(f"{root}/prompt-stack.json" for root in ROOTS),
            "PROMPT_STACK_VERSION",
        ),
        ("tests/test_prompt_stack_hash.py",),
    ),
    "parity": (
        ("scripts/lint-prompt-parity.py",),
        ("tests/test_prompt_parity_lint.py",),
    ),
    "sync_engine": (
        ("scripts/sync-engine.py", "scripts/sync-targets.yml", "tests/fixtures/**"),
        ("tests/test_sync_engine_integration.py", "tests/test_cli_init_equivalence.py"),
    ),
    "cli": (
        ("cli/**",),
        ("tests/test_cli_init_equivalence.py",),
    ),
}

# Shared test machinery: a change here can break any deferred module.
EVERYTHING = (
    "tests/conftest.py",
    "tests/__init__.py",
    "scripts/run-tests.py",
    "scripts/select-focused-tests.py",
    "pyproject.toml",
    ".nvmrc",
)


def all_modules() -> list[str]:
    return sorted({module for _, modules in GROUPS.values() for module in modules})


def matches(path: str, pattern: str) -> bool:
    parts = (
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(\*\*|\*)", pattern)
    )
    return re.fullmatch("".join(parts), path) is not None


def select(paths: list[str]) -> list[str]:
    known = set(all_modules())
    selected: set[str] = set()
    for path in paths:
        if path in EVERYTHING:
            return sorted(known)
        if path in known:
            selected.add(path)
        for patterns, modules in GROUPS.values():
            if any(matches(path, pattern) for pattern in patterns):
                selected.update(modules)
    return sorted(selected)


def main() -> int:
    paths = [line.strip() for line in sys.stdin if line.strip()]
    for module in select(paths):
        print(module)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
