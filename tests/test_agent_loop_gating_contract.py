"""Under agent-loop the wrapper's validation hook is the review pass's gating run.

The critique skills and the ledger's "Validate before attesting" rule tell a
standalone engine to run the unfiltered gating suite before it attests. Inside
an agent-loop pass the wrapper runs its validation hook on the exact head after
the pass and owns the attestation, so the engine must not run the suite again:
the duplicate costs a suite per pass and was the command most often left
running when a one-shot pass ended early. These tests pin that exception on
every surface that carries the rule, and pin the standalone rule beside it.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LEDGER_DOCS = [ROOT / root / "references/local-review-ledger.md" for root in (".claude", ".codex", ".agents")]
CRITIQUE_SKILLS = [ROOT / root / "skills/critique/SKILL.md" for root in (".claude", ".codex")]


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_ledger_rule_names_the_wrapper_validation_as_the_agent_loop_gating_run() -> None:
    texts = [_flat(path) for path in LEDGER_DOCS]
    assert len(set(texts)) == 1, "the ledger reference must stay identical across prompt roots"
    text = texts[0]
    assert "Under agent-loop (`AGENT_LOOP_REVIEW_CONTRACT_VERSION` is set) the wrapper's validation hook is the gating run" in text
    assert "does not run the unfiltered suite itself" in text
    assert "Standalone and review-chain passes keep the rule above." in text
    # The standalone rule is unchanged.
    assert "verify a successful unfiltered run of the repository's required gating suite at the exact final head" in text


def test_both_critique_surfaces_skip_the_unfiltered_run_only_under_agent_loop() -> None:
    for path in CRITIQUE_SKILLS:
        text = _flat(path)
        assert "run the repository's gating suite unfiltered" in text, path
        assert "Under agent-loop (`$AGENT_LOOP_REVIEW_CONTRACT_VERSION` is set) skip this run" in text, path
        assert "validation for your fixes only" in text, path
        assert "write the result only after every command you started has finished" in text, path


def _template_text(root: str) -> str:
    template = (ROOT / root / "skills/agent-loop/agent-loop.config.template").read_text(encoding="utf-8")
    return " ".join(" ".join(line.lstrip("#") for line in template.splitlines()).split())


def test_config_template_does_not_ask_review_hooks_for_a_gating_run() -> None:
    text = _template_text(".claude")
    assert "Ask review hooks for focused checks on their own fixes only" in text
    assert "validation_hook is the gating suite" in text
    assert "critical tier" not in text


def test_every_config_template_makes_validation_hook_the_gating_run() -> None:
    # Every wrapper exports the contract version, so the ledger's agent-loop
    # exception applies to each root's consumers.
    for root in (".claude", ".codex", ".agents"):
        text = _template_text(root)
        assert "This is the gating run: use the repository's declared review gate" in text, root
        assert "Prefer targeted checks" not in text, root
