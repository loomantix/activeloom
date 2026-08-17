"""Canonical Codex skill/sync-surface regression gates."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = REPO_ROOT / ".agents/skills"
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


def _canonical_sync_command(consumer: Path) -> list[str]:
    config = consumer / ".gemini-platform-config.yml"
    config.write_text(
        "substitutions: {}\nskip_targets:\n  - .github/copilot-instructions.md\n",
        encoding="utf-8",
    )
    return [
        sys.executable,
        str(SYNC_ENGINE),
        "--upstream-repo",
        str(REPO_ROOT),
        "--consumer-dir",
        str(consumer),
        "--config",
        str(config),
    ]


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


def test_install_skills_prunes_only_retired_owned_links(
    tmp_path: Path, monkeypatch: Any
) -> None:
    upstream = tmp_path / "upstream"
    skills = upstream / ".agents/skills"
    active = skills / "active"
    active.mkdir(parents=True)
    destination = tmp_path / "installed"
    destination.mkdir()

    owned_stale = destination / "retired"
    owned_stale.symlink_to(skills / "retired")
    mismatched_alias = destination / "alias"
    mismatched_alias.symlink_to(skills / "retired")
    foreign_stale = destination / "foreign"
    foreign_stale.symlink_to(tmp_path / "elsewhere/missing")
    # Same *name* as a skill this installer would create, but owned by a
    # different checkout. Only full-target equality rejects this one — a
    # basename-only ownership check would delete another checkout's link.
    foreign_same_name = destination / "shared"
    foreign_same_name.symlink_to(tmp_path / "other-clone/.agents/skills/shared")
    local_skill = destination / "local"
    local_skill.mkdir()

    env = {
        **os.environ,
        "UPSTREAM_ROOT_OVERRIDE": "upstream",
        "GEMINI_SKILLS_DIR": str(destination),
    }
    script = REPO_ROOT / "scripts/install-skills.sh"
    monkeypatch.chdir(tmp_path)

    dry_run = subprocess.run(
        [str(script), "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "1 would prune" in dry_run.stdout
    # A dry run must not claim it removed anything.
    assert "would prune dangling link" in dry_run.stdout
    assert "removing dangling link" not in dry_run.stdout
    assert owned_stale.is_symlink()

    subprocess.run([str(script)], check=True, capture_output=True, text=True, env=env)
    assert not owned_stale.is_symlink()
    assert mismatched_alias.is_symlink()
    assert foreign_stale.is_symlink()
    assert foreign_same_name.is_symlink()
    assert local_skill.is_dir()
    assert (destination / "active").resolve() == active.resolve()

    # Pruning is a no-op once nothing retired remains; a regression that
    # dropped the dangling-link check would delete every owned link here.
    clean = subprocess.run(
        [str(script)], check=True, capture_output=True, text=True, env=env
    )
    assert "0 pruned" in clean.stdout
    assert (destination / "active").resolve() == active.resolve()


def test_canonical_sync_preserves_consumer_owned_files_and_is_idempotent(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    cmd = _canonical_sync_command(consumer)
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    consumer_owned = [
        consumer / ".agents/skills/agent-loop/agent-loop.config",
        consumer / ".agents/skills/agent-loop/prompt.txt",
        consumer / "agent-loop-instructions.md",
        consumer / ".agents/skills/backlog-refinement/RUBRIC.md",
        consumer / ".agents/skills/backlog-refinement/LEARNINGS.md",
    ]
    for path in consumer_owned:
        assert path.is_file()
    for guard_name in ("hook-gh-guard", "hook-git-guard"):
        relative = Path(".agents/skills/agent-loop/scripts") / guard_name
        synced_guard = consumer / relative
        upstream_guard = REPO_ROOT / relative
        assert synced_guard.is_file()
        assert synced_guard.read_bytes() == upstream_guard.read_bytes()
        assert stat.S_IMODE(synced_guard.stat().st_mode) == 0o755
    ledger_relative = Path(".agents/references/local-review-ledger.md")
    synced_ledger = consumer / ledger_relative
    assert synced_ledger.is_file()
    assert synced_ledger.read_bytes() == (REPO_ROOT / ledger_relative).read_bytes()
    assert "review_contract_version = 3" in (
        consumer / ".agents/skills/agent-loop/agent-loop.config"
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


def test_sync_replaces_retired_review_skill_paths_on_success(tmp_path: Path) -> None:
    consumer = tmp_path / "consumer"
    # `.agents/skills/grill/SKILL.md` is retired AND reissued: the old review
    # skill is deleted and the new pre-code interview skill is written to the
    # same path. So the contract here is "the retired CONTENT is gone", not
    # "the path is absent" — the other three paths do go away entirely.
    reissued = consumer / ".agents/skills/grill/SKILL.md"
    old_paths = [
        reissued,
        consumer / ".agents/skills/grill/scripts/review-ledger.py",
        consumer / ".agents/skills/deepgrill/SKILL.md",
        consumer / ".agents/skills/pr-grill/SKILL.md",
    ]
    for path in old_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("retired\n", encoding="utf-8")

    subprocess.run(
        _canonical_sync_command(consumer),
        check=True,
        capture_output=True,
        text=True,
    )

    assert not any(path.exists() for path in old_paths if path != reissued)
    # Written after the delete, not clobbered by it.
    assert reissued.is_file()
    assert reissued.read_text(encoding="utf-8") != "retired\n"
    assert _frontmatter(reissued)["name"] == "grill"
    for skill in ("critique", "deepcritique", "pr-critique"):
        assert (consumer / f".agents/skills/{skill}/SKILL.md").is_file()
    ledger = consumer / ".agents/skills/critique/scripts/review-ledger.py"
    assert ledger.is_file()
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o755


def test_new_script_modes_are_executable() -> None:
    expected = {
        ".agents/skills/agent-loop/scripts/agent-loop.sh": 0o755,
        ".agents/skills/agent-loop/scripts/agent-loop-state.py": 0o755,
        ".agents/skills/agent-loop/scripts/hook-gh-guard": 0o755,
        ".agents/skills/agent-loop/scripts/hook-git-guard": 0o755,
        ".agents/skills/backlog-refinement/scripts/bail-report.py": 0o755,
        ".agents/skills/backlog-refinement/scripts/candidates.py": 0o755,
        ".agents/skills/critique/scripts/review-ledger.py": 0o755,
        ".agents/skills/issues/scripts/ready.py": 0o755,
    }
    assert {
        path: stat.S_IMODE((REPO_ROOT / path).stat().st_mode)
        for path in expected
    } == expected


def test_local_review_skills_share_cache_stable_scoped_context() -> None:
    ledger = (REPO_ROOT / ".agents/references/local-review-ledger.md").read_text(
        encoding="utf-8"
    )
    deepcritique = (SKILLS_ROOT / "deepcritique/SKILL.md").read_text(encoding="utf-8")
    critique = (SKILLS_ROOT / "critique/SKILL.md").read_text(encoding="utf-8")
    refactorpass = (SKILLS_ROOT / "refactorpass/SKILL.md").read_text(encoding="utf-8")
    normalized = {
        name: " ".join(text.split())
        for name, text in {
            "ledger": ledger,
            "deepcritique": deepcritique,
            "critique": critique,
            "refactorpass": refactorpass,
        }.items()
    }

    assert "Build one immutable review packet" in normalized["ledger"]
    assert "REVIEW_PACKET_V1" in normalized["ledger"]
    assert "byte-identical prompt prefix" in normalized["ledger"]
    assert 'fork_turns="none"' in normalized["ledger"]
    assert "git diff <base-sha>..<head-sha> -- <path>" in normalized["ledger"]
    assert "Do not create one whole-diff" in normalized["ledger"]
    assert "Bound lane output" in normalized["ledger"]
    assert "maximum 1000 words" in normalized["ledger"]
    assert "Review-significant config" in normalized["ledger"]
    assert "dependency manifests and lockfiles" in normalized["ledger"]
    assert "`attest --threads-file <path> --allowed-heads-file <path>`" in ledger

    for skill in (
        normalized["deepcritique"],
        normalized["critique"],
        normalized["refactorpass"],
    ):
        assert "immutable review packet" in skill
        assert "scoped diff" in skill.lower()
        assert "no inherited conversation history" in skill

    assert "changed-file list, and diff stat once" in normalized["deepcritique"]
    assert "end that packet and build a new immutable review packet" in normalized[
        "deepcritique"
    ]
    assert "Do not make each lane reload the PR ledger" in normalized["critique"]
    assert "do not hand every lane a whole-diff artifact" in normalized["refactorpass"]


def test_renamed_review_skills_preserve_in_flight_compatibility() -> None:
    reviewit = (SKILLS_ROOT / "reviewit/SKILL.md").read_text(encoding="utf-8")
    pr_critique = (SKILLS_ROOT / "pr-critique/SKILL.md").read_text(encoding="utf-8")
    normalized_reviewit = " ".join(reviewit.lower().split())

    assert "final-deepgrill" in reviewit
    assert "deepgrillRan" in reviewit
    # Pin the discriminator, not just the phrase: a legacy key with its current
    # counterpart absent is ordinary migration input and must not fail closed.
    assert "normalize it silently" in normalized_reviewit
    assert (
        "fail closed only on a genuine conflict — both spellings present with "
        "different values" in normalized_reviewit
    )
    assert "PR_GRILL_REVIEW_BASE_SHA" in pr_critique
    assert "AGENT_LOOP_REVIEW_BASE_SHA" in pr_critique
    assert "review base overrides are set to different commits" in pr_critique
    assert "round-matched fix bias" in pr_critique


def test_parallel_review_lanes_delegate_validation_to_orchestrator() -> None:
    for skill_name in ("critique", "pr-critique"):
        text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        normalized = " ".join(text.split())

        assert "Review lanes are read-only analysis workers" in normalized
        assert "must not run test suites, linters, formatters, builds" in normalized
        assert "package installation, or CI polling" in normalized
        assert "smallest proposed probe to the orchestrator" in normalized
        assert "one consolidated validation pass" in normalized


def test_ship_staging_marks_drafts_ready_and_verifies_the_transition() -> None:
    text = (SKILLS_ROOT / "ship-staging/SKILL.md").read_text(encoding="utf-8")

    ready_command = text.index("gh pr ready <pr>")
    refetch = text.index("Could not verify PR after marking it ready")
    merge_command = text.index("gh pr merge <pr> --merge --delete-branch", refetch)

    assert ready_command < refetch < merge_command
    assert '.headRefOid == $head' in text
    assert ".isDraft == false" in text


def test_reissued_destinations_are_deleted_before_they_are_written() -> None:
    """A destination that is both retired and reissued must have its
    `delete: true` entry BEFORE its copy entry in the manifest.

    `sync-engine.py` applies targets in list order and does not dedup by
    destination, so a copy placed before the delete is written and then
    unlinked in the same run — the consumer silently never receives the
    file, and the sync log still reads as a success. `grill` is the live
    case: the old review skill is retired at the same path the new
    pre-code interview skill is written to.
    """
    first_copy: dict[str, int] = {}
    last_delete: dict[str, int] = {}
    for index, target in enumerate(_manifest_targets()):
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
