"""Policy tests for bounded pull-request Actions usage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent


def _workflow(name: str) -> dict[str, Any]:
    value = yaml.load(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(value, dict)
    return value


def test_ci_uses_draft_preflight_and_ready_full_gate() -> None:
    workflow = _workflow("ci.yml")
    pull_request = workflow["on"]["pull_request"]
    assert pull_request["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
        "converted_to_draft",
    ]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["concurrency"] == {
        "group": "ci-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }

    jobs = workflow["jobs"]
    preflight = jobs["draft-preflight"]
    assert preflight["if"] == (
        "github.event_name == 'pull_request' && github.event.pull_request.draft == true"
    )
    assert preflight["timeout-minutes"] == "5"
    assert len(preflight["steps"]) == 2
    preflight_text = preflight["steps"][1]["run"]
    assert "git diff --check" in preflight_text
    assert "python3 -m py_compile" in preflight_text
    assert "bash -n" in preflight_text
    assert "pnpm" not in preflight_text
    assert "pip install" not in preflight_text

    full_condition = (
        "github.event_name == 'push' || github.event.pull_request.draft == false"
    )
    assert jobs["static-checks"]["name"] == "Static checks"
    assert jobs["static-checks"]["if"] == full_condition
    assert jobs["static-checks"]["timeout-minutes"] == "20"
    assert jobs["python-types-and-tests"]["name"] == "Python types + tests"
    assert jobs["python-types-and-tests"]["if"] == full_condition
    assert jobs["python-types-and-tests"]["timeout-minutes"] == "20"


def test_pytest_run_pins_coverage_and_names_the_branch_config() -> None:
    """Subprocess coverage must read the same `branch = true` as the workers.

    pytest-cov exports `COV_CORE_CONFIG` — the config every auto-started
    subprocess reads — only when `--cov-config` names an existing file.
    Its default is `.coveragerc`, which this repo does not ship, so
    without the explicit flag subprocesses collect statement-only data
    while the in-process workers collect branch data, and `coverage
    combine` refuses the mix. `coverage` is pinned for the same reason
    every other tool is: 7.16.0 is what turned that mismatch from a
    silent merge into a hard `DataError`, and an unpinned floor means a
    future release can move the failure again.
    """
    steps = _workflow("ci.yml")["jobs"]["python-types-and-tests"]["steps"]
    install = next(s for s in steps if s.get("name") == "Install pinned tooling")
    assert "'coverage==7.16.0'" in install["run"]
    pytest_step = next(s for s in steps if s.get("run", "").startswith("python3 -m pytest"))
    assert "--cov-config=pyproject.toml" in pytest_step["run"]


def test_codeql_cancels_superseded_runs_and_skips_drafts() -> None:
    workflow = _workflow("codeql.yml")
    assert workflow["on"]["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
        "converted_to_draft",
    ]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["concurrency"] == {
        "group": "codeql-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    detector = workflow["jobs"]["python-changes"]
    assert detector["if"] == (
        "github.event_name == 'push' || github.event.pull_request.draft == false"
    )
    assert detector["timeout-minutes"] == "5"
    assert workflow["jobs"]["analyze"]["timeout-minutes"] == "30"


def test_dco_keeps_required_context_and_cancels_superseded_heads() -> None:
    workflow = _workflow("dco.yml")
    assert workflow["on"]["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
    ]
    assert workflow["concurrency"] == {
        "group": "dco-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }
    job = workflow["jobs"]["dco-check"]
    assert job["name"] == "DCO sign-off check"
    assert job["timeout-minutes"] == "5"
