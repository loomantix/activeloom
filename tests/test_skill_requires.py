"""scripts/skill-requires.txt must declare every cross-skill reference.

Installers install a skill's required closure and report what is missing from
it, so an undeclared dependency is a skill a user can end up without.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIRES = ROOT / "scripts/skill-requires.txt"
SKILL_ROOTS = (".claude/skills", ".codex/skills", ".agents/skills")
TEXT_SUFFIXES = {".md", ".py", ".sh", ".js", ".cjs", ".mjs", ".json", ".hbs", ".txt"}


def parse_requires(text: str) -> dict[str, tuple[set[str], set[str]]]:
    table: dict[str, tuple[set[str], set[str]]] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, rest = line.partition(":")
        required, _, optional = rest.partition("|")
        assert name.strip() not in table, f"{name} declared twice"
        table[name.strip()] = (set(required.split()), set(optional.split()))
    return table


def skill_names() -> set[str]:
    return {
        p.parent.name
        for root in SKILL_ROOTS
        for p in (ROOT / root).glob("*/SKILL.md")
    }


def skill_files() -> list[tuple[str, Path]]:
    """Every text file of every skill, in each rendered root and in prompts/."""
    out = []
    for root in SKILL_ROOTS + ("prompts/skills",):
        for skill_dir in sorted((ROOT / root).iterdir()):
            if not skill_dir.is_dir():
                continue
            for f in skill_dir.rglob("*"):
                if f.is_file() and f.suffix in TEXT_SUFFIXES and "__pycache__" not in f.parts:
                    out.append((skill_dir.name, f))
    return out


def references() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(script references, any reference) per skill.

    A reference is a path into another skill (`skills/<x>/`,
    `<<SKILLS_ROOT>>/<x>/`) or an invocation (`<<INVOKE>>x`, `` `/x ``). The
    Codex and Gemini roots name skills bare, so only their paths are scanned;
    their rendered sources are scanned through prompts/.
    """
    names = skill_names()
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    path = re.compile(rf"(?:skills|<<SKILLS_ROOT>>)/({alt})/(\S*)")
    invoke = re.compile(rf"(?:<<INVOKE>>|`/)({alt})(?![\w-])")
    scripts: dict[str, set[str]] = {}
    anyref: dict[str, set[str]] = {}
    for owner, f in skill_files():
        text = f.read_text(errors="ignore")
        for m in path.finditer(text):
            if m.group(1) == owner:
                continue
            anyref.setdefault(owner, set()).add(m.group(1))
            if m.group(2).startswith("scripts/"):
                scripts.setdefault(owner, set()).add(m.group(1))
        if ".claude/" in str(f) or "prompts/" in str(f):
            for m in invoke.finditer(text):
                if m.group(1) != owner:
                    anyref.setdefault(owner, set()).add(m.group(1))
    return scripts, anyref


@pytest.fixture(scope="module")
def table() -> dict[str, tuple[set[str], set[str]]]:
    return parse_requires(REQUIRES.read_text())


def test_names_exist(table: dict[str, tuple[set[str], set[str]]]) -> None:
    names = skill_names()
    unknown = {
        f"{skill} -> {dep}" if dep else skill
        for skill, (req, opt) in table.items()
        for dep in [None, *req, *opt]
        if (dep or skill) not in names
    }
    assert sorted(unknown) == []
    overlap = {s for s, (req, opt) in table.items() if req & opt or s in req | opt}
    assert sorted(overlap) == [], "a dependency is both required and optional, or a skill needs itself"


def test_script_references_are_required(table: dict[str, tuple[set[str], set[str]]]) -> None:
    scripts, _ = references()
    undeclared = sorted(
        f"{skill} runs {dep}'s scripts"
        for skill, deps in scripts.items()
        for dep in deps
        if dep not in table.get(skill, (set(), set()))[0]
    )
    assert undeclared == []


def test_every_reference_is_declared(table: dict[str, tuple[set[str], set[str]]]) -> None:
    _, anyref = references()
    undeclared = sorted(
        f"{skill} -> {dep}"
        for skill, deps in anyref.items()
        for dep in deps
        if dep not in set().union(*table.get(skill, (set(), set())))
    )
    assert undeclared == [], "add each to scripts/skill-requires.txt as required or optional"


def test_parse_requires_shape() -> None:
    parsed = parse_requires("# c\na: b c | d\ne: |\nf: | g\n")
    assert parsed == {"a": ({"b", "c"}, {"d"}), "e": (set(), set()), "f": (set(), {"g"})}
