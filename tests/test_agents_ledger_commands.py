"""The Agy critique skill's written-out ledger commands match the shipped helpers.

The commands are copied verbatim by the engine, so each flag they use must be
one the vendored helper or run controller lists in its own `--help`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".agents/skills/critique/SKILL.md"
HELPER = ".agents/skills/critique/scripts/review-ledger.js"
CONTROLLER = ".agents/skills/critique/scripts/local-review-handoff.py"
PREFIXES = {
    f"node {HELPER}": ["node", HELPER],
    f"python3 -I {CONTROLLER}": ["python3", "-I", CONTROLLER],
}


def _commands() -> list[tuple[str, str, list[str]]]:
    text = SKILL.read_text(encoding="utf-8")
    joined = re.sub(r"\\\n\s*", " ", text)
    found = []
    for prefix in PREFIXES:
        for match in re.finditer(
            re.escape(prefix) + r"\s+([a-z][a-z-]+)([^\n`]*)", joined
        ):
            flags = sorted(set(re.findall(r"--[a-z0-9-]+", match.group(2))))
            found.append((prefix, match.group(1), flags))
    return found


COMMANDS = _commands()


def test_skill_writes_out_the_pass_commands() -> None:
    names = {name for _, name, _ in COMMANDS}
    assert {
        "authorize-pass",
        "post-finding",
        "authorize-fix",
        "preflight-fix",
        "reconcile",
        "dispose",
        "write-result",
        "validate-result",
        "export-threads",
        "attest",
    } <= names


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize(
    ("prefix", "name", "flags"),
    COMMANDS,
    ids=[f"{name}" for _, name, _ in COMMANDS],
)
def test_written_command_uses_only_flags_its_help_lists(
    prefix: str, name: str, flags: list[str]
) -> None:
    result = subprocess.run(
        [*PREFIXES[prefix], name, "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # Only the synopsis lists the flags a command takes; help notes name other
    # commands' flags. The helper's synopsis is its first line; argparse wraps
    # the controller's usage block until the first blank line.
    synopsis = result.stdout.split("\n\n", 1)[0]
    assert synopsis.startswith("usage:"), result.stdout
    if flags == ["--help"]:
        # Help itself is global; the response must identify this particular subcommand.
        assert synopsis.startswith(f"usage: review-ledger {name} ")
        return
    listed = set(re.findall(r"--[a-z0-9-]+", synopsis))
    assert set(flags) <= listed, sorted(set(flags) - listed)
