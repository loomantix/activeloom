"""Fail closed on incomplete Python test partitions and upstream job failures."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from tests.conftest import _load_script
from tests.test_ci_workflows import _workflow


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    full = [f"tests/test_example.py::test_{i}" for i in range(8)]
    for index in range(1, 5):
        shard = tmp_path / f"python-shard-{index}"
        shard.mkdir()
        (shard / "full.txt").write_text("\n".join(full) + "\n8 tests collected\n")
        (shard / "selected.txt").write_text("\n".join(full[index - 1::4]))
        (shard / ".coverage").write_bytes(b"coverage data")
        (shard / "junit.xml").write_text("<testsuites/>")
    return tmp_path


def test_complete_partition(artifacts: Path) -> None:
    module = _load_script("verify_pytest_shards", Path("scripts/verify-pytest-shards.py"))
    assert module.verify(artifacts) == 8


@pytest.mark.parametrize("damage", ["missing", "duplicate", "empty", "different", "coverage"])
def test_incomplete_partition_fails(artifacts: Path, damage: str) -> None:
    module = _load_script("verify_pytest_shards", Path("scripts/verify-pytest-shards.py"))
    shard = artifacts / "python-shard-4"
    if damage == "missing":
        (shard / "selected.txt").unlink()
    elif damage == "duplicate":
        (shard / "selected.txt").write_text((artifacts / "python-shard-1/selected.txt").read_text())
    elif damage == "empty":
        (shard / "selected.txt").write_text("no tests ran\n")
    elif damage == "different":
        (shard / "full.txt").write_text("tests/test_other.py::test_other\n")
    else:
        (shard / ".coverage").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        module.verify(artifacts)


@pytest.mark.parametrize("tests", ["success", "failure", "skipped", "cancelled", ""])
def test_regression_gate_requires_all_shards(tests: str) -> None:
    job = _workflow("regression.yml")["jobs"]["python-types-and-tests"]
    script = job["steps"][0]["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={"TEST_RESULT": tests},
        check=False,
    )
    assert (result.returncode == 0) == (tests == "success")


def test_matrix_and_manual_checkpoint() -> None:
    workflow = _workflow("regression.yml")
    assert "workflow_dispatch" in workflow["on"]
    jobs = workflow["jobs"]
    matrix = jobs["python-tests"]["strategy"]
    assert matrix["matrix"]["shard"] == ["1", "2", "3", "4"]
    assert matrix["fail-fast"] == "false"
    assert jobs["python-types-and-tests"]["needs"] == "python-tests"
    assert "!cancelled()" in jobs["python-types-and-tests"]["if"]
    for step in jobs["python-tests"]["steps"]:
        if "python3 -m pytest" in step.get("run", ""):
            for command in step["run"].splitlines():
                if "python3 -m pytest" in command:
                    assert "--test-lane=regression" in command
