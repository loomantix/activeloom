"""Entry guards must survive rendering into every reviewer harness."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("root", [".agents", ".claude", ".codex"])
@pytest.mark.parametrize("skill", ["critique", "deepcritique", "refactorpass"])
def test_reviewer_preflight_precedes_other_gates(root: str, skill: str) -> None:
    text = (ROOT / root / "skills" / skill / "SKILL.md").read_text()
    guard = text.index("## Reviewer environment preflight")
    next_gate = text.index("## Step 0: Human-glance gate")
    assert guard < next_gate
    preflight = text[guard:next_gate]
    for required in (
        "git rev-parse HEAD",
        f"node {root}/skills/critique/scripts/review-ledger.js --version",
        f"{root}/skills/critique/scripts/review-ledger.version",
        "exact stdout/stderr",
        "Post nothing",
        "Do not simulate a ledger write",
        "create a\nworktree",
        "start a launcher",
        "including `transition-run`",
        "Never retry the\nsame transition",
    ):
        assert required in preflight


@pytest.mark.parametrize("root", [".agents", ".claude", ".codex"])
@pytest.mark.parametrize("skill", ["critique", "deepcritique", "refactorpass"])
def test_missing_ledger_arguments_allow_one_same_pass_correction(root: str, skill: str) -> None:
    text = (ROOT / root / "skills" / skill / "SKILL.md").read_text()
    preflight = text[text.index("## Reviewer environment preflight"):text.index("## Step 0:")]
    for required in (
        "missing-required-argument diagnostic",
        "subcommand's `usage:` synopsis",
        "Read that subcommand's `--help`",
        "local and live PR heads are unchanged since the failed invocation",
        "live PR head still equals any `--head` it named",
        "previously captured ledger evidence is unchanged",
        "one corrected invocation",
        "same run, round and budget",
        "stale-head, authorization, lifecycle or integrity refusal",
        "partial or uncertain write",
        "A second failure stops the pass",
    ):
        assert required in preflight
    assert preflight.index("missing-required-argument diagnostic") < preflight.index(
        "At any other later ledger or controller refusal"
    )


@pytest.mark.parametrize("root", [".agents", ".claude", ".codex"])
def test_session_mode_handoff_is_rendered_by_the_controller(root: str) -> None:
    """The user handoff is engine-neutral and never hand-written by the herder.

    The launch line, preflight and invocation themselves are covered by
    tests/test_user_pass_handoff.py against the controller that renders them.
    """
    reference = (ROOT / root / "references/herdr.md").read_text()
    skill = (ROOT / root / "skills/hreview/SKILL.md").read_text()
    assert "### Session mode" in reference and "### Human handoff" not in reference
    for text in (reference, skill):
        assert "user-handoff" in text
    assert "A bare CLI will not do" in reference
    assert "`git rev-parse HEAD`" in reference
    assert "`review-ledger.js --version`" in reference
    for line in ("claude --model", "codex -m", "agy --model"):
        assert line in reference
