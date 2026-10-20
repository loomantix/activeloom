"""Rendered herd skills keep one owner for the opt-in glance dispatch."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("root", [".claude", ".codex", ".agents"])
def test_hreview_dispatches_only_at_the_eligible_opted_in_gate(root: str) -> None:
    body = (ROOT / root / "skills/hreview/SKILL.md").read_text()
    gate = body.split("6. Tier extensions first:", 1)[1].split("7. Settings", 1)[0]
    assert gate.index("always-Deep") < gate.index("On `skip` or `smallChange`")
    for exception in ("tier marker", "local-review-run:v1", "explicitly asked"):
        assert exception in gate
    assert "classify-changeset --base <merge-base sha> --head <head>" in gate
    disabled, enabled = gate.split("**`glance.auto` is `true`**", 1)
    assert "**`glance.auto` is `false`**" in disabled
    assert "stop that PR for a human glance" in disabled
    for requirement in (
        "herd-config.py repo",
        "auto-glance <n> --author <author>",
        "`--author` wins over commit trailers",
        "missing or ambiguous authorship blocks",
        "yourself in this herder session",
        "wait for its result",
        "without creating a chain run or roster",
        "independent/companion ordering",
        "A declined or blocked glance is not permission to start a chain",
    ):
        assert requirement in gate
    assert "ready flip" in enabled
    hard_stops = body.split("## Hard stops", 1)[1].split("## Paths", 1)[0]
    assert "auto-glance`'s clean-result and CI gate" in hard_stops
    assert "never merge" in hard_stops
    assert "instead of a run ID or rounds" in body


@pytest.mark.parametrize("root", [".claude", ".codex", ".agents"])
def test_orchestrators_observe_the_glance_instead_of_dispatching_twice(root: str) -> None:
    def skill(name: str) -> str:
        return (ROOT / root / "skills" / name / "SKILL.md").read_text()

    horch = skill("horch")
    assert "Do not launch a second auto-glance" in horch
    assert "clean auto-glance → [Shipping]" in horch
    assert "do not independently route it to auto-glance" in horch
    assert "prompt the same herder the `auto-glance` skill" not in horch
    hloop = skill("hloop")
    assert "`hreview` dispatches auto-glance itself" in hloop
    assert "**converged or clean auto-glance** → step 5" in hloop
    assert "do not launch another auto-glance or override the gate" in hloop
    assert "without starting a second glancer" in skill("hpromote")
