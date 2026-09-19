"""Every review entry skill runs the review-profile preflight before it launches.

The preflight is the only path from a missing profile setting to guided setup in
the same conversation, so each entry skill in each root must carry it, point at
its own root's helper, honour the non-interactive marker, and hand off to the
review-setup skill's inline flow.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
HARNESS_ROOTS = (".claude", ".codex", ".agents")
ENTRY_SKILLS = ("agent-loop", "critique", "deepcritique", "pr-critique", "reviewit")
HEADING = "## Review profile preflight"

ENTRY_POINTS = [
    (root, skill)
    for root in HARNESS_ROOTS
    for skill in ENTRY_SKILLS
    if (ROOT / root / "skills" / skill / "SKILL.md").is_file()
]


def _section(body: str, heading: str) -> str:
    return " ".join(body.split(heading, 1)[1].split("\n## ", 1)[0].split())


def test_every_named_entry_skill_is_covered() -> None:
    # pr-critique is the only entry skill a root may lack (the Claude root).
    assert len(ENTRY_POINTS) == 14


@pytest.mark.parametrize("root,skill", ENTRY_POINTS, ids=lambda v: str(v))
def test_entry_skill_runs_the_preflight(root: str, skill: str) -> None:
    body = (ROOT / root / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert HEADING in body, f"{root}/{skill} has no review profile preflight"
    section = _section(body, HEADING)
    helper = f"{root}/skills/review-setup/scripts/review-profile.py check"
    assert helper in section
    assert (ROOT / root / "skills/review-setup/scripts/review-profile.py").is_file()
    assert (
        "`AGENT_LOOP_NONINTERACTIVE=1` or `AGENT_LOOP_REVIEW_ENGINE` is set" in section
    )
    assert 'review-setup` "Inline setup"' in section
    # A schema-1 profile lacks only worker keys, which no review run reads;
    # storing them relabels the shared profile as schema 2.
    assert (
        'Exit 3 with `"configured": true` and only `ENGINE.worker.*` keys in '
        "`missing` also means continue" in section
    )


# Every path that starts a one-pass reviewer; the preflight skips on the
# variable they export, so an unattended pass never reaches interactive setup.
LAUNCHERS = (
    ".codex/skills/critique/scripts/run-claude-review.sh",
    ".codex/skills/critique/scripts/run-codex-review.py",
    ".codex/skills/critique/scripts/review-chain-runner.py",
    ".claude/skills/critique/scripts/run-agy-review.sh",
    ".codex/skills/critique/scripts/run-agy-review.sh",
    ".codex/skills/agent-loop/scripts/agent-loop.sh",
    ".agents/skills/agent-loop/scripts/agent-loop.sh",
)


@pytest.mark.parametrize("path", LAUNCHERS)
def test_launchers_export_the_preflight_skip_variable(path: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert re.search(r"AGENT_LOOP_REVIEW_ENGINE[\"']?\s*[=:]", source), path


@pytest.mark.parametrize("root,skill", ENTRY_POINTS, ids=lambda v: str(v))
def test_preflight_follows_the_human_glance_gate(root: str, skill: str) -> None:
    body = (ROOT / root / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## .*$", body, re.M)
    expected = 0 if skill == "agent-loop" else 1
    assert sections.index(HEADING) == expected


@pytest.mark.parametrize("root", HARNESS_ROOTS)
def test_review_setup_defines_the_inline_flow(root: str) -> None:
    body = (ROOT / root / "skills/review-setup/SKILL.md").read_text(encoding="utf-8")
    section = _section(body, "## Inline setup")
    for phrase in (
        "Run `show`, `detect`, and `defaults`",
        "`missing`",
        "`suggested`",
        "availability=unavailable",
        "`set`",
    ):
        assert phrase in section
    assert "`check` exits 0" in section
