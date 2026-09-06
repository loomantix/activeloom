"""`npx activeloom init` delivers exactly what the CI sync delivers.

This is the executable form of #786's acceptance criterion: "`init` on a
triple-harness repo writes the same trees sync would deliver". It is an
equivalence claim, so it is proved the way activeloom's other equivalence
claims are — render both sides from one config and compare the bytes — rather
than asserted in prose.

The comparison is expected to be trivially true, and that is the design rather
than a weakness of the test: `init` shells out to `scripts/sync-engine.py` with
the same arguments the consumer workflow uses, so there is no second
implementation that *could* drift. What this test actually pins is that the CLI
keeps delegating instead of growing its own copy of the manifest walk — the day
someone reimplements the engine in JavaScript "for speed", this goes red.

Modes are compared alongside content because the manifest ships `0755` targets
(`skills/issues/scripts/ready.py`) that are invoked directly. A copy that
flattened permissions would pass a content-only diff and install a skill that
silently cannot run its own helper.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "cli" / "bin" / "activeloom.js"
FIXTURE_CONFIG = REPO_ROOT / "tests" / "fixtures" / "render-check" / ".activeloom-config.yml"

# Written by `init` but not by the sync engine: the CLI's own outputs. They are
# additions on top of an identical synced tree, not divergences within it, so
# the comparison excludes them by name rather than by pattern.
CLI_ONLY = frozenset(
    {
        ".activeloom-config.yml",
        ".git",
    }
)


def _require(binary: str) -> None:
    if shutil.which(binary) is None:
        pytest.skip(f"{binary} not available")


def _init_git_repo(path: Path) -> None:
    """A consumer `init` will accept: a git repo with a GitHub origin.

    `init` refuses to run outside a git repository, and tiers 2+ additionally
    require a GitHub remote. This builds the minimum that satisfies both, with
    no network access.
    """
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/consumer.git"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=path,
        check=True,
    )


def _tree(root: Path) -> dict[str, int]:
    """Map every file under `root` to its permission bits.

    Directories are not listed in their own right: an empty directory on one
    side and not the other shows up as a missing file, and a directory that
    holds files is implied by them. Symlinks are excluded because the shipped
    surface contains none, so one appearing is a separate bug from this one.
    """
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        top = rel_dir.parts[0] if rel_dir.parts else None
        if top in CLI_ONLY:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if (rel_dir / d).parts[0] not in CLI_ONLY]
        for name in filenames:
            rel = rel_dir / name
            if rel.parts[0] in CLI_ONLY:
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            out[rel.as_posix()] = stat.S_IMODE(full.stat().st_mode)
    return out


@pytest.mark.parametrize("tier_flags", [[], ["--sync"]], ids=["tier1", "tier2"])
def test_init_writes_what_sync_writes(tmp_path: Path, tier_flags: list[str]) -> None:
    """The synced surface is byte- and mode-identical between the two doors."""
    _require("node")
    _require("git")

    # --- side A: the engine, invoked exactly as the consumer workflow does ---
    via_sync = tmp_path / "via-sync"
    via_sync.mkdir()
    shutil.copy(FIXTURE_CONFIG, via_sync / ".activeloom-config.yml")
    engine = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "sync-engine.py"),
            "--upstream-repo",
            str(REPO_ROOT),
            "--consumer-dir",
            str(via_sync),
        ],
        capture_output=True,
        text=True,
    )
    assert engine.returncode == 0, engine.stderr

    # --- side B: the CLI ---------------------------------------------------
    via_cli = tmp_path / "via-cli"
    via_cli.mkdir()
    _init_git_repo(via_cli)
    # The same config, placed before the run. `init` keeps an existing config
    # rather than overwriting it, which is what makes the two sides comparable:
    # a generated config would carry this repo's detected values and substitute
    # different content into `.github/copilot-instructions.md`.
    shutil.copy(FIXTURE_CONFIG, via_cli / ".activeloom-config.yml")
    cli = subprocess.run(
        [
            "node",
            str(CLI),
            "init",
            *tier_flags,
            "--upstream-dir",
            str(REPO_ROOT),
            "--consumer-dir",
            str(via_cli),
            "--python",
            sys.executable,
        ],
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stdout + cli.stderr

    # --- compare ------------------------------------------------------------
    sync_tree = _tree(via_sync)
    cli_tree = _tree(via_cli)

    # Tier 2 additionally installs the sync workflow, which the engine does not
    # ship. Account for it explicitly rather than filtering `.github/workflows`
    # wholesale — the engine *does* ship `dco.yml` there, and a filter broad
    # enough to hide the CLI's workflow would hide a real divergence in that one.
    workflow = ".github/workflows/sync-from-upstream.yml"
    if "--sync" in tier_flags:
        assert workflow in cli_tree, "tier 2 must install the sync workflow"
        del cli_tree[workflow]
    else:
        assert workflow not in cli_tree, "tier 1 must not install a workflow"

    assert sorted(cli_tree) == sorted(sync_tree), (
        "the two doors delivered different file sets:\n"
        f"  only via CLI:  {sorted(set(cli_tree) - set(sync_tree))}\n"
        f"  only via sync: {sorted(set(sync_tree) - set(cli_tree))}"
    )

    mode_diffs = {
        rel: (sync_tree[rel], cli_tree[rel]) for rel in sync_tree if sync_tree[rel] != cli_tree[rel]
    }
    assert not mode_diffs, f"permission bits differ (sync, cli): {mode_diffs}"

    # `shallow=False` forces a content read. The default compares stat
    # signatures, and two files written seconds apart with the same size would
    # compare equal without either being opened.
    match, mismatch, errors = filecmp.cmpfiles(
        via_sync, via_cli, list(sync_tree), shallow=False
    )
    assert not mismatch, f"content differs: {sorted(mismatch)}"
    assert not errors, f"could not compare: {sorted(errors)}"
    assert len(match) == len(sync_tree)


def test_init_refuses_to_sync_a_tree_into_itself(tmp_path: Path) -> None:
    """The guard that a self-sync would otherwise delete source files.

    The manifest carries `delete: true` retirement targets, so running the
    engine with the upstream as its own consumer removes files from the
    upstream working tree while reporting a successful sync. This is reachable
    by typing something reasonable, so it is refused rather than documented.
    """
    _require("node")

    result = subprocess.run(
        [
            "node",
            str(CLI),
            "init",
            "--upstream-dir",
            str(REPO_ROOT),
            "--consumer-dir",
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "refusing to sync a tree into itself" in result.stdout + result.stderr


def test_init_refuses_a_non_repository(tmp_path: Path) -> None:
    """`init` writes files a team commits, so it needs a repository to be in."""
    _require("node")

    result = subprocess.run(
        ["node", str(CLI), "init", "--upstream-dir", str(REPO_ROOT), "--consumer-dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "not a git repository" in combined
    # The refusal has to name the tier that *does* work without a repo, or a
    # first-time user reads it as "this tool does not work here".
    assert "activeloom add" in combined
