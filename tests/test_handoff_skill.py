"""A handoff written in one harness must resume in another, so the comment
format is identical in every rendered copy of the skill."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOTS = (".claude", ".codex", ".agents")
MARKER = "<!-- handoff:v1 from=<short-sha or \"session\"> date=<YYYY-MM-DD> -->"
SELECTOR = 'select(.body | contains("<!-- handoff:v1"))'
SECTIONS = (
    "State",
    "Decisions to make before writing code",
    "Sequencing",
    "Traps",
    "Validation contract",
    "Do not",
)


def skill(root: str) -> str:
    return (REPO_ROOT / root / "skills" / "handoff" / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("root", ROOTS)
def test_marker_and_resume_selector_agree(root: str) -> None:
    text = skill(root)
    assert MARKER in text
    assert SELECTOR in text


@pytest.mark.parametrize("root", ROOTS)
def test_section_headings_are_fixed_and_ordered(root: str) -> None:
    found = re.findall(r"^\d+\. \*\*(.+?)\*\* —", skill(root), flags=re.MULTILINE)
    assert tuple(found) == SECTIONS


def test_comment_format_is_identical_across_harnesses() -> None:
    def format_block(text: str) -> str:
        start = text.index("### 3. Contents")
        end = text.index("### 5. Print the resume prompt")
        return text[start:end]

    blocks = {root: format_block(skill(root)) for root in ROOTS}
    assert len(set(blocks.values())) == 1, sorted(blocks)
