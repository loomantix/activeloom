"""Frontmatter invariants for every `.claude/skills/*/SKILL.md`.

Three things these guard, in increasing order of how expensive the
failure is:

1. The frontmatter parses as YAML at all. `argument-hint` values are
   full of `[`, `|`, and `<...>`, and an unquoted value starting with
   `[` is read as a flow sequence rather than a string — which is how
   `pr-stats` shipped with unparseable frontmatter that nothing caught.
2. `name` matches the directory, so `/name` resolves to the skill a
   reader expects to find at that path.
3. **A skill that another skill invokes via the `Skill()` tool is not
   user-invoked.** `disable-model-invocation: true` removes a skill from
   the model's reach; whether a user-invoked skill stays reachable from
   another skill's body is undocumented, so the chain steps
   (`/refactorpass`, `/critique`, `/deepcritique`) have to stay model-invoked.
   Flipping one is a silent break — the chain simply stops running its
   tail, with no error — so it is worth a test rather than a comment.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

# `Skill(skill="foo")` / `Skill(skill='foo')` — a programmatic invocation
# of one skill from another skill's body. Prose mentions of `/foo` are
# deliberately not matched: they instruct the human, not the model, so
# they survive a skill becoming user-invoked.
SKILL_TOOL_CALL = re.compile(r"""Skill\(\s*skill\s*=\s*["']([a-z0-9-]+)["']""")

SKILL_FILES = sorted(SKILLS_DIR.glob("*/SKILL.md"))


def _split_frontmatter(path: Path) -> tuple[str, str]:
    text = path.read_text()
    if not text.startswith("---\n"):
        raise AssertionError(f"{path} does not open with a `---` frontmatter fence")
    try:
        end = text.index("\n---\n", 3)
    except ValueError as exc:  # pragma: no cover - defensive
        raise AssertionError(f"{path} has an unterminated frontmatter block") from exc
    return text[4 : end + 1], text[end + 5 :]


def _frontmatter(path: Path) -> dict[str, Any]:
    raw, _ = _split_frontmatter(path)
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, dict), f"{path} frontmatter is not a mapping"
    return loaded


def test_skill_files_found() -> None:
    """Guard the glob itself — an empty list would pass every test below."""
    assert len(SKILL_FILES) >= 10


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_frontmatter_parses_as_yaml(path: Path) -> None:
    """A value like `argument-hint: [--months N | ...]` is a YAML flow
    sequence, not a string, and fails to parse. Quote it."""
    _frontmatter(path)


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_name_matches_directory(path: Path) -> None:
    assert _frontmatter(path).get("name") == path.parent.name


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_invocation_fields_are_booleans(path: Path) -> None:
    fm = _frontmatter(path)
    for field in ("disable-model-invocation", "user-invocable"):
        if field in fm:
            assert isinstance(fm[field], bool), (
                f"{path.parent.name}: `{field}` is {fm[field]!r}; "
                f"an unquoted YAML string like `\"true\"` will not gate invocation"
            )


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_user_invoked_skills_keep_a_description(path: Path) -> None:
    """The description is dropped from model context when a skill is
    user-invoked, but it still names the skill in the human's picker."""
    fm = _frontmatter(path)
    if fm.get("disable-model-invocation"):
        assert str(fm.get("description", "")).strip(), (
            f"{path.parent.name}: user-invoked skills still need a "
            f"human-facing description"
        )


def test_skill_tool_targets_remain_model_invoked() -> None:
    """Any skill reached via `Skill(skill=...)` from another skill must
    stay model-invoked, or that call silently stops firing."""
    user_invoked = {
        p.parent.name for p in SKILL_FILES if _frontmatter(p).get("disable-model-invocation")
    }
    local_skills = {p.parent.name for p in SKILL_FILES}

    violations: list[str] = []
    for path in SKILL_FILES:
        _, body = _split_frontmatter(path)
        for target in SKILL_TOOL_CALL.findall(body):
            # Built-ins such as `Skill(skill="simplify")` live outside this
            # repo and carry no frontmatter here to check.
            if target in local_skills and target in user_invoked:
                violations.append(f"{path.parent.name} -> Skill(skill=\"{target}\")")

    assert not violations, (
        "these skills invoke a user-invoked skill via the Skill tool, which "
        "will not fire: " + "; ".join(sorted(violations))
    )


def test_reissued_destinations_are_deleted_before_they_are_written() -> None:
    """A destination that is both retired and reissued must have its
    `delete: true` entry BEFORE its copy entry in the manifest.

    `sync-engine.py` walks targets in list order and does not dedup by
    destination, so a copy placed before the delete is written and then
    unlinked in the same run — the consumer silently never receives the
    file, and the sync log reads as a success. `/grill` is the live case:
    the old review skill is retired at the same path the new pre-code
    interview skill is written to.
    """
    manifest = yaml.safe_load((REPO_ROOT / "scripts" / "sync-targets.yml").read_text())

    first_copy: dict[str, int] = {}
    last_delete: dict[str, int] = {}
    for index, target in enumerate(manifest["targets"]):
        dest = target.get("destination")
        if not dest:
            continue
        if target.get("delete"):
            last_delete[dest] = index
        elif dest not in first_copy:
            first_copy[dest] = index

    clobbered = sorted(
        dest
        for dest, copy_index in first_copy.items()
        if dest in last_delete and last_delete[dest] > copy_index
    )

    assert not clobbered, (
        "these destinations are written and then deleted in the same sync run, "
        "so consumers never receive them — move the copy entry below the "
        "`delete: true` entry: " + "; ".join(clobbered)
    )
