"""The human-glance outcome: gated before the chain, labelled on the PR.

A docs/config-only range must leave every review entry point before that entry
point can produce a side effect — a draft PR, a ledger result, an attestation, a
tier or refactor marker, or a telemetry record. These tests hold the prompt
surface and the label workflow to that ordering, because the ordering is the
whole feature: a gate that runs after the PR is opened has already spent what it
was meant to save.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]

GATE_HEADING = "## Step 0: Human-glance gate"

# Every entry point named by the workflow's "Human glance" section, per root.
ENTRY_POINTS = [
    (".claude", "critique"),
    (".claude", "deepcritique"),
    (".claude", "refactorpass"),
    (".claude", "reviewit"),
    (".claude", "codex-review"),
    (".codex", "critique"),
    (".codex", "deepcritique"),
    (".codex", "pr-critique"),
    (".codex", "refactorpass"),
    (".codex", "reviewit"),
    (".agents", "critique"),
    (".agents", "deepcritique"),
    (".agents", "pr-critique"),
    (".agents", "refactorpass"),
    (".agents", "reviewit"),
]

HARNESS_ROOTS = [".claude", ".codex", ".agents"]

# Side effects the gate must precede. Each is named in the gate's own text, so
# a rewrite that quietly drops one from the ordering fails here.
GATED_SIDE_EFFECTS = (
    "draft PR",
    "ledger result",
    "attestation",
    "tier or refactor marker",
    "telemetry record",
)


def _skill(root: str, skill: str) -> str:
    return (ROOT / root / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("root,skill", ENTRY_POINTS, ids=lambda v: str(v))
def test_the_gate_is_the_first_section_of_every_entry_point(root: str, skill: str) -> None:
    body = _skill(root, skill)
    first = re.search(r"^## .*$", body, re.M)
    assert first is not None, f"{root}/{skill} has no sections"
    assert first.group(0) == GATE_HEADING, (
        f"{root}/{skill} opens with {first.group(0)!r}; the human-glance gate must run "
        "before every other step"
    )


@pytest.mark.parametrize("root,skill", ENTRY_POINTS, ids=lambda v: str(v))
def test_the_gate_stops_before_each_side_effect(root: str, skill: str) -> None:
    body = _skill(root, skill)
    gate = " ".join(body.split(GATE_HEADING, 1)[1].split("\n## ", 1)[0].split())
    for effect in GATED_SIDE_EFFECTS:
        assert effect in gate, f"{root}/{skill} gate does not name {effect!r}"
    assert "Human glance" in gate


@pytest.mark.parametrize("root,skill", ENTRY_POINTS, ids=lambda v: str(v))
def test_no_entry_point_keeps_an_in_chain_skip(root: str, skill: str) -> None:
    """The in-chain skip is unreachable now; leaving it invites a second answer."""
    body = _skill(root, skill)
    assert "docs/config-only skip" not in body
    assert "Skip docs/config-only" not in body
    assert "`skipped`" not in body, (
        f"{root}/{skill} still routes a changeset to a skipped telemetry record"
    )


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_the_workflow_defines_human_glance_ahead_of_the_tier(root: str) -> None:
    body = (ROOT / root / "REVIEW_WORKFLOW.md").read_text(encoding="utf-8")
    assert "### Human glance" in body
    assert body.index("### Human glance") < body.index("### What sets the tier")
    assert "classify-changeset" in body
    assert (
        "Human glance: N docs/config files, no review-significant changes — "
        "read the diff and merge. No review chain run." in body
    )
    # The override and the controller carve-out are the only two ways past it.
    assert "trigger 6" in body.split("### Human glance", 1)[1]
    assert "AGENT_LOOP_REVIEW_RESULT_FILE" in body.split("### Human glance", 1)[1]


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_invoking_a_review_is_not_trigger_6(root: str) -> None:
    body = " ".join((ROOT / root / "REVIEW_WORKFLOW.md").read_text(encoding="utf-8").split())
    gate = body.split("### Human glance", 1)[1].split("### What sets the tier", 1)[0]
    assert 'or asking to "review this PR" or "run the review chain", is not that request' in gate
    # A session never argues its way past "skip": false; the classifier rule is fixed instead.
    assert 'A session does not overrule `"skip": false`' in gate
    assert "without asking for Deep is not this trigger" in body
    assert "so it adds no engines, repeats no steps, and does not select the Deep order" in body


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_no_workflow_still_emits_a_skipped_pass(root: str) -> None:
    body = (ROOT / root / "REVIEW_WORKFLOW.md").read_text(encoding="utf-8")
    assert "Docs/config-only skip" not in body
    assert "## Skip Path" not in body


def test_the_protocol_names_human_glance_as_the_first_step() -> None:
    body = (ROOT / "packages/review-ledger/protocol/local-review-ledger.md").read_text(
        encoding="utf-8"
    )
    assert "**human glance**" in body
    assert "Every cleanup and adversarial lane skips docs/config-only" not in body
    # The record writer stays backward compatible for stored records.
    assert "still accepts `status=skipped`" in body


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_agent_loop_classifies_before_its_first_review_round(root: str) -> None:
    script = (ROOT / root / "skills/agent-loop/scripts/agent-loop.sh").read_text(
        encoding="utf-8"
    )
    gate = script.index("if human_glance_gate ")
    assert gate < script.index("\n    run_review_convergence 1"), (
        f"{root} agent-loop runs a review round before classifying the range"
    )
    assert script.index("open_draft_pr \"$SELECTED_ID\"") < gate, (
        f"{root} agent-loop must leave the human a draft PR to read"
    )
    # An unreadable classification reviews rather than silently merging.
    assert "classify-changeset" in script
    assert "HUMAN_GLANCE_FILES" in script


# --- the synced label workflow ----------------------------------------------

WORKFLOW_PATH = ROOT / ".github/workflows/review-glance-label.yml"


def _label_workflow() -> dict[str, Any]:
    value = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def _label_steps() -> list[dict[str, Any]]:
    steps = _label_workflow()["jobs"]["classify"]["steps"]
    assert isinstance(steps, list)
    return steps


def _label_script() -> str:
    script = next(step["run"] for step in _label_steps() if "run" in step)
    assert isinstance(script, str)
    return script


def test_label_workflow_runs_on_pull_request_target_events() -> None:
    triggers = _label_workflow()["on"]
    assert set(triggers) == {"pull_request_target"}
    assert triggers["pull_request_target"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
    ]


def test_label_workflow_holds_only_the_permissions_it_uses() -> None:
    assert _label_workflow()["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
    }


def test_label_workflow_checks_out_the_base_and_never_the_head() -> None:
    checkout = next(
        step for step in _label_steps() if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    body = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "pull_request.head" not in body, (
        "a pull_request_target job holding a write token must never resolve the PR head ref"
    )


def test_label_workflow_pins_every_action_to_a_sha() -> None:
    # Read the file, not the parsed step: the version tag lives in a comment.
    uses_lines = [
        line.strip()
        for line in WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
        if "uses:" in line
    ]
    assert uses_lines
    for line in uses_lines:
        assert re.search(r"@[0-9a-f]{40} # \S", line), f"{line} is not SHA-pinned with its version"


def test_label_workflow_interpolates_no_pull_request_authored_text() -> None:
    """Expressions may reach the script only through env, and only trusted ones."""
    script = _label_script()
    assert "${{" not in script
    env = next(step for step in _label_steps() if "run" in step)["env"]
    for value in env.values():
        if "${{" not in value:
            continue
        assert re.fullmatch(
            r"\$\{\{ (secrets\.GITHUB_TOKEN|github\.repository|github\.event\.pull_request\.number) \}\}",
            value,
        ), f"{value} is not a trusted expression"


def test_label_workflow_probes_every_harness_classifier() -> None:
    script = _label_script()
    for root in HARNESS_ROOTS:
        assert f"{root}/skills/critique/scripts/review-ledger.js" in script


def test_label_workflow_fails_closed_on_every_unclassified_path() -> None:
    """A missing classifier, a failed diff fetch, and a failed run all unlabel."""
    script = _label_script()
    blocks = [
        block
        for block in script.split("\n\n")
        if "::notice::" in block and "exit 0" in block
    ]
    assert len(blocks) == 3, "expected the three fail-closed branches"
    for block in blocks:
        assert "remove_label" in block
        assert "add_label" not in block
    assert 'if [ "$skip" = true ] && [ "$files" -gt 0 ]; then' in script


def test_label_workflow_is_synced_to_consumers_with_an_opt_out() -> None:
    manifest = yaml.safe_load((ROOT / "scripts/sync-targets.yml").read_text(encoding="utf-8"))
    destinations = {target["destination"] for target in manifest["shared"]["targets"]}
    assert ".github/workflows/review-glance-label.yml" in destinations
