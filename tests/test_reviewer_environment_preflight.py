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
@pytest.mark.parametrize("skill", ["critique", "deepcritique", "refactorpass"])
def test_disposition_head_typo_correction_requires_unchanged_published_evidence(root: str, skill: str) -> None:
    text = (ROOT / root / "skills" / skill / "SKILL.md").read_text()
    preflight = " ".join(text[text.index("## Reviewer environment preflight"):text.index("## Step 0:")].split())
    for required in (
        "Pass full SHAs directly from Git output; never retype or expand an abbreviated SHA",
        "only to `dispose`",
        "full SHA captured after this pass's successful push",
        "a pass that publishes nothing records its Git-derived reviewed head instead",
        "local HEAD, the pushed remote branch and the live PR head all equal that recorded SHA",
        "complete pre-call ledger snapshot",
        "unchanged after the refusal",
        "verify from the pinned implementation that the failed call stopped before any write",
        "change only `--head`",
        "Do not replace a previously valid head with a newer head",
        "Missing evidence, a real head move or a partial/uncertain write stops the pass",
        "Both exceptions share one correction attempt per pass",
        "never revive an already-blocked result",
    ):
        assert required in preflight


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
