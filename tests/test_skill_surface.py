"""Canonical Codex skill/sync-surface regression gates."""
from __future__ import annotations

import hashlib
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = REPO_ROOT / ".codex/skills"
MANIFEST = REPO_ROOT / "scripts/sync-targets.yml"
SYNC_ENGINE = REPO_ROOT / "scripts/sync-engine.py"
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _manifest_targets() -> list[dict[str, Any]]:
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return cast(list[dict[str, Any]], doc["targets"])


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} lacks YAML frontmatter"
    _, raw, _ = text.split("---", 2)
    return cast(dict[str, Any], yaml.safe_load(raw))


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out[rel] = (digest, stat.S_IMODE(path.stat().st_mode))
    return out


def test_every_skill_passes_current_frontmatter_rules() -> None:
    skills = sorted(SKILLS_ROOT.glob("*/SKILL.md"))
    assert skills
    for path in skills:
        metadata = _frontmatter(path)
        assert set(metadata) == {"name", "description"}, path
        assert metadata["name"] == path.parent.name
        assert SKILL_NAME_RE.fullmatch(metadata["name"]), path
        assert len(metadata["name"]) < 64
        assert isinstance(metadata["description"], str) and metadata["description"].strip()


def test_manifest_covers_every_skill_and_declares_executable_modes() -> None:
    targets = _manifest_targets()
    sources = {target.get("source") for target in targets if target.get("source")}
    skill_dirs = sorted(path.parent for path in SKILLS_ROOT.glob("*/SKILL.md"))
    missing = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in skill_dirs
        if f"{path.relative_to(REPO_ROOT).as_posix()}/SKILL.md" not in sources
    ]
    assert missing == []

    for target in targets:
        source = target.get("source")
        if not source:
            continue
        path = REPO_ROOT / source
        assert path.is_file(), source
        source_mode = stat.S_IMODE(path.stat().st_mode)
        declared_mode = target.get("mode")
        if source_mode & 0o111:
            assert declared_mode is not None, f"executable target lacks mode: {source}"
        if declared_mode is not None:
            assert source_mode == int(str(declared_mode), 8), source


def test_recommended_prettierignore_mirrors_static_sync_targets() -> None:
    desired = {
        target["destination"]
        for target in _manifest_targets()
        if not target.get("delete")
        and not str(target.get("source", "")).endswith(".template")
        and not target.get("substitutions")
    }
    text = (REPO_ROOT / "recommended-prettierignore.txt").read_text(encoding="utf-8")
    marker = text.split("# >>> platform-synced paths <<<", 1)[1].split(
        "# <<< platform-synced paths >>>", 1
    )[0]
    actual = {line.strip() for line in marker.splitlines() if line.strip()}
    assert actual == desired


def test_canonical_sync_preserves_consumer_owned_files_and_is_idempotent(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    config = consumer / ".codex-platform-config.yml"
    config.write_text(
        "substitutions: {}\n"
        "skip_targets:\n"
        "  - .github/copilot-instructions.md\n",
        encoding="utf-8",
    )
    cmd = [
        sys.executable,
        str(SYNC_ENGINE),
        "--upstream-repo",
        str(REPO_ROOT),
        "--consumer-dir",
        str(consumer),
        "--config",
        str(config),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    consumer_owned = [
        consumer / ".codex/skills/agent-loop/agent-loop.config",
        consumer / ".codex/skills/agent-loop/prompt.txt",
        consumer / "agent-loop-instructions.md",
        consumer / ".codex/skills/backlog-refinement/RUBRIC.md",
        consumer / ".codex/skills/backlog-refinement/LEARNINGS.md",
    ]
    for path in consumer_owned:
        assert path.is_file()
    for guard_name in ("hook-gh-guard", "hook-git-guard"):
        relative = Path(".codex/skills/agent-loop/scripts") / guard_name
        synced_guard = consumer / relative
        upstream_guard = REPO_ROOT / relative
        assert synced_guard.is_file()
        assert synced_guard.read_bytes() == upstream_guard.read_bytes()
        assert stat.S_IMODE(synced_guard.stat().st_mode) == 0o755
    assert "review_contract_version = 2" in (
        consumer / ".codex/skills/agent-loop/agent-loop.config"
    ).read_text(encoding="utf-8")
    sentinel = "\nconsumer customization\n"
    for path in consumer_owned:
        path.write_text(path.read_text(encoding="utf-8") + sentinel, encoding="utf-8")

    before = _snapshot(consumer)
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    after = _snapshot(consumer)
    assert after == before
    assert all(path.read_text(encoding="utf-8").endswith(sentinel) for path in consumer_owned)
    assert "unchanged" in result.stdout.lower() or "no changes" in result.stdout.lower()


def test_new_script_modes_are_executable() -> None:
    expected = {
        ".codex/skills/agent-loop/scripts/agent-loop.sh": 0o755,
        ".codex/skills/agent-loop/scripts/hook-gh-guard": 0o755,
        ".codex/skills/agent-loop/scripts/hook-git-guard": 0o755,
        ".codex/skills/backlog-refinement/scripts/bail-report.py": 0o755,
        ".codex/skills/backlog-refinement/scripts/candidates.py": 0o755,
        ".codex/skills/issues/scripts/ready.py": 0o755,
    }
    assert {
        path: stat.S_IMODE((REPO_ROOT / path).stat().st_mode)
        for path in expected
    } == expected
