"""Herd dispatch templates keep Claude/agy skill invocations single-line.

Claude and agy parse a slash command only when it arrives as typed input. A
prompt that carries a brief after the command arrives as pasted text, and a
skill that only the user may invoke refuses to start. The herd skills must send
the bare `/<skill> <args>` alone and deliver any brief as a separate prompt.
"""
import re
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

HERD_SKILLS = ("hgrill", "horch", "hloop", "hpromote", "hreview", "hred-team", "auto-glance")
ROOTS = ("prompts", ".claude", ".agents", ".codex")
# Templates whose pass prompt carries a brief alongside a skill invocation.
BRIEF_TEMPLATES = ("hgrill", "hreview", "hred-team", "horch")
REFUSAL = "Claude couldn't run /<skill>. It runs only when you type it at the prompt."

# The indent excludes newlines so each opening fence pairs with its own close.
FENCE = re.compile(r"^([ \t]*)```[^\n]*\n(.*?)^\1```[ \t]*$", re.M | re.S)


def _herd_sources() -> list[Path]:
    paths = []
    for root in ROOTS:
        base = REPO_ROOT / root
        paths.append(base / "references/herdr.md")
        paths.extend(base / "skills" / s / "SKILL.md" for s in HERD_SKILLS)
    return paths


def _engines_row(text: str, engine: str) -> list[str]:
    section = text.split("## Engines\n", 1)[1].split("\n## ", 1)[0]
    row = next(line for line in section.splitlines() if line.startswith(f"| `{engine}`"))
    return [cell.strip() for cell in row.strip("|").split("|")]


@pytest.mark.parametrize("path", _herd_sources(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_fenced_prompt_appends_a_brief_to_a_slash_command(path: Path) -> None:
    for match in FENCE.finditer(path.read_text()):
        lines = [ln.strip() for ln in match.group(2).splitlines() if ln.strip()]
        if lines and re.match(r"^(?:/|<<INVOKE>>)[a-z<]", lines[0]):
            assert len(lines) == 1, f"{path}: slash command followed by a brief:\n{match.group(2)}"


@pytest.mark.parametrize("root", ROOTS)
def test_herdr_reference_states_the_invocation_contract(root: str) -> None:
    text = (REPO_ROOT / root / "references/herdr.md").read_text()
    for engine in ("claude", "gemini"):
        invoke = _engines_row(text, engine)[4]
        assert invoke == "`/<skill> <args>`"
    assert "is the whole prompt: one short line" in text
    assert "Codex takes its invocation and brief in one prompt." in text
    assert "Send it first as its own plain prompt" in text
    assert "Do not act on it yet" in text
    assert "reply to this brief must be exactly `BRIEF-READY`" in text
    assert "Any other reply is a block." in text
    assert "Then send `/<skill> <args>` alone." in text
    assert REFUSAL in text
    assert "on any `idle`/`done` read before sending a continue or finish nudge" in text
    assert "resend the bare `/<skill> <args>` once" in text
    assert "fresh refusal following the retry" in text
    assert "A second refusal is a block; never record it as a failed pass." in text
    assert "the recovery line joins the brief, never the slash-command line" in text


@pytest.mark.parametrize("root", ROOTS)
@pytest.mark.parametrize("skill", BRIEF_TEMPLATES)
def test_brief_templates_deliver_the_brief_separately(root: str, skill: str) -> None:
    text = (REPO_ROOT / root / "skills" / skill / "SKILL.md").read_text()
    assert "herdr.md's Prompting gives" in text
    assert "then the bare" in text
    for stale in (
        "First line: the `<kind>` skill",
        "Text: the skill line, then",
        "addressed the way its engine expects, then:",
        "prompt its `skill` with the issue, the `brief` to read first",
    ):
        assert stale not in text


@pytest.mark.parametrize("root", ROOTS)
def test_secondary_brief_paths_use_the_brief_protocol(root: str) -> None:
    hgrill = (REPO_ROOT / root / "skills/hgrill/SKILL.md").read_text()
    assert "with this appended to the brief:" in hgrill

    hreview = (REPO_ROOT / root / "skills/hreview/SKILL.md").read_text()
    assert "(`<n>`, plus `silent` for a silent PR)" in hreview
    assert "pass `silent` to the skill" not in hreview
    assert "Send each prompt with `herdr agent prompt" in hreview
    assert "Its brief carries one extra line naming the earlier blocked result" in hreview

    horch = (REPO_ROOT / root / "skills/horch/SKILL.md").read_text()
    assert "delivered through herdr.md's Prompting brief protocol, never on the slash-command line." in horch


@pytest.mark.parametrize("root", ROOTS)
def test_promotion_resume_preserves_dry_run(root: str) -> None:
    text = (REPO_ROOT / root / "skills/hpromote/SKILL.md").read_text()
    blocks = text.split("## Blocks\n", 1)[1]
    assert "`run --dry-run` for a dry run" in blocks
    assert "`merged` for Post-merge" in blocks
    assert "`deployed` for Post-deploy" in blocks
    assert "Supply the requested phase and context as the brief for that invocation" in blocks
