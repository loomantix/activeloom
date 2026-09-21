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

import functools
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
HARNESS_ROOTS = (".claude", ".codex", ".agents")

# `[^)]*` and `\s*` span newlines on their own, which is what lets this match the
# formatter-wrapped form `.codex` carries. No `.` appears in the pattern, so no
# DOTALL is needed.
PIN_RE = re.compile(r'"--state-version"\][^)]*,\s*"run state"\)\s*\)?\s*!=\s*"(\d+)"')


def _script(root: str, name: str) -> Path:
    return ROOT / root / "skills/agent-loop/scripts" / name


@functools.lru_cache(maxsize=None)
def _state_version(root: str) -> str:
    result = subprocess.run(
        [sys.executable, str(_script(root, "agent-loop-state.py")), "--state-version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _doctor_pin(root: str) -> str:
    doctor = _script(root, "config-doctor.py").read_text(encoding="utf-8")
    pins: list[str] = PIN_RE.findall(doctor)
    # Exactly one, so a second `--state-version` call site cannot leave this
    # asserting against the wrong pin while staying green.
    assert len(pins) == 1, (
        f"{root} doctor has {len(pins)} run-state version pins, want 1"
    )
    return pins[0]


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_doctor_pins_the_version_its_own_state_helper_reports(root: str) -> None:
    assert _doctor_pin(root) == _state_version(root)


# The version each root's run-state schema reached when `reviewSettings` was
# versioned. A root may move past its floor; falling back to or below it is what
# a coordinated revert of a constant and its doctor pin looks like, and the
# doctor-parity test above cannot see that on its own. Raise a floor deliberately
# as part of each future bump.
REVIEW_SETTINGS_FLOOR = {".claude": 2, ".codex": 2, ".agents": 3}


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_review_settings_bearing_schema_meets_its_version_floor(root: str) -> None:
    """`reviewSettings` landed in all three roots, so none may drop below its floor."""
    helper = _script(root, "agent-loop-state.py").read_text(encoding="utf-8")
    assert "reviewSettings" in helper, (
        f"{root} no longer carries reviewSettings; its floor needs revisiting"
    )
    assert int(_state_version(root)) >= REVIEW_SETTINGS_FLOOR[root]
