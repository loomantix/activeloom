"""The Codex renders of hreview and the review workflow agree on Claude transport.

`hreview` runs every engine, Claude included, as an interactive herdr pane and
excludes the headless launchers. The Codex workflow's Claude invocation boundary
must therefore carve out herd-launched interactive panes, or a Codex herder that
follows both documents stops before round 1.
"""
import re

from tests.conftest import REPO_ROOT

HREVIEW = REPO_ROOT / ".codex/skills/hreview/SKILL.md"
WORKFLOW = REPO_ROOT / ".codex/REVIEW_WORKFLOW.md"


def _boundary() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"^### Claude invocation boundary\n(.*?)^### ", text, re.M | re.S)
    assert match, "Codex workflow lost its Claude invocation boundary section"
    return " ".join(match.group(1).split())


def test_hreview_codex_render_requires_interactive_panes() -> None:
    text = " ".join(HREVIEW.read_text(encoding="utf-8").split())
    assert "runs as an interactive session" in text
    assert "not the headless launchers" in text
    assert "$P launch-args --engine" in text
    assert "$C user-args --engine" in text
    assert "$H user-handoff" in text


def test_codex_boundary_permits_herd_interactive_claude_panes() -> None:
    boundary = _boundary()
    assert "sole exception" not in boundary
    assert "herd chain's interactive Claude reviewer pane" in boundary
    assert "herdr agent start" in boundary
    assert "local-review-handoff.py user-handoff" in boundary
    assert "review-profile.py launch-args" in boundary
    assert "herd-config.py user-args" in boundary
    assert "Never add an option of your own" in boundary


def test_codex_boundary_keeps_its_prohibitions_and_two_exceptions() -> None:
    boundary = _boundary()
    assert "There are exactly two exceptions." in boundary
    assert "Third," not in boundary
    assert "Never invoke the raw `claude` CLI directly" in boundary
    assert "Do not set `CLAUDE_REVIEW_CLI` outside launcher tests." in boundary
    assert "--effort low" not in boundary


def test_codex_boundary_preserves_agent_loop_hook_settings() -> None:
    boundary = _boundary()
    assert (
        "`.codex/skills/agent-loop/scripts/run-codex-review.sh`, which starts Claude "
        "with the run's pinned model and effort"
    ) in boundary


def test_codex_boundary_keeps_launcher_outside_both_exceptions() -> None:
    boundary = _boundary()
    assert boundary.endswith(
        "Outside the contract-v4 `agent-loop` hook and the herd-launched interactive "
        "reviewer session, use `run-claude-review.sh` for every Claude review."
    )
