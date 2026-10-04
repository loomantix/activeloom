"""Exercise test selection through pytest, including its failure exit codes."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tests.conftest import _load_script


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def suite(tmp_path: Path) -> Path:
    (tmp_path / "conftest.py").write_text((ROOT / "tests/conftest.py").read_text())
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    regression: integration scenario\n    fast: cheap contract\n"
    )
    (tmp_path / "test_unit.py").write_text(
        "from pathlib import Path\n"
        "def test_unit(): Path('unit-ran').touch()\n"
    )
    (tmp_path / "test_integration.py").write_text(
        "from pathlib import Path\nimport pytest\n"
        "pytestmark = pytest.mark.regression\n"
        "def test_slow(): Path('regression-ran').touch()\n"
        "@pytest.mark.fast\n"
        "def test_contract(): Path('contract-ran').touch()\n"
    )
    return tmp_path


def run_suite(suite: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *args],
        cwd=suite, env=env, capture_output=True, text=True, timeout=15, check=False,
    )


@pytest.mark.parametrize("args", [(), ("--test-lane=fast",)])
def test_routine_selection_runs_units_and_contracts_without_integration(
    suite: Path, args: tuple[str, ...],
) -> None:
    result = run_suite(suite, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (suite / "unit-ran").exists()
    assert (suite / "contract-ran").exists()
    assert not (suite / "regression-ran").exists()
    assert "1 deselected" in result.stdout


def test_regression_preserves_the_complete_suite(suite: Path) -> None:
    result = run_suite(suite, "--test-lane=regression")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
    assert (suite / "regression-ran").exists()


def test_focused_selection_runs_only_the_requested_integration_case(suite: Path) -> None:
    result = run_suite(suite, "--test-lane=focused", "test_integration.py::test_slow")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert (suite / "regression-ran").exists()
    assert not (suite / "unit-ran").exists()
    assert not (suite / "contract-ran").exists()


@pytest.mark.parametrize("args", [(), (".",), ("-k", "slow")])
def test_focused_lane_requires_explicit_test_files(
    suite: Path, args: tuple[str, ...],
) -> None:
    result = run_suite(suite, "--test-lane=focused", *args)
    assert result.returncode == 4
    assert "explicit test files" in result.stderr
    assert not (suite / "regression-ran").exists()


def test_empty_fast_selection_fails_instead_of_claiming_coverage(suite: Path) -> None:
    result = run_suite(suite, "test_integration.py::test_slow")
    assert result.returncode == 5
    assert not (suite / "regression-ran").exists()


def test_unknown_lane_cannot_run_the_suite(suite: Path) -> None:
    result = run_suite(suite, "--test-lane=typo")
    assert result.returncode == 4
    assert not (suite / "unit-ran").exists()


def test_bounded_runner_preserves_test_failure() -> None:
    runner = _load_script("run_tests", ROOT / "scripts/run-tests.py")
    assert runner.run([sys.executable, "-c", "raise SystemExit(7)"], 5) == 7


@pytest.mark.parametrize("paths", [[], ["unknown.file"], ["cli/index.js"], ["prompts/example.md"],
                                   ["cli/index.js", "unknown.file"]])
def test_review_contract_runs_fast_tests_once_for_mapped_and_unmapped_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paths: list[str],
) -> None:
    module = _load_script("lane_contract_runner", ROOT / ".codex/skills/critique/scripts/review-chain-runner.py")
    contract = module.parse_validation_contract((ROOT / ".activeloom-review.json").read_bytes())
    runner = module.Runner(SimpleNamespace(), tmp_path)
    runner.state = {"base": "b" * 40, "config": {"validation": {
        "mode": "contract-v1", "contract": contract, "base_environment": {},
        "policy_revision": "b" * 40, "manifest_sha256": "c" * 64,
    }}}
    monkeypatch.setattr(runner, "changed_paths", lambda base, head: paths)
    commands = [item["argv"] for item in runner.resolved_validation("a" * 40)["commands"]]
    assert sum(argv == ["python3", "scripts/run-tests.py", "fast"] for argv in commands) == 1
    assert not any("regression" in " ".join(argv) for argv in commands)


@pytest.mark.skipif(os.name != "posix", reason="POSIX worker process groups")
@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "sigterm"])
def test_bounded_runner_stops_its_workers(tmp_path: Path, cancel: bool) -> None:
    ready = tmp_path / "worker.pid"
    worker = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {worker!r}]); time.sleep(60)"
    )
    launcher = (
        "import runpy, sys; "
        f"module = runpy.run_path({str(ROOT / 'scripts/run-tests.py')!r}); "
        f"raise SystemExit(module['run']([sys.executable, '-c', {parent!r}], 3))"
    )
    process = subprocess.Popen([sys.executable, "-c", launcher])
    worker_pid: int | None = None
    group: int | None = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "worker never started"
        worker_pid = int(ready.read_text())
        group = os.getpgid(worker_pid)
        if cancel:
            process.terminate()
        assert process.wait(timeout=10) == (143 if cancel else 124)
        # A killed orphan may briefly remain a zombie until init reaps it.
        if Path(f"/proc/{worker_pid}/stat").exists():
            assert Path(f"/proc/{worker_pid}/stat").read_text().split(")", 1)[1].split()[0] == "Z"
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(worker_pid, 0)
    finally:
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait()
