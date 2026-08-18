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


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
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
    # A second, correctly signed tag deliberately dated in the past. This is
    # what a replayed release looks like: the signature is genuine and still
    # verifies, but the object is stale. The tagger date comes from
    # GIT_COMMITTER_DATE and is inside the signed payload.
    _git(
        repo,
        "tag",
        "-s",
        "old-release",
        "-m",
        "previous signed release",
        env={"GIT_COMMITTER_DATE": "2020-01-01T00:00:00+0000"},
    )
    # Isolate this fixture from a developer-level `tag.gpgsign=true` setting.
    _git(repo, "-c", "tag.gpgSign=false", "tag", "-a", "unsigned", "-m", "unsigned fixture")
    return repo, f"maintainer@example.com {public_key}"


def _consumer_dir(repo: Path) -> Path:
    """Stand-in for `GITHUB_WORKSPACE` — where the gate keeps its replay state."""
    return repo.parent / "consumer"


def _state_file(repo: Path) -> Path:
    return _consumer_dir(repo) / ".github" / "sync-upstream-state"


def _step_output(repo: Path) -> str:
    path = repo.parent / "step-output"
    return path.read_text() if path.is_file() else ""


def _run_verification(
    repo: Path,
    *,
    ref_kind: str,
    upstream_ref: str,
    allowed_signers: str,
) -> subprocess.CompletedProcess[str]:
    workspace = _consumer_dir(repo)
    workspace.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "ALLOWED_SIGNERS": allowed_signers,
            "REF_KIND": ref_kind,
            "UPSTREAM_DIR": str(repo),
            "UPSTREAM_REPO": "fixture/upstream",
            "UPSTREAM_REF": upstream_ref,
            # Redirect the Actions-provided sinks into the fixture tree. Without
            # this the gate would write its replay state into *this* repo when
            # the suite runs locally.
            "GITHUB_WORKSPACE": str(workspace),
            "GITHUB_OUTPUT": str(repo.parent / "step-output"),
            "GITHUB_STEP_SUMMARY": str(repo.parent / "step-summary"),
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


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")
def test_signature_gate_records_the_accepted_tag_as_a_high_water_mark(
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
    assert "verified=true" in _step_output(repo)
    recorded = _state_file(repo).read_text()
    assert "last_verified_tag_timestamp=" in recorded
    assert "last_verified_tag_object=" in recorded


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")
def test_signature_gate_rejects_replay_of_an_older_signed_tag(
    signed_tag_repo: tuple[Path, str],
) -> None:
    """A stale tag object stays validly signed forever.

    Moving the ref back onto one needs push access but no signing key, so
    `git verify-tag` alone cannot tell a rollback from a release.
    """
    repo, allowed_signers = signed_tag_repo
    # Accept the current release first, establishing the high-water mark.
    _run_verification(
        repo,
        ref_kind="tag",
        upstream_ref="sync-v1",
        allowed_signers=allowed_signers,
    )
    replayed = _run_verification(
        repo,
        ref_kind="tag",
        upstream_ref="old-release",
        allowed_signers=allowed_signers,
    )
    assert replayed.returncode != 0
    assert "is OLDER than the last tag this repo accepted" in replayed.stderr


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")
def test_replay_guard_still_allows_a_deliberate_re_release(
    signed_tag_repo: tuple[Path, str],
) -> None:
    """A genuine revert mints a fresh tag object dated now, so it must pass."""
    repo, allowed_signers = signed_tag_repo
    _state_file(repo).parent.mkdir(parents=True, exist_ok=True)
    _state_file(repo).write_text("last_verified_tag_timestamp=1577836800\n")
    result = _run_verification(
        repo,
        ref_kind="tag",
        upstream_ref="sync-v1",
        allowed_signers=allowed_signers,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")
def test_unverified_run_is_marked_for_downstream_steps(
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
    assert "verified=false" in _step_output(repo)
    assert "NOT signature-verified" in (repo.parent / "step-summary").read_text()


def test_checkout_does_not_persist_the_app_token() -> None:
    """The token would otherwise sit in `.git/config` of the tree upstream code runs in."""
    step = next(
        item for item in _workflow_steps() if item.get("name") == "Checkout consumer repo"
    )
    assert cast(dict[str, Any], step["with"])["persist-credentials"] is False


def test_unverified_syncs_are_flagged_in_the_pr_body() -> None:
    """A `::warning::` in a green job is not a control; the PR body is read."""
    step = next(
        item
        for item in _workflow_steps()
        if cast(str, item.get("name", "")).startswith("Create signed commit")
    )
    assert cast(dict[str, Any], step["env"])["TAG_VERIFIED"] == "${{ steps.verify.outputs.verified }}"
    run = cast(str, step["run"])
    assert 'if [ "${TAG_VERIFIED:-}" != "true" ]' in run
    assert "not signature-verified" in run


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
