"""Regression tests for the deterministic npm release helpers."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import urllib.error
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / ".codex/skills/publish-npm-package/scripts"


def _load_script(name: str) -> ModuleType:
    path = SCRIPTS / name
    module_name = name.removesuffix(".py").replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def release_preflight() -> ModuleType:
    return _load_script("release-preflight.py")


@pytest.fixture(scope="session")
def published_package_verifier() -> ModuleType:
    return _load_script("verify-published-package.py")


def _tarball(path: Path, name: str = "example-package", version: str = "1.2.3") -> bytes:
    manifest = json.dumps({"name": name, "version": version}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
    return path.read_bytes()


def _slsa_entry(
    *,
    commit: str,
    subjects: list[dict[str, Any]] | None = None,
    dependencies: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    payload = {
        "subject": subjects
        or [
            {
                "name": "pkg:npm/%40example/example-package@1.2.3",
                "digest": {"sha512": "artifact-digest"},
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1",
                "externalParameters": {
                    "workflow": {
                        "repository": "https://github.com/example/packages",
                        "path": ".github/workflows/publish.yml",
                        "ref": "refs/tags/example-package-v1.2.3",
                    }
                },
                "resolvedDependencies": dependencies
                or [
                    {
                        "uri": "git+https://github.com/example/packages@refs/tags/example-package-v1.2.3",
                        "digest": {"gitCommit": commit},
                    }
                ],
            },
            "runDetails": {
                "builder": {"id": "https://github.com/actions/runner/github-hosted"}
            },
        },
    }
    return {
        "predicateType": "https://slsa.dev/provenance/v1",
        "bundle": {
            "dsseEnvelope": {
                "payload": base64.b64encode(json.dumps(payload).encode()).decode()
            }
        },
    }


def test_release_preflight_inspects_identity_and_emits_stable_digests(
    release_preflight: ModuleType, tmp_path: Path
) -> None:
    artifact = tmp_path / "package.tgz"
    data = _tarball(artifact)

    assert release_preflight.inspect_tarball(artifact, "example-package", "1.2.3") == {
        "entries": 1,
        "name": "example-package",
        "version": "1.2.3",
    }
    result = release_preflight.digest(artifact)
    assert result["bytes"] == str(len(data))
    assert len(result["sha1"]) == 40
    assert len(result["sha256"]) == 64
    assert len(result["sha512"]) == 128
    assert result["integrity"].startswith("sha512-")


def test_release_preflight_rejects_embedded_identity_mismatch(
    release_preflight: ModuleType, tmp_path: Path
) -> None:
    artifact = tmp_path / "package.tgz"
    _tarball(artifact)

    with pytest.raises(RuntimeError, match="identity mismatch"):
        release_preflight.inspect_tarball(artifact, "other-package", "1.2.3")


def test_release_preflight_normalizes_supported_github_repository_urls(
    release_preflight: ModuleType,
) -> None:
    assert (
        release_preflight.github_repository("git+https://github.com/example/example-package.git")
        == "github.com/example/example-package"
    )
    assert (
        release_preflight.github_repository("git@github.com:example/example-package.git")
        == "github.com/example/example-package"
    )


def test_helpers_remove_ambient_npm_credentials(
    release_preflight: ModuleType,
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NODE_AUTH_TOKEN", "do-not-use")
    monkeypatch.setenv("NPM_TOKEN", "do-not-use")
    monkeypatch.setenv("NPM_OTP", "do-not-use")
    monkeypatch.setenv("NPM_CONFIG__AUTH", "do-not-use")
    monkeypatch.setenv("NPM_CONFIG_CAFILE", "/example/ca.pem")
    monkeypatch.setenv("NPM_CONFIG_CERT", "do-not-use")
    monkeypatch.setenv("NPM_CONFIG_KEY", "do-not-use")

    for module in (release_preflight, published_package_verifier):
        config_dir = tmp_path / module.__name__
        config_dir.mkdir()
        env = module.credential_free_npm_env(config_dir)
        assert "NODE_AUTH_TOKEN" not in env
        assert "NPM_TOKEN" not in env
        assert "NPM_OTP" not in env
        assert "NPM_CONFIG__AUTH" not in env
        assert "NPM_CONFIG_CAFILE" not in env
        assert "NPM_CONFIG_CERT" not in env
        assert "NPM_CONFIG_KEY" not in env
        assert Path(env["NPM_CONFIG_USERCONFIG"]).read_text() == ""
        assert Path(env["NPM_CONFIG_GLOBALCONFIG"]).read_text() == ""
        assert env["NPM_CONFIG_USERCONFIG"] != env["NPM_CONFIG_GLOBALCONFIG"]


def test_signature_audit_selects_exact_target(published_package_verifier: ModuleType) -> None:
    audit = {
        "invalid": [],
        "missing": [],
        "verified": [
            {
                "name": "example-package",
                "version": "1.2.3",
            },
            {"name": "signed-dependency", "version": "9.9.9"},
        ],
    }

    assert published_package_verifier.target_verification(
        audit, "example-package", "1.2.3"
    ) == audit["verified"][0]


def test_signature_audit_does_not_accept_a_verified_dependency_for_target(
    published_package_verifier: ModuleType,
) -> None:
    audit = {
        "invalid": [],
        "missing": [],
        "verified": [{"name": "signed-dependency", "version": "9.9.9"}],
    }

    with pytest.raises(RuntimeError, match="exact target"):
        published_package_verifier.target_verification(audit, "example-package", "1.2.3")


def test_slsa_verification_binds_artifact_source_workflow_tag_and_commit(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40
    identity = (
        "https://github.com/example/packages/.github/workflows/publish.yml"
        "@refs/tags/example-package-v1.2.3"
    )
    monkeypatch.setattr(
        published_package_verifier, "certificate_identities", lambda _bundle: {identity}
    )
    entry = {"attestationBundles": [_slsa_entry(commit=commit)]}

    assert published_package_verifier.verify_slsa(
        entry,
        artifact_sha512="artifact-digest",
        package="@example/example-package",
        version="1.2.3",
        repository="https://github.com/example/packages.git",
        workflow_path=".github/workflows/publish.yml",
        tag="example-package-v1.2.3",
        commit=commit,
    ) == {
        "builder": "https://github.com/actions/runner/github-hosted",
        "build_type": "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1",
        "certificate_identity": identity,
        "commit": commit,
        "matching_bundles": 1,
        "predicate_type": "https://slsa.dev/provenance/v1",
        "repository": "https://github.com/example/packages",
        "tag": "example-package-v1.2.3",
        "workflow_path": ".github/workflows/publish.yml",
    }


def test_slsa_verification_rejects_split_subject_and_unrelated_dependency(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40
    identity = (
        "https://github.com/example/packages/.github/workflows/publish.yml"
        "@refs/tags/example-package-v1.2.3"
    )
    monkeypatch.setattr(
        published_package_verifier, "certificate_identities", lambda _bundle: {identity}
    )
    split_subjects = [
        {"name": "pkg:npm/wrong@1.2.3", "digest": {"sha512": "artifact-digest"}},
        {
            "name": "pkg:npm/%40example/example-package@1.2.3",
            "digest": {"sha512": "wrong-digest"},
        },
    ]
    unrelated_dependencies = [
        {
            "uri": "git+https://github.com/attacker/repository@refs/tags/example-package-v1.2.3",
            "digest": {"gitCommit": commit},
        }
    ]

    with pytest.raises(RuntimeError, match="target package identity"):
        published_package_verifier.verify_slsa(
            {"attestationBundles": [_slsa_entry(commit=commit, subjects=split_subjects)]},
            artifact_sha512="artifact-digest",
            package="@example/example-package",
            version="1.2.3",
            repository="https://github.com/example/packages.git",
            workflow_path=".github/workflows/publish.yml",
            tag="example-package-v1.2.3",
            commit=commit,
        )

    with pytest.raises(RuntimeError, match="repository, tag, and release commit"):
        published_package_verifier.verify_slsa(
            {
                "attestationBundles": [
                    _slsa_entry(commit=commit, dependencies=unrelated_dependencies)
                ]
            },
            artifact_sha512="artifact-digest",
            package="@example/example-package",
            version="1.2.3",
            repository="https://github.com/example/packages.git",
            workflow_path=".github/workflows/publish.yml",
            tag="example-package-v1.2.3",
            commit=commit,
        )


def test_slsa_verification_checks_all_candidate_bundles(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40
    identity = (
        "https://github.com/example/packages/.github/workflows/publish.yml"
        "@refs/tags/example-package-v1.2.3"
    )
    monkeypatch.setattr(
        published_package_verifier, "certificate_identities", lambda _bundle: {identity}
    )
    wrong = _slsa_entry(
        commit=commit,
        subjects=[{"name": "pkg:npm/wrong@1.2.3", "digest": {"sha512": "wrong"}}],
    )
    result = published_package_verifier.verify_slsa(
        {"attestationBundles": [wrong, _slsa_entry(commit=commit)]},
        artifact_sha512="artifact-digest",
        package="@example/example-package",
        version="1.2.3",
        repository="https://github.com/example/packages.git",
        workflow_path=".github/workflows/publish.yml",
        tag="example-package-v1.2.3",
        commit=commit,
    )
    assert result["matching_bundles"] == 1


def test_verifier_hashes_match_npm_integrity_shape(
    published_package_verifier: ModuleType, tmp_path: Path
) -> None:
    artifact = tmp_path / "package.tgz"
    data = _tarball(artifact)

    result = published_package_verifier.hashes(data)
    assert result["bytes"] == str(len(data))
    assert result["integrity"].startswith("sha512-")
    assert len(result["sha512"]) == 128


def test_release_preflight_rejects_special_tar_entries(
    release_preflight: ModuleType, tmp_path: Path
) -> None:
    artifact = tmp_path / "package.tgz"
    with tarfile.open(artifact, "w:gz") as archive:
        link = tarfile.TarInfo("package/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)

    with pytest.raises(RuntimeError, match="unsafe tar entry type"):
        release_preflight.inspect_tarball(artifact, "example-package", "1.2.3")


def test_registry_absence_requires_structured_e404(
    release_preflight: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def result(code: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["npm", "view"], 1, json.dumps({"error": {"code": code}}), ""
        )

    monkeypatch.setattr(release_preflight, "run", lambda *_args, **_kwargs: result("E404"))
    release_preflight.registry_version_absent("example-package", "1.2.3", "https://npm.test")

    monkeypatch.setattr(release_preflight, "run", lambda *_args, **_kwargs: result("E401"))
    with pytest.raises(RuntimeError, match="returned E401"):
        release_preflight.registry_version_absent(
            "example-package", "1.2.3-e404", "https://npm.test"
        )


def test_subprocess_helpers_timeout_fail_closed(
    release_preflight: ModuleType,
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["git", "status"], 300)

    monkeypatch.setattr(subprocess, "run", timeout)
    for module in (release_preflight, published_package_verifier):
        with pytest.raises(RuntimeError, match="command timed out"):
            module.run(["git", "status"], tmp_path)


def test_registry_redirect_is_rejected_before_second_request(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class RedirectingOpener:
        def open(self, url: str, timeout: int) -> None:
            calls.append(url)
            headers = Message()
            headers["Location"] = "https://registry.example:4444/private"
            raise urllib.error.HTTPError(
                url,
                302,
                "Found",
                headers,
                None,
            )

    monkeypatch.setattr(
        published_package_verifier.urllib.request,
        "build_opener",
        lambda *_handlers: RedirectingOpener(),
    )
    with pytest.raises(RuntimeError, match="outside the configured origin"):
        published_package_verifier.download_from_registry(
            "https://registry.example/package.tgz", "https://registry.example"
        )
    assert calls == ["https://registry.example/package.tgz"]


def test_main_entry_points_reject_artifact_output_alias(
    release_preflight: ModuleType,
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "package.tgz"
    _tarball(artifact)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release-preflight.py",
            "--package-dir",
            str(tmp_path),
            "--artifact",
            str(artifact),
            "--tag",
            "v1.2.3",
            "--access",
            "public",
            "--output",
            str(artifact),
        ],
    )
    with pytest.raises(RuntimeError, match="must not overwrite"):
        release_preflight.main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify-published-package.py",
            "--package",
            "example-package",
            "--version",
            "1.2.3",
            "--artifact",
            str(artifact),
            "--access",
            "public",
            "--provenance",
            "unavailable",
            "--output",
            str(artifact),
        ],
    )
    with pytest.raises(RuntimeError, match="must not overwrite"):
        published_package_verifier.main()


def test_release_preflight_main_prepare_success(
    release_preflight: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    package_dir = repo / "package"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "example-package",
                "version": "1.2.3",
                "repository": "https://github.com/example/packages.git",
            }
        )
    )
    artifact = tmp_path / "package.tgz"
    _tarball(artifact)
    output = tmp_path / "preflight.json"
    commit = "a" * 40

    def fake_run(command: list[str], *_args: object, **_kwargs: object) -> object:
        key = tuple(command[:3])
        if key == ("git", "rev-parse", "--show-toplevel"):
            stdout = str(repo)
        elif key == ("git", "remote", "get-url"):
            stdout = "https://github.com/example/packages.git"
        elif key == ("git", "rev-parse", "HEAD"):
            stdout = commit
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 1 if command[1:3] == ["show-ref", "--verify"] else 0, stdout, "")

    monkeypatch.setattr(release_preflight, "run", fake_run)
    monkeypatch.setattr(release_preflight, "remote_tag_object", lambda *_args: None)
    monkeypatch.setattr(release_preflight, "registry_version_absent", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release-preflight.py",
            "--package-dir",
            str(package_dir),
            "--artifact",
            str(artifact),
            "--tag",
            "v1.2.3",
            "--access",
            "public",
            "--output",
            str(output),
        ],
    )
    assert release_preflight.main() == 0
    assert json.loads(output.read_text())["commit"] == commit


def test_verifier_main_unavailable_provenance_success(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "package.tgz"
    data = _tarball(artifact)
    digests = published_package_verifier.hashes(data)
    metadata = {
        "dist": {
            "integrity": digests["integrity"],
            "shasum": digests["sha1"],
            "signatures": [{"keyid": "test", "sig": "test"}],
            "tarball": "https://registry.example/package.tgz",
        }
    }

    def fake_run(command: list[str], *_args: object, **_kwargs: object) -> object:
        stdout = ""
        if command[:2] == ["npm", "view"]:
            stdout = json.dumps(metadata)
        elif command[:3] == ["npm", "audit", "signatures"]:
            stdout = json.dumps({"invalid": [], "missing": [], "verified": []})
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(published_package_verifier, "run", fake_run)
    monkeypatch.setattr(
        published_package_verifier, "download_from_registry", lambda *_args: data
    )
    tag_result = {
        "object": "b" * 40,
        "signer_fingerprint": "C" * 40,
        "target": "a" * 40,
    }
    monkeypatch.setattr(
        published_package_verifier, "verify_release_tag", lambda *_args: tag_result
    )
    output = tmp_path / "verification.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify-published-package.py",
            "--package",
            "example-package",
            "--version",
            "1.2.3",
            "--artifact",
            str(artifact),
            "--registry",
            "https://registry.example",
            "--access",
            "public",
            "--provenance",
            "unavailable",
            "--tag",
            "v1.2.3",
            "--commit",
            "a" * 40,
            "--repository-dir",
            str(tmp_path),
            "--signer-fingerprint",
            "C" * 40,
            "--output",
            str(output),
        ],
    )
    assert published_package_verifier.main() == 0
    result = json.loads(output.read_text())
    assert result["verified_registry_signatures"] == 1
    assert result["verified_attestations"] == 0
    assert result["release_tag"] == tag_result


def test_release_tag_verification_binds_signer_remote_object_and_commit(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commit = "a" * 40
    tag_object = "b" * 40
    fingerprint = "C" * 40

    def fake_run(command: list[str], *_args: object, **_kwargs: object) -> object:
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            stdout = str(tmp_path)
        elif command[:3] == ["git", "cat-file", "-t"]:
            stdout = "tag"
        elif command[:2] == ["git", "verify-tag"]:
            stdout = ""
        elif command[:3] == ["git", "rev-list", "-n"]:
            stdout = commit
        elif command[:2] == ["git", "rev-parse"]:
            stdout = tag_object
        elif command[:2] == ["git", "ls-remote"]:
            stdout = f"{tag_object}\trefs/tags/v1.2.3"
        else:
            raise AssertionError(command)
        stderr = f"[GNUPG:] VALIDSIG {fingerprint} 0 0 0" if command[:2] == ["git", "verify-tag"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, stderr)

    monkeypatch.setattr(published_package_verifier, "run", fake_run)
    assert published_package_verifier.verify_release_tag(
        tmp_path, "origin", "v1.2.3", commit, fingerprint
    ) == {"object": tag_object, "signer_fingerprint": fingerprint, "target": commit}
