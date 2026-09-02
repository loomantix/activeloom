"""Policy tests for bounded pull-request Actions usage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent
ACCEPTED_FINDINGS = ROOT / ".github/codeql-accepted-findings.json"


def _workflow(name: str) -> dict[str, Any]:
    value = yaml.load(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(value, dict)
    return value


def test_ci_uses_draft_preflight_and_ready_full_gate() -> None:
    workflow = _workflow("ci.yml")
    assert workflow["on"]["pull_request"]["types"] == [
        "opened", "synchronize", "reopened", "ready_for_review", "converted_to_draft"
    ]
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
    preflight_text = preflight["steps"][1]["run"]
    assert "git diff --check" in preflight_text
    assert "python3 -m py_compile" in preflight_text
    assert "bash -n" in preflight_text
    assert "pnpm" not in preflight_text
    full = "github.event_name == 'push' || github.event.pull_request.draft == false"
    for job_name in ("static-checks", "python-types-and-tests"):
        assert jobs[job_name]["if"] == full
        assert jobs[job_name]["timeout-minutes"] == "20"


def test_codeql_cancels_superseded_runs_and_skips_drafts() -> None:
    workflow = _workflow("codeql.yml")
    assert workflow["on"]["pull_request"]["types"] == [
        "opened", "synchronize", "reopened", "ready_for_review", "converted_to_draft"
    ]
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


def test_codeql_gate_grades_against_the_accepted_findings_list() -> None:
    """The gate is zero-findings plus one auditable, self-expiring exception.

    This repo cannot upload SARIF to Code Scanning, so a finding here is a
    red build rather than a Security-tab entry. `.github/codeql-accepted-
    findings.json` is the only escape hatch: rule id + path, never a line
    number, so a refactor that moves the code does not churn the list. Every
    entry carries a written justification, and the gate fails on an entry
    that matches nothing — an exception outliving the code it excuses is how
    a gate goes quietly blind.
    """
    steps = _workflow("codeql.yml")["jobs"]["analyze"]["steps"]
    gate = next(s for s in steps if s.get("name") == "Gate on CodeQL findings")
    assert gate["env"]["ACCEPTED"] == str(
        ACCEPTED_FINDINGS.relative_to(ROOT)
    )
    # Both halves of the verdict are enforced, not merely computed.
    assert ".unexpected | length" in gate["run"]
    assert ".stale | length" in gate["run"]

    accepted = json.loads(ACCEPTED_FINDINGS.read_text(encoding="utf-8"))
    assert isinstance(accepted, list)
    keys = [(entry["rule"], entry["path"]) for entry in accepted]
    assert len(keys) == len(set(keys)), "duplicate accepted-finding entries"
    for entry in accepted:
        assert set(entry) == {"rule", "path", "why"}, entry
        assert entry["rule"].startswith("py/"), entry
        assert (ROOT / entry["path"]).is_file(), entry
        # A bare "known issue" is not a justification anyone can re-review.
        assert len(entry["why"].split()) >= 20, entry


def test_dco_cancels_superseded_heads_and_is_bounded() -> None:
    workflow = _workflow("dco.yml")
    assert workflow["on"]["pull_request"]["types"] == [
        "opened", "synchronize", "reopened", "ready_for_review", "converted_to_draft"
    ]
    assert workflow["concurrency"] == {
        "group": "dco-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }
    assert workflow["jobs"]["dco-check"]["timeout-minutes"] == "5"
