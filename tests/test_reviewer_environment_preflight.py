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
        "local and live PR heads still equal the pass head",
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
def test_human_handoff_has_full_agy_launch_and_preflight(root: str) -> None:
    reference = (ROOT / root / "references/herdr.md").read_text()
    skill = (ROOT / root / "skills/hreview/SKILL.md").read_text()
    for text in (reference, skill):
        assert "agy --model <pinned-model> --effort <pinned-effort> --dangerously-skip-permissions" in text
        assert "bare `agy` will not work" in text
    assert "git rev-parse HEAD" in reference
    assert "node .agents/skills/critique/scripts/review-ledger.js --version" in reference
