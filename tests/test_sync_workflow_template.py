"""Security-contract tests for the consumer sync workflow template."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "sync-from-upstream.yml.template"
GETTING_STARTED_PATH = REPO_ROOT / "docs" / "getting-started.md"
SYNC_DOC_PATH = REPO_ROOT / "docs" / "sync.md"
MANIFEST_PATH = REPO_ROOT / "scripts" / "sync-targets.yml"


def _workflow_steps() -> list[dict[str, Any]]:
    document = cast(dict[str, Any], yaml.safe_load(WORKFLOW_PATH.read_text()))
    jobs = cast(dict[str, Any], document["jobs"])
    sync_job = cast(dict[str, Any], jobs["sync"])
    return cast(list[dict[str, Any]], sync_job["steps"])


def _verification_script() -> str:
    step = next(
        item for item in _workflow_steps() if item.get("name") == "Verify upstream tag signature"
    )
    return cast(str, step["run"])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _generate_key(path: Path) -> str:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path.with_suffix(".pub").read_text().strip()


@pytest.fixture
def signed_tag_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Release Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    (repo / "payload.txt").write_text("trusted\n")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-qm", "fixture")

    public_key = _generate_key(tmp_path / "release-key")
    _git(repo, "config", "gpg.format", "ssh")
    _git(repo, "config", "user.signingkey", str(tmp_path / "release-key.pub"))
    _git(repo, "tag", "-s", "sync-v1", "-m", "signed fixture")
    # Isolate this fixture from a developer-level `tag.gpgsign=true` setting.
    _git(repo, "-c", "tag.gpgSign=false", "tag", "-a", "unsigned", "-m", "unsigned fixture")
    return repo, f"maintainer@example.com {public_key}"


def _run_verification(
    repo: Path,
    *,
    ref_kind: str,
    upstream_ref: str,
    allowed_signers: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "ALLOWED_SIGNERS": allowed_signers,
            "REF_KIND": ref_kind,
            "UPSTREAM_DIR": str(repo),
            "UPSTREAM_REPO": "fixture/upstream",
            "UPSTREAM_REF": upstream_ref,
        }
    )
    return subprocess.run(
        ["bash", "-c", _verification_script()],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")
def test_signature_gate_accepts_an_allowed_signer(
    signed_tag_repo: tuple[Path, str],
) -> None:
    repo, allowed_signers = signed_tag_repo
    result = _run_verification(
        repo,
        ref_kind="tag",
        upstream_ref="sync-v1",
        allowed_signers=allowed_signers,
    )
    assert result.returncode == 0, result.stderr
    assert "Verified signature on sync-v1" in result.stdout


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")
def test_signature_gate_rejects_unsigned_and_wrong_key_tags(
    signed_tag_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, allowed_signers = signed_tag_repo
    unsigned = _run_verification(
        repo,
        ref_kind="tag",
        upstream_ref="unsigned",
        allowed_signers=allowed_signers,
    )
    assert unsigned.returncode != 0
    assert "unsigned or its signature does not match" in unsigned.stdout
    assert "no signature found" in unsigned.stderr

    wrong_key = _generate_key(tmp_path / "wrong-key")
    wrong = _run_verification(
        repo,
        ref_kind="tag",
        upstream_ref="sync-v1",
        allowed_signers=f"maintainer@example.com {wrong_key}",
    )
    assert wrong.returncode != 0
    assert "unsigned or its signature does not match" in wrong.stdout


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")
def test_signature_gate_preserves_explicit_migration_mode(
    signed_tag_repo: tuple[Path, str],
) -> None:
    repo, _ = signed_tag_repo
    result = _run_verification(
        repo,
        ref_kind="tag",
        upstream_ref="sync-v1",
        allowed_signers="",
    )
    assert result.returncode == 0
    assert "SYNC_TAG_ALLOWED_SIGNERS is not set" in result.stdout


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")
def test_signature_gate_rejects_branch_downgrade_when_configured(
    signed_tag_repo: tuple[Path, str],
) -> None:
    repo, allowed_signers = signed_tag_repo
    result = _run_verification(
        repo,
        ref_kind="branch",
        upstream_ref="sync-v1",
        allowed_signers=allowed_signers,
    )
    assert result.returncode != 0
    assert "resolved to a branch, not a tag" in result.stderr


def _first_yaml_block(path: Path, heading: str | None = None) -> dict[str, Any]:
    text = path.read_text()
    if heading is not None:
        text = text[text.index(heading) :]
    match = re.search(r"```yaml\n(.*?)\n```", text, flags=re.DOTALL)
    assert match is not None
    return cast(dict[str, Any], yaml.safe_load(match.group(1)))


def test_documented_allowlist_covers_the_canonical_manifest(sync_engine: ModuleType) -> None:
    getting_started = _first_yaml_block(GETTING_STARTED_PATH)
    sync_doc = _first_yaml_block(SYNC_DOC_PATH, "## Bounding what the sync can write")
    recommended = cast(list[str], getting_started["allowed_destinations"])
    assert recommended == cast(list[str], sync_doc["allowed_destinations"])

    manifest = cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text()))
    targets = cast(list[dict[str, Any]], manifest["targets"])
    compiled = tuple(sync_engine.glob_to_regex(pattern) for pattern in recommended)
    uncovered = [
        cast(str, target["destination"])
        for target in targets
        if not sync_engine.path_matches_any(cast(str, target["destination"]), compiled)
    ]
    assert uncovered == []
