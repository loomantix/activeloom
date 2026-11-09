"""Rendered herd skills keep one owner for the opt-in glance dispatch."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("root", [".claude", ".codex", ".agents"])
def test_hreview_returns_on_zero_files_before_any_glance_or_chain(root: str) -> None:
    body = " ".join((ROOT / root / "skills/hreview/SKILL.md").read_text().split())
    gate = body.split("6. Tier extensions first:", 1)[1].split("7. Settings", 1)[0]
    zero, nonempty = gate.split("Otherwise, for a nonempty diff or an empty one whose lifecycle is not `none`:", 1)
    for requirement in (
        "**Verified zero-file comparison**",
        "common PR-wide `run-state` inventory",
        "Only `none` permits this route",
        "active, foreign, ended and transitioned runs retain step 9's lifecycle restrictions",
        "Failed or incomplete classification, Git or remote reads block",
        "`skip` alone is insufficient",
        "An empty merge-base diff with lifecycle `none` that does not verify",
        "blocks this PR's path: notify the block and start no chain or auto-glance",
        "return for this PR without a chain or auto-glance",
        "bootstrap, roster, review artifacts or ready flip",
        "Continue independent/companion ordering",
    ):
        assert requirement in zero, requirement
    assert "On `skip` or `smallChange`" in nonempty
    assert "stop that PR for a human glance" in nonempty
    assert "For a verified zero-file comparison, report no AI content review needed" in body


@pytest.mark.parametrize("root", [".claude", ".codex", ".agents"])
def test_horch_checks_before_recommending_and_ships_without_ai_attestation(root: str) -> None:
    body = " ".join((ROOT / root / "skills/horch/SKILL.md").read_text().split())
    route, fallback = body.split("**Review chain**", 1)[1].split(
        "For a nonempty diff, or an empty one whose lifecycle is not `none`, start the herder", 1
    )
    assert "prompt it the `hreview` skill" in fallback.split("Before starting one", 1)[0]
    settled = "act on a PR only when it is open, its author session has finished, and its head is not about to move"
    assert route.index(settled) < route.index("**Verified zero-file comparison**")
    for requirement in (
        "before recommending or launching a herder",
        "**Verified zero-file comparison**",
        "fresh local and remote head/base evidence",
        "only `none` permits this route",
        "every existing run retains the lifecycle restrictions",
        "go to [Shipping](#shipping) within existing authority",
        "launch neither a chain nor auto-glance",
        "Nonempty docs/config changes keep their current routing",
        "An empty diff with lifecycle `none` that does not verify blocks this PR",
        "launch, recommend or ship nothing",
    ):
        assert requirement in route, requirement
    shipping = body.split("## Shipping", 1)[1].split("## ", 1)[0]
    zero = shipping.split("**zero-file alternative:**", 1)[1].split("**otherwise either**", 1)[0]
    for requirement in (
        "fresh common PR-wide lifecycle is `none`",
        "requires no AI attestation, coverage, glance result or dependency analysis",
        "Retain every CI, mergeability, tests, authority and instruction-file gate",
        "fresh head/base checks before and after",
        "drift restores draft status and blocks shipment",
        "complete zero-file verification immediately before merge, after any wait",
        "requiring the same head and base",
        "tree preservation evidence before and after merge, for an ancestry-only repair its intended ancestry evidence",
        "the workflow's introduced-commit check",
        "No existing run may be ended, restarted, replaced or rewritten",
    ):
        assert requirement in zero, requirement
    issues = shipping.split("3. **Linked issues**", 1)[1].split("4. **After the ship:**", 1)[0]
    assert issues.index("after a zero-file ship, leave linked issues unchanged") < issues.index("`close` →")
    assert "never for `ship-staging` guardrails or repository merge authority" in shipping
    assert "With no herder for a zero-file comparison" in shipping


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
    assert "On `dependencyUpdate`, resolve the workflow's durable dependency review override first" in hreview
    assert "A matching extension takes precedence and selects Deep under trigger 6" in hreview
    assert "whatever the tier extensions" not in hreview
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
