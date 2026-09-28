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


@pytest.mark.parametrize(
    ("plan", "skip", "tests", "passes"),
    [
        ("success", "false", "success", True),
        ("success", "true", "skipped", True),
        ("failure", "true", "skipped", False),
        ("success", "false", "failure", False),
        ("success", "false", "skipped", False),
        ("success", "false", "cancelled", False),
        ("success", "", "success", False),
        ("success", "true", "success", False),
    ],
)
def test_gate_requires_all_expected_jobs(plan: str, skip: str, tests: str, passes: bool) -> None:
    job = _workflow("ci.yml")["jobs"]["python-types-and-tests"]
    script = job["steps"][0]["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={"PLAN_RESULT": plan, "SKIP": skip, "TEST_RESULT": tests},
        check=False,
    )
    assert (result.returncode == 0) == passes


def test_matrix_and_manual_checkpoint() -> None:
    workflow = _workflow("ci.yml")
    assert "workflow_dispatch" in workflow["on"]
    jobs = workflow["jobs"]
    matrix = jobs["python-tests"]["strategy"]
    assert matrix["matrix"]["shard"] == ["1", "2", "3", "4"]
    assert matrix["fail-fast"] == "false"
    for name in ("python-plan", "static-checks", "python-types-and-tests"):
        assert "github.event_name != 'pull_request'" in jobs[name]["if"]
    assert set(jobs["python-types-and-tests"]["needs"]) == {"python-plan", "python-tests"}
    assert "!cancelled()" in jobs["python-types-and-tests"]["if"]
