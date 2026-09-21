"""Each harness root's run-state version must match the version its own doctor pins.

The three roots carry independent version lineages (`.claude` and `.codex` at 2,
`.agents` at 3) because they were promoted out of three separate upstream repos,
so this asserts per-root agreement rather than a single shared number.

The drift this guards against is silent: when `reviewSettings` was added to all
three run-state schemas, no root bumped its version. Backward compatibility held
(the new validator subtracts the key), but a consumer that rolled back mid-run
could not tell the two schemas apart and had its state file rejected by a
validator that rejects unknown fields.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
HARNESS_ROOTS = (".claude", ".codex", ".agents")

# Tolerates the formatter wrapping the comparison onto its own line, which is
# how `.codex` carries it.
PIN_RE = re.compile(
    r'"--state-version"\][^)]*,\s*"run state"\)\s*\)?\s*!=\s*"(\d+)"',
    re.DOTALL,
)


def _state_version(root: str) -> str:
    helper = ROOT / root / "skills/agent-loop/scripts/agent-loop-state.py"
    result = subprocess.run(
        [sys.executable, str(helper), "--state-version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _doctor_pin(root: str) -> str:
    doctor = (ROOT / root / "skills/agent-loop/scripts/config-doctor.py").read_text(
        encoding="utf-8"
    )
    match = PIN_RE.search(doctor)
    assert match is not None, f"{root} doctor has no run-state version pin"
    return match.group(1)


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_doctor_pins_the_version_its_own_state_helper_reports(root: str) -> None:
    assert _doctor_pin(root) == _state_version(root)


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_review_settings_bearing_schema_is_past_version_one(root: str) -> None:
    """`reviewSettings` landed in all three roots, so none may still read as v1."""
    helper = (ROOT / root / "skills/agent-loop/scripts/agent-loop-state.py").read_text(
        encoding="utf-8"
    )
    if "reviewSettings" not in helper:
        pytest.skip(f"{root} does not carry reviewSettings")
    assert int(_state_version(root)) > 1
