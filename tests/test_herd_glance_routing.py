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
    assert "a clean auto-glance, or a `dependency-analysis:v1` comment at `verdict=clear` → [Shipping]" in horch
    assert "do not independently route it to auto-glance" in horch
    assert "prompt the same herder the `auto-glance` skill" not in horch
    hloop = skill("hloop")
    assert "`hreview` dispatches auto-glance itself" in hloop
    assert "**converged or clean auto-glance**, or a dependency analysis at `verdict=clear` → step 5" in hloop
    assert "do not launch another auto-glance or override the gate" in hloop
    assert "without starting a second glancer" in skill("hpromote")


@pytest.mark.parametrize("root", [".claude", ".codex", ".agents"])
def test_herd_skills_route_a_dependency_update_to_the_analysis(root: str) -> None:
    def skill(name: str) -> str:
        return " ".join((ROOT / root / "skills" / name / "SKILL.md").read_text().split())

    hreview = skill("hreview")
    assert "On `dependencyUpdate`, whatever the tier extensions or markers: start no chain, unless the PR carries the `review: full` label" in hreview
    assert "classify again with `--force-review` and continue as a mixed range" in hreview
    assert hreview.index("On `dependencyUpdate`") < hreview.index("On `skip` or `smallChange`")
    assert "run `" in hreview and "dep-analysis <n>` once the chain converges" in hreview
    glance = skill("auto-glance")
    assert glance.index("On `dependencyUpdate`, the PR gets a dependency analysis, not a glance") < glance.index(
        "Continue only if `skip` or `smallChange` is true"
    )
    horch = skill("horch")
    assert (
        "reports `dependencyUpdate`, and its latest `dependency-analysis:v1` comment says `verdict=clear` at that exact head"
        in horch
    )
    assert "**mixed range:**" in horch
    assert "Never route a dependency update to a review chain or auto-glance" in horch
    assert "Do not start a review chain on the dependency update" in skill("hloop")
