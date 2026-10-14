"""A review prompt resolves its helpers under its own harness root.

The review-protocol prompts are read by an agent working in a repository that
installed some subset of the harnesses. A prompt that names another root's path
is a command that fails whenever that root was not selected — and it fails at
review time, because nothing at install checks it.

Two of these have been found by hand: the run controller (#358) and
`review-profile.py` in all three `REVIEW_WORKFLOW.md` files (#360). Both were
one copied sentence. This pins the class instead of the instances.

The exception is deliberate and small: `review-chain-runner.py` drives the
automatic multi-engine chain and lives only in the Codex control surface, so
every root names it by its `.codex` path. That is recorded in
`docs/decisions/0015-run-controller-is-vendored-per-root.md` and is why the
allowlist below is a set of paths rather than a blanket opt-out per file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
HARNESS_ROOTS = (".claude", ".codex", ".agents")

# The prompts an agent follows to run a review. Deliberately excludes the
# linters under `.claude/` (they scan every root as input data) and `agent-loop`
# (per-harness by record 0007, and it launches other engines' surfaces on
# purpose).
REVIEW_PROMPTS = (
    "REVIEW_WORKFLOW.md",
    "skills/critique/SKILL.md",
    "skills/deepcritique/SKILL.md",
    "skills/refactorpass/SKILL.md",
    "skills/reviewit/SKILL.md",
)

# Executable paths a prompt may name outside its own root, each because exactly
# one copy exists by design.
CROSS_ROOT_ALLOWED = frozenset(
    {
        ".codex/skills/critique/scripts/review-chain-runner.py",
    }
)

# The trailing lookahead matters: without it `.js` matches inside `.json` and
# every `<root>/prompt-stack.json` reference reads as a missing script.
HELPER_PATH = re.compile(
    r"(?:\.claude|\.codex|\.agents)/[A-Za-z0-9._/-]+\.(?:py|js|sh)(?![A-Za-z0-9])"
)


def _prompt_files() -> list[tuple[str, Path]]:
    found = []
    for harness_root in HARNESS_ROOTS:
        for relative in REVIEW_PROMPTS:
            path = ROOT / harness_root / relative
            if path.is_file():
                found.append((harness_root, path))
    return found


def test_the_prompt_set_is_actually_present() -> None:
    # Guards the guard: a renamed prompt would otherwise silently empty the
    # parametrization below and the suite would still pass.
    discovered = _prompt_files()
    assert len(discovered) >= 12, f"expected the review prompt set, found {len(discovered)}"


@pytest.mark.parametrize(
    ("harness_root", "prompt"),
    [(root, path) for root, path in _prompt_files()],
    ids=[f"{root}:{path.name}" for root, path in _prompt_files()],
)
def test_review_prompts_name_helpers_under_their_own_root(
    harness_root: str, prompt: Path
) -> None:
    referenced = sorted(set(HELPER_PATH.findall(prompt.read_text(encoding="utf-8"))))
    offenders = [
        path
        for path in referenced
        if not path.startswith(f"{harness_root}/") and path not in CROSS_ROOT_ALLOWED
    ]
    assert not offenders, (
        f"{prompt.relative_to(ROOT)} names {offenders} outside {harness_root}; a "
        "repository that did not select that harness runs a command with no file "
        "behind it"
    )


@pytest.mark.parametrize(
    ("harness_root", "prompt"),
    [(root, path) for root, path in _prompt_files()],
    ids=[f"{root}:{path.name}" for root, path in _prompt_files()],
)
def test_every_helper_a_review_prompt_names_exists(
    harness_root: str, prompt: Path
) -> None:
    referenced = sorted(set(HELPER_PATH.findall(prompt.read_text(encoding="utf-8"))))
    missing = [path for path in referenced if not (ROOT / path).is_file()]
    assert not missing, (
        f"{prompt.relative_to(ROOT)} names {missing}, which do not exist in this "
        "repository"
    )
