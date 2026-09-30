"""The run controller ships with every harness, at that harness's own path.

`critique` and `deepcritique` call the controller on every pass and stop when it
is absent, so the harness a repository selected used to decide whether it could
review at all: one copy existed, under `.codex/`, and `sync-targets.yml`
delivered it with the `codex` target set only. Record 0015 makes it one source
vendored into each root.

These tests pin the two halves that a future edit could quietly undo — the
copies existing and agreeing, and no prompt reaching across roots to find them.
A drifted copy is caught by `render-prompts.py --check`; what is not caught
there is a *deleted* sync-target entry or a path reference creeping back, which
is what fails here.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
CONTROLLER_SOURCE = ROOT / "packages/review-ledger/protocol/local-review-handoff.py"
ROOT_RELATIVE = "skills/critique/scripts/local-review-handoff.py"
HARNESS_ROOTS = (".claude", ".codex", ".agents")

# Which target set owns which root. Presence alone is not the invariant: a set
# delivering another set's root reinstates the coupling record 0015 removes.
ROOT_BY_TARGET_SET = {"claude": ".claude", "codex": ".codex", "gemini": ".agents"}

# Every prompt that resolves the controller by path. Each must name its own root.
# `.codex/skills/{critique,deepcritique}/SKILL.md` name it by bare filename
# rather than by path, so they cannot satisfy the positive half below.
CONTROLLER_CALLERS = (
    ".claude/REVIEW_WORKFLOW.md",
    ".claude/skills/critique/SKILL.md",
    ".claude/skills/deepcritique/SKILL.md",
    ".codex/REVIEW_WORKFLOW.md",
    ".agents/REVIEW_WORKFLOW.md",
    ".agents/skills/critique/SKILL.md",
    ".agents/skills/deepcritique/SKILL.md",
)


def _tracked_files() -> list[str]:
    """Repository-relative paths Git tracks, as POSIX strings.

    Not `rglob`: a linked worktree under `.claude/worktrees/` is an untracked
    nested checkout carrying the source and every root's copy, so walking the
    filesystem counts those too and fails for a reason unrelated to any
    invariant here — green in CI, red in the checkout the work happens in.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [entry for entry in listing.split("\0") if entry]


def test_the_controller_has_exactly_one_source() -> None:
    assert CONTROLLER_SOURCE.is_file(), CONTROLLER_SOURCE
    found = sorted(
        path
        for path in _tracked_files()
        if Path(path).name == "local-review-handoff.py" and "imports/" not in path
    )
    expected = sorted(
        [CONTROLLER_SOURCE.relative_to(ROOT).as_posix()]
        + [f"{root}/{ROOT_RELATIVE}" for root in HARNESS_ROOTS]
    )
    assert found == expected, (
        "the controller must exist as one source plus one vendored copy per "
        f"harness root; found {found}"
    )


@pytest.mark.parametrize("harness_root", HARNESS_ROOTS)
def test_every_root_carries_the_source_bytes_and_stays_executable(
    harness_root: str,
) -> None:
    copy = ROOT / harness_root / ROOT_RELATIVE
    assert copy.is_file(), f"{harness_root} has no run controller"
    assert copy.read_bytes() == CONTROLLER_SOURCE.read_bytes(), (
        f"{harness_root}'s copy has drifted from the source; "
        "run `python3 scripts/render-prompts.py`"
    )
    # The skills invoke it directly, so a copy that lost its mode is unusable
    # in a way no content comparison would report.
    assert copy.stat().st_mode & stat.S_IXUSR, f"{harness_root}'s copy is not executable"


@pytest.mark.parametrize("harness_root", HARNESS_ROOTS)
def test_every_target_set_delivers_the_controller_to_its_own_root(
    harness_root: str,
) -> None:
    document = yaml.safe_load(
        (ROOT / "scripts/sync-targets.yml").read_text(encoding="utf-8")
    )
    destinations = {
        harness: {
            target["destination"]
            for target in block["targets"]
            if "destination" in target
        }
        for harness, block in document["harnesses"].items()
    }
    assert set(destinations) == set(ROOT_BY_TARGET_SET), (
        "a target set was added or renamed; map it to its root so this test "
        f"still covers every one: {sorted(destinations)}"
    )
    owning = sorted(
        harness
        for harness, paths in destinations.items()
        if f"{harness_root}/{ROOT_RELATIVE}" in paths
    )
    expected = sorted(
        harness for harness, root in ROOT_BY_TARGET_SET.items() if root == harness_root
    )
    assert owning == expected, (
        f"{harness_root}/{ROOT_RELATIVE} must be delivered by {expected} and by "
        f"no other target set; found {owning}. Missing, and a repository "
        "selecting that harness alone could not run a review pass; delivered "
        "from elsewhere, and it is the cross-root coupling record 0015 removed"
    )


@pytest.mark.parametrize("caller", CONTROLLER_CALLERS)
def test_no_prompt_resolves_the_controller_through_another_root(caller: str) -> None:
    path = ROOT / caller
    text = path.read_text(encoding="utf-8")
    own_root = caller.split("/")[0]
    assert f"{own_root}/{ROOT_RELATIVE}" in text, (
        f"{caller} must resolve the controller under {own_root}"
    )
    for other in HARNESS_ROOTS:
        if other == own_root:
            continue
        assert f"{other}/{ROOT_RELATIVE}" not in text, (
            f"{caller} reaches into {other} for the run controller; that is the "
            "coupling record 0015 removed"
        )
