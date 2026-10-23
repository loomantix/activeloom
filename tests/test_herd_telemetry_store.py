"""Herd panes take the review metrics destination from the repository's herd config.

The value comes from `.activeloom-herd.json` on the integration branch, never
from an engine harness's own config, and reaches the ledger as an environment
variable on the pane. The bundles themselves stay free of a gate file, so a
bundle used outside a herd pane resolves exactly as before.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "prompts/skills/hreview/scripts/herd-config.py"
STORE = "LOOM_REVIEW_TELEMETRY_STORE"
BUNDLES = (".agents", ".claude", ".codex")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_config(work: Path, branch: str, config: dict[str, Any] | None) -> None:
    git(work, "checkout", "-q", "-B", branch)
    target = work / ".activeloom-herd.json"
    if config is None:
        target.unlink(missing_ok=True)
    else:
        target.write_text(json.dumps(config))
    (work / "touch").write_text(branch)
    git(work, "add", "-A")
    git(
        work,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        branch,
    )
    git(work, "push", "-q", "-f", "origin", branch)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    origin, checkout = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(checkout))
    git(checkout, "remote", "add", "origin", str(origin))
    return checkout


def pane_env(work: Path) -> list[str]:
    result = subprocess.run(
        ["python3", "-I", str(HELPER), "pane-env", "--repo-dir", str(work)],
        capture_output=True,
        text=True,
        check=True,
    )
    entries: list[str] = json.loads(result.stdout)
    return entries


def resolved_store(bundle: str, entries: list[str]) -> str:
    """The store this bundle's gate reader resolves in a pane created with `entries`."""
    env = {k: v for k, v in os.environ.items() if k != STORE}
    for entry in entries:
        key, value = entry.split("=", 1)
        env[key] = value
    gates = ROOT / bundle / "skills/critique/scripts/review-telemetry-gates.js"
    script = f"import({json.dumps(gates.as_uri())}).then(m => process.stdout.write(m.resolveGates().store))"
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_absent_setting_adds_nothing_to_a_pane(work: Path) -> None:
    commit_config(work, "main", {"schema_version": 1})
    assert pane_env(work) == []


@pytest.mark.parametrize("store", ["pr", "issue"])
def test_setting_becomes_the_pane_variable(work: Path, store: str) -> None:
    commit_config(work, "main", {"schema_version": 1, "telemetry": {"store": store}})
    assert pane_env(work) == [f"{STORE}={store}"]


def test_pull_request_head_cannot_choose_the_store(work: Path) -> None:
    commit_config(work, "main", {"schema_version": 1})
    commit_config(
        work, "feature", {"schema_version": 1, "telemetry": {"store": "issue"}}
    )
    assert pane_env(work) == []


@pytest.mark.parametrize("bundle", BUNDLES)
def test_herd_pane_routes_every_bundle_to_the_issue_store(
    work: Path, bundle: str
) -> None:
    # `.agents` is the bundle Gemini passes load, from the pinned surface clone.
    commit_config(work, "main", {"schema_version": 1, "telemetry": {"store": "issue"}})
    assert resolved_store(bundle, pane_env(work)) == "issue"


@pytest.mark.parametrize("bundle", BUNDLES)
def test_bundle_outside_a_herd_pane_still_resolves_to_pr(bundle: str) -> None:
    # Personal installs and the shared Agy surface link these directories; no
    # gate file beside them means they keep the default wherever else they run.
    assert not (
        ROOT / bundle / "skills/critique/scripts/review-telemetry.json"
    ).exists()
    assert resolved_store(bundle, []) == "pr"


@pytest.mark.parametrize("root", ["prompts", *BUNDLES])
def test_pane_rule_covers_every_engine(root: str) -> None:
    reference = (ROOT / root / "references/herdr.md").read_text()
    panes = reference.split("## Tabs and panes", 1)[1].split("\n## ", 1)[0]
    assert (
        "Every new tab or split for a session, of any engine, also takes `--env <entry>`"
        in panes
    )
    assert "`pane-env`" in panes
