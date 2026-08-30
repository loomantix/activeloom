"""Policy tests for bounded pull-request Actions usage.

These assert the *properties* the workflows must hold, not the text they happen
to be written in. A test that compares an `if:` expression or a `concurrency`
block against a copied literal is refreshed alongside any edit that breaks it,
so it detects renames rather than regressions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github/workflows"

# Status checks branch protection requires on `main`. `private-reference-policy`
# is posted by an off-repo scanner and has no job here; the other two are job
# `name:` values in this repository, and renaming one silently disables a
# required gate.
REQUIRED_CHECK_JOB_NAMES = frozenset({"Static checks", "DCO sign-off check"})


def _workflow_paths() -> list[Path]:
    # `.template` files are rendered per consumer and are not runnable workflows.
    return sorted(p for p in WORKFLOW_DIR.glob("*.y*ml") if p.suffix in {".yml", ".yaml"})


def _load(path: Path) -> dict[str, Any]:
    # BaseLoader keeps every scalar a string, which avoids YAML 1.1 turning the
    # `on:` key into the boolean True.
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def _workflow(name: str) -> dict[str, Any]:
    return _load(WORKFLOW_DIR / name)


def _jobs(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: job for name, job in workflow.get("jobs", {}).items() if isinstance(job, dict)
    }


def _draft_gated(job: dict[str, Any]) -> bool:
    return "draft == false" in job.get("if", "")


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda p: p.name)
def test_every_job_is_time_bounded(path: Path) -> None:
    """A job without `timeout-minutes` inherits the 360-minute default."""
    for name, job in _jobs(_load(path)).items():
        if "uses" in job:  # a reusable-workflow call carries its own bound
            continue
        assert "timeout-minutes" in job, f"{path.name}:{name} has no timeout-minutes"


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda p: p.name)
def test_pull_request_heads_are_superseded_but_main_is_never_queued(path: Path) -> None:
    """Obsolete PR heads may be cancelled; a push or scheduled run may not.

    `cancel-in-progress: false` is not enough on its own: a newly queued run
    cancels whatever is already *pending* in its group. So a non-PR run must
    land in a group of its own rather than sharing one keyed on `github.ref`.
    """
    workflow = _load(path)
    concurrency = workflow.get("concurrency")
    assert concurrency, f"{path.name} declares no concurrency group"
    group = concurrency["group"]
    triggers = workflow.get("on", {})
    if "push" in triggers or "schedule" in triggers:
        assert "github.ref" not in group, (
            f"{path.name} keys non-PR runs on github.ref, so push and schedule share "
            "a group and a pending run can be cancelled"
        )
        assert "github.event.pull_request.number" in group, (
            f"{path.name} must still group PR runs by PR number"
        )
    assert "pull_request" in str(concurrency["cancel-in-progress"]) or (
        "push" not in triggers and "schedule" not in triggers
    ), f"{path.name} may cancel a push or scheduled run in progress"


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda p: p.name)
def test_draft_gated_jobs_rerun_when_the_pull_request_is_marked_ready(path: Path) -> None:
    """A job that skips drafts must fire on `ready_for_review`.

    Without it the job never runs once the PR is ready, so a required check is
    reported `skipped` — which branch protection treats as satisfied — for the
    whole life of the pull request.
    """
    workflow = _load(path)
    if not any(_draft_gated(job) for job in _jobs(workflow).values()):
        return
    types = workflow["on"]["pull_request"]["types"]
    assert "ready_for_review" in types, f"{path.name} skips drafts but never re-runs on ready"


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda p: p.name)
def test_retargeted_stacked_pull_requests_still_run(path: Path) -> None:
    """A base-branch filter plus an explicit `types:` list needs `edited`.

    Retargeting a stacked PR onto `main` as its parent merges fires `edited` and
    nothing else, so without it the newly eligible PR runs no CI at all.
    """
    pull_request = _load(path).get("on", {}).get("pull_request")
    if not isinstance(pull_request, dict) or "types" not in pull_request:
        return
    if "branches" not in pull_request:
        return
    assert "edited" in pull_request["types"], f"{path.name} would skip a retargeted stacked PR"


def test_required_check_names_are_present_and_ungated() -> None:
    """The jobs backing required checks must exist under their exact names.

    A required check that never runs reports nothing and blocks; one that is
    skipped reports `skipped`, which counts as a pass. `dco-check` must
    therefore carry no draft gate at all.
    """
    found: dict[str, dict[str, Any]] = {}
    for path in _workflow_paths():
        for job in _jobs(_load(path)).values():
            name = job.get("name")
            if name in REQUIRED_CHECK_JOB_NAMES:
                found[name] = job
    assert set(found) == set(REQUIRED_CHECK_JOB_NAMES), (
        f"missing jobs for required checks: {sorted(REQUIRED_CHECK_JOB_NAMES - set(found))}"
    )
    assert "if" not in found["DCO sign-off check"], (
        "DCO is a required check and must run on drafts too; any `if:` here can "
        "turn it into a green skip"
    )


def test_codeql_keeps_its_scheduled_baseline_scan() -> None:
    """The weekly scan is what catches a latent finding no PR touched."""
    schedule = _workflow("codeql.yml")["on"]["schedule"]
    assert any("cron" in entry for entry in schedule)


def test_draft_preflight_is_cheap_and_installs_nothing() -> None:
    """The draft gate's whole point is that it is fast, so it may not install."""
    preflight = _workflow("ci.yml")["jobs"]["draft-preflight"]
    assert preflight["if"].endswith("github.event.pull_request.draft == true")
    steps = preflight["steps"]
    for step in steps:
        assert "pip install" not in step.get("run", "")
        assert "npm install" not in step.get("run", "")
        uses = step.get("uses", "")
        assert not uses or uses.startswith("actions/checkout@"), (
            f"draft-preflight may only check out, not install: {uses}"
        )
    checks = "\n".join(step.get("run", "") for step in steps)
    assert "git diff --check" in checks
    assert "python3 -m py_compile" in checks
    assert "bash -n" in checks


def test_full_gate_runs_on_pushes_and_on_ready_pull_requests() -> None:
    for job_name in ("static-checks", "python-types-and-tests"):
        job = _workflow("ci.yml")["jobs"][job_name]
        assert _draft_gated(job), f"{job_name} should skip draft pull requests"
        assert "github.event_name" in job["if"], f"{job_name} must still run on push"
