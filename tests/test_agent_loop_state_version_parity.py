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
import json
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


# The batch schema is versioned separately from the run state, and has never
# changed. These are the numbers each root's batch schema carried before the
# run-state bump that added `reviewSettings`; a batch checkpoint written by any
# earlier helper must still validate, so this is an exact pin rather than a
# floor. Move a number here only in the same change that alters the batch schema.
BATCH_STATE_VERSION = {".claude": 1, ".codex": 1, ".agents": 2}


@functools.lru_cache(maxsize=None)
def _batch_state_version(root: str) -> str:
    result = subprocess.run(
        [
            sys.executable,
            str(_script(root, "agent-loop-state.py")),
            "--batch-state-version",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_batch_state_version_is_pinned_to_its_original_value(root: str) -> None:
    assert int(_batch_state_version(root)) == BATCH_STATE_VERSION[root]


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_batch_paths_do_not_read_the_run_state_constant(root: str) -> None:
    """The regression is textual: reusing STATE_VERSION re-couples the schemas.

    Pinning the number alone would stay green if a later edit pointed the batch
    validator back at the run-state constant while the two happened to agree.
    """
    helper = _script(root, "agent-loop-state.py").read_text(encoding="utf-8")
    batch = helper[helper.index("def _validate_batch(") :]
    validator = batch[: batch.index("\ndef ")]
    assert "BATCH_STATE_VERSION" in validator, f"{root} batch validator lost its own constant"
    assert "STATE_VERSION" not in validator.replace("BATCH_STATE_VERSION", ""), (
        f"{root} batch validator still reads the run-state constant"
    )


def _write_batch(path: Path, version: object) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": version,
                "kind": "batch",
                "runId": "r1",
                "repo": "o/r",
                "baseBranch": "main",
                "allowlist": [1],
                "cursor": 0,
                "issues": [{"issue": 1, "status": "pending", "childRunState": None}],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_batch_version_mismatch_names_the_version(root: str, tmp_path: Path) -> None:
    """A stale checkpoint must not be reported as structurally corrupt.

    Folding the version compare into the field-set boolean made an older batch
    file read as "missing, unknown, or unsupported fields", which sends the
    operator looking for corruption that is not there.
    """
    stale = _write_batch(tmp_path / "batch.json", BATCH_STATE_VERSION[root] + 1)
    result = subprocess.run(
        [sys.executable, str(_script(root, "agent-loop-state.py")), "batch-show", "--file", str(stale)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "unsupported batch state version" in result.stderr
    assert "missing" not in result.stderr


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_batch_checkpoint_at_its_pinned_version_validates(root: str, tmp_path: Path) -> None:
    """The bug this guards: a run-state bump invalidating in-flight batch state."""
    good = _write_batch(tmp_path / "batch.json", BATCH_STATE_VERSION[root])
    result = subprocess.run(
        [sys.executable, str(_script(root, "agent-loop-state.py")), "batch-show", "--file", str(good)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
