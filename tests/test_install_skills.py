"""Behaviour of scripts/install-skills.sh across every declared harness.

Each test builds a throwaway upstream checkout (manifest plus skill trees) and
a throwaway $HOME, so nothing touches the developer's real skills directories.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install-skills.sh"

MANIFEST = """\
# comment before the block
harnesses:
  claude:
    root: .claude
    home: .claude
    skills: .claude/skills
    cli: fake-claude-cli
    targets: []

  codex:
    root: .codex
    home: .codex
    skills: .agents/skills
    legacy_skills: .codex/skills
    cli: fake-codex-cli
    targets: []

  gemini:
    root: .agents
    home: .gemini/antigravity-cli
    skills: .gemini/antigravity-cli/skills
    cli: fake-agy-cli
    targets: []

shared:
  targets: []
"""

SKILLS = {".claude": ["critique", "issues"], ".codex": ["critique", "pr-critique"], ".agents": ["issues"]}
DESTS = {
    "claude": ".claude/skills",
    "codex": ".agents/skills",
    "gemini": ".gemini/antigravity-cli/skills",
}


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    root = tmp_path / "upstream"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "sync-targets.yml").write_text(MANIFEST)
    for harness_root, names in SKILLS.items():
        for name in names:
            skill = root / harness_root / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"{harness_root}/{name}\n")
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def run(
    upstream: Path, home: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = {
        "HOME": str(home),
        # System tools only, so no real harness CLI is detected.
        "PATH": "/usr/bin:/bin",
        "UPSTREAM_ROOT_OVERRIDE": str(upstream),
    }
    full_env.update(env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def linked(dest: Path) -> dict[str, str]:
    if not dest.is_dir():
        return {}
    return {p.name: os.readlink(p) for p in dest.iterdir() if p.is_symlink()}


def test_installs_every_harness_into_its_own_directory(upstream: Path, home: Path) -> None:
    result = run(upstream, home, "--harness", "claude,codex", "--harness=gemini")
    assert result.returncode == 0, result.stderr

    for harness, root in (("claude", ".claude"), ("codex", ".codex"), ("gemini", ".agents")):
        assert linked(home / DESTS[harness]) == {
            name: str(upstream / root / "skills" / name) for name in SKILLS[root]
        }
    assert "Done: 5 installed" in result.stdout


def test_default_selects_harnesses_by_home_directory(upstream: Path, home: Path) -> None:
    (home / ".codex").mkdir()
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)

    result = run(upstream, home)
    assert result.returncode == 0, result.stderr
    assert "Harnesses: codex gemini (detected on this machine)" in result.stdout
    assert not (home / ".claude").exists()
    assert set(linked(home / DESTS["codex"])) == {"critique", "pr-critique"}
    assert set(linked(home / DESTS["gemini"])) == {"issues"}


def test_default_selects_harnesses_by_cli_on_path(upstream: Path, home: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "fake-agy-cli"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)

    result = run(upstream, home, env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert "Harnesses: gemini (detected on this machine)" in result.stdout


def test_falls_back_to_claude_without_evidence(upstream: Path, home: Path) -> None:
    result = run(upstream, home)
    assert result.returncode == 0, result.stderr
    assert "defaulting to Claude Code" in result.stdout
    assert set(linked(home / DESTS["claude"])) == {"critique", "issues"}
    assert not (home / ".agents").exists()


def test_dry_run_writes_nothing(upstream: Path, home: Path) -> None:
    stale = home / ".agents" / "skills"
    stale.mkdir(parents=True)
    (stale / "retired").symlink_to(upstream / ".codex" / "skills" / "retired")

    result = run(upstream, home, "--harness", "claude,codex,gemini", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Dry run: 5 would install, 0 would replace, 1 would prune" in result.stdout
    assert not (home / ".claude").exists()
    assert not (home / ".gemini").exists()
    assert linked(stale) == {"retired": str(upstream / ".codex" / "skills" / "retired")}


def test_prunes_only_dangling_links_into_this_checkout(upstream: Path, home: Path, tmp_path: Path) -> None:
    dest = home / DESTS["gemini"]
    dest.mkdir(parents=True)
    (dest / "retired").symlink_to(upstream / ".agents" / "skills" / "retired")
    (dest / "other-checkout").symlink_to(tmp_path / "elsewhere" / "gone")
    (dest / "local").mkdir()

    result = run(upstream, home, "--harness", "gemini")
    assert result.returncode == 0, result.stderr
    assert not (dest / "retired").is_symlink()
    assert (dest / "other-checkout").is_symlink(), "a link into another checkout is left alone"
    assert (dest / "local").is_dir(), "a real directory is never touched"
    assert "1 pruned" in result.stdout


def test_legacy_directory_is_pruned_and_reported_never_installed(upstream: Path, home: Path) -> None:
    legacy = home / ".codex" / "skills"
    legacy.mkdir(parents=True)
    (legacy / "retired").symlink_to(upstream / ".codex" / "skills" / "retired")
    (legacy / "critique").symlink_to(upstream / ".codex" / "skills" / "critique")

    result = run(upstream, home, "--harness", "codex")
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in legacy.iterdir()) == ["critique"]
    assert "critique is also linked from" in result.stdout
    assert set(linked(home / DESTS["codex"])) == {"critique", "pr-critique"}


def test_existing_entries_are_kept_unless_forced(upstream: Path, home: Path, tmp_path: Path) -> None:
    dest = home / DESTS["codex"]
    dest.mkdir(parents=True)
    (dest / "critique").mkdir()
    (dest / "critique" / "SKILL.md").write_text("local\n")
    (dest / "pr-critique").symlink_to(tmp_path)

    result = run(upstream, home, "--harness", "codex")
    assert result.returncode == 0, result.stderr
    assert (dest / "critique" / "SKILL.md").read_text() == "local\n"
    assert os.readlink(dest / "pr-critique") == str(tmp_path)

    result = run(upstream, home, "--harness", "codex", "--force")
    assert result.returncode == 0, result.stderr
    assert linked(dest) == {
        "critique": str(upstream / ".codex" / "skills" / "critique"),
        "pr-critique": str(upstream / ".codex" / "skills" / "pr-critique"),
    }
    backups = [p for p in dest.iterdir() if p.name.startswith("critique.bak.")]
    assert len(backups) == 1 and (backups[0] / "SKILL.md").read_text() == "local\n"


def test_destination_overrides(upstream: Path, home: Path, tmp_path: Path) -> None:
    by_flag = tmp_path / "by-flag"
    by_env = tmp_path / "by-env"
    result = run(
        upstream,
        home,
        "--harness",
        "codex,gemini",
        "--dest",
        f"gemini={by_flag}",
        env={"CODEX_SKILLS_DIR": str(by_env), "GEMINI_SKILLS_DIR": str(tmp_path / "loses")},
    )
    assert result.returncode == 0, result.stderr
    assert set(linked(by_flag)) == {"issues"}
    assert set(linked(by_env)) == {"critique", "pr-critique"}
    assert not (tmp_path / "loses").exists(), "--dest wins over the environment variable"
    assert not (home / ".agents").exists()


def test_two_harnesses_sharing_a_directory_are_refused(upstream: Path, home: Path, tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    result = run(upstream, home, "--harness", "claude,codex", "--dest", f"claude={shared}", "--dest", f"codex={shared}")
    assert result.returncode == 2
    assert "give one a --dest" in result.stderr
    assert not shared.exists()


@pytest.mark.parametrize(
    "args, message",
    [
        (("--harness", "gemeni"), "unknown harness: gemeni"),
        (("--dest", "gemeni=/x"), "unknown harness in --dest: gemeni"),
        (("--dest", "codex"), "--dest takes ID=DIR"),
        (("--harness",), "--harness needs a value"),
        (("--bogus",), "unknown argument: --bogus"),
    ],
)
def test_bad_arguments_are_refused(upstream: Path, home: Path, args: tuple[str, ...], message: str) -> None:
    result = run(upstream, home, *args)
    assert result.returncode == 2
    assert message in result.stderr
    assert not any(home.iterdir()), "nothing is written for a refused invocation"


def test_harness_missing_a_required_key_is_refused(upstream: Path, home: Path) -> None:
    manifest = upstream / "scripts" / "sync-targets.yml"
    manifest.write_text(manifest.read_text().replace("    cli: fake-agy-cli\n", ""))
    result = run(upstream, home)
    assert result.returncode == 2
    assert "harness `gemini`" in result.stderr


def test_real_manifest_declares_every_harness(home: Path) -> None:
    """The checked-in manifest parses and maps each tree to its vendor's user directory."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "--harness", "claude,codex,gemini", "--dry-run"],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for harness, root in (("claude", ".claude"), ("codex", ".codex"), ("gemini", ".agents")):
        assert f"{harness}: {root}/skills → {home / DESTS[harness]}" in result.stdout
    assert not any(home.iterdir())
