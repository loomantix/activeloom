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
    assert "review-profile.py launch-args" in text or "launch-args --engine" in text
    assert "user-args" in text


def test_codex_boundary_permits_herd_interactive_claude_panes() -> None:
    boundary = _boundary()
    assert "sole exception" not in boundary
    assert "herd chain's interactive Claude reviewer pane" in boundary
    assert "herdr agent start" in boundary
    assert "review-profile.py launch-args" in boundary
    assert "herd-config.py user-args" in boundary
    assert "Never hand-compose" in boundary


def test_codex_boundary_keeps_launcher_outside_herd_chains() -> None:
    boundary = _boundary()
    assert "Outside a herd chain" in boundary
    assert re.search(r"Outside a herd chain,[^.]*uses `run-claude-review\.sh` only", boundary)
    # The unconditional rule that would forbid hreview's interactive pane is gone.
    assert "Interactive review and every non-agent-loop caller continue to use" not in boundary
