"""Verify `npx activeloom init` delivers byte- and mode-identical trees to sync-engine.py."""

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

# Outputs written by init but not sync-engine.py.
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
    """Initialize a git repo with an origin remote to satisfy init preconditions."""
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


def _copy_config(path: Path, tier_flags: list[str]) -> None:
    """Write the same valid config policy both doors receive for this tier."""
    body = FIXTURE_CONFIG.read_text(encoding="utf-8")
    if "--sync" in tier_flags:
        # Tier 2 workflow uses GITHUB_TOKEN and must skip workflow target updates.
        body += "\nskip_targets:\n  - .github/workflows/dco.yml\n"
    path.write_text(body, encoding="utf-8")


def _tree(root: Path) -> dict[str, int]:
    """Map every file under `root` to its permission bits."""
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

    via_sync = tmp_path / "via-sync"
    via_sync.mkdir()
    _copy_config(via_sync / ".activeloom-config.yml", tier_flags)
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

    via_cli = tmp_path / "via-cli"
    via_cli.mkdir()
    _init_git_repo(via_cli)
    # Pre-placed config avoids detected substitutions so outputs are comparable.
    _copy_config(via_cli / ".activeloom-config.yml", tier_flags)
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

    sync_tree = _tree(via_sync)
    cli_tree = _tree(via_cli)

    # Tier 2 installs the sync workflow, which the sync engine does not ship.
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

    # shallow=False forces content comparison over stat signatures.
    match, mismatch, errors = filecmp.cmpfiles(
        via_sync, via_cli, list(sync_tree), shallow=False
    )
    assert not mismatch, f"content differs: {sorted(mismatch)}"
    assert not errors, f"could not compare: {sorted(errors)}"
    assert len(match) == len(sync_tree)


def test_init_refuses_to_sync_a_tree_into_itself(tmp_path: Path) -> None:
    """Verify that init refuses to sync an upstream repository into itself."""
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


@pytest.mark.parametrize(
    "link_kind", ["ancestor", "dangling-leaf", "existing-leaf"]
)
def test_init_refuses_selected_consumer_symlinks_before_writing(
    tmp_path: Path, link_kind: str
) -> None:
    """A checkout-controlled link cannot redirect an init write onto the host."""
    _require("node")
    _require("git")

    consumer = tmp_path / "consumer"
    outside = tmp_path / "outside"
    consumer.mkdir()
    outside.mkdir()
    _init_git_repo(consumer)
    _copy_config(consumer / ".activeloom-config.yml", [])

    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    if link_kind == "ancestor":
        (consumer / ".codex").mkdir()
        (consumer / ".codex" / "skills").symlink_to(
            outside, target_is_directory=True
        )
        redirected = outside / "onboard" / "SKILL.md"
    else:
        leaf = consumer / ".codex" / "skills" / "onboard" / "SKILL.md"
        leaf.parent.mkdir(parents=True)
        redirected = sentinel if link_kind == "existing-leaf" else outside / "missing.txt"
        leaf.symlink_to(redirected)

    subprocess.run(["git", "config", "user.name", "Probe"], cwd=consumer, check=True)
    subprocess.run(
        ["git", "config", "user.email", "probe@example.invalid"],
        cwd=consumer,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=consumer, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "probe"], cwd=consumer, check=True)

    result = subprocess.run(
        [
            "node",
            str(CLI),
            "init",
            "--upstream-dir",
            str(REPO_ROOT),
            "--consumer-dir",
            str(consumer),
            "--python",
            sys.executable,
            "--yes",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "consumer-side symlink" in result.stdout + result.stderr
    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    if link_kind != "existing-leaf":
        assert not redirected.exists()
    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_init_refuses_a_non_repository(tmp_path: Path) -> None:
    """Verify init refuses a non-repository consumer directory."""
    _require("node")

    result = subprocess.run(
        ["node", str(CLI), "init", "--upstream-dir", str(REPO_ROOT), "--consumer-dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "not a git repository" in combined
    assert "activeloom add" in combined


@pytest.mark.parametrize("tier_flags", [[], ["--sync"]], ids=["tier1", "tier2"])
def test_init_generated_config_is_accepted_by_the_engine(
    tmp_path: Path, tier_flags: list[str]
) -> None:
    """Verify generated config is accepted by the sync engine for the selected tier."""
    _require("node")
    _require("git")

    repo = tmp_path / "fresh"
    repo.mkdir()
    _init_git_repo(repo)
    result = subprocess.run(
        [
            "node",
            str(CLI),
            "init",
            *tier_flags,
            "--yes",
            "--harness",
            "claude",
            "--upstream-dir",
            str(REPO_ROOT),
            "--consumer-dir",
            str(repo),
            "--python",
            sys.executable,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    config = (repo / ".activeloom-config.yml").read_text(encoding="utf-8")
    dco = repo / ".github" / "workflows" / "dco.yml"
    if "--sync" in tier_flags:
        assert "skip_targets:\n  - .github/workflows/dco.yml" in config
        assert "allow_sensitive_writes: []" in config
        assert not dco.exists(), "tier 2 must not deliver a workflow GITHUB_TOKEN cannot push"
    else:
        assert "allow_sensitive_writes:\n  - .github/workflows/dco.yml" in config
        assert "skip_targets: []" in config
        assert dco.is_file(), "tier 1 delivers the shared DCO workflow"
    assert (repo / ".claude" / "REVIEW_WORKFLOW.md").is_file()
