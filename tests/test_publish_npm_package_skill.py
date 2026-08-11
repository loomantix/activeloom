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
    handlers: list[object] = []

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

    def fake_build_opener(*passed: object) -> RedirectingOpener:
        handlers.extend(passed)
        return RedirectingOpener()

    monkeypatch.setattr(
        published_package_verifier.urllib.request, "build_opener", fake_build_opener
    )
    with pytest.raises(RuntimeError, match="outside the configured origin"):
        published_package_verifier.download_from_registry(
            "https://registry.example/package.tgz", "https://registry.example", 1024
        )
    assert calls == ["https://registry.example/package.tgz"]
    # The opener must be built with a handler that refuses automatic redirects;
    # without this assertion the origin check could be bypassed by urllib
    # following the redirect before the helper ever inspects the new URL.
    redirect_handlers = [
        handler
        for handler in handlers
        if hasattr(handler, "redirect_request")
        and handler.redirect_request(None, None, 302, "Found", Message(), "https://elsewhere")
        is None
    ]
    assert redirect_handlers


def test_registry_download_is_bounded(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizedResponse:
        def __enter__(self) -> OversizedResponse:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self, amount: int) -> bytes:
            return b"x" * amount

    class OversizedOpener:
        def open(self, url: str, timeout: int) -> OversizedResponse:
            return OversizedResponse()

    monkeypatch.setattr(
        published_package_verifier.urllib.request,
        "build_opener",
        lambda *_handlers: OversizedOpener(),
    )
    with pytest.raises(RuntimeError, match="verification bound"):
        published_package_verifier.download_from_registry(
            "https://registry.example/package.tgz", "https://registry.example", 16
        )


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
    repository_dir = tmp_path / "checkout"
    repository_dir.mkdir()
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
    seen_env: list[object] = []

    def fake_run(
        command: list[str], *_args: object, env: object = None, **_kwargs: object
    ) -> object:
        seen_env.append(env)
        stdout = ""
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            stdout = str(repository_dir)
        elif command[:2] == ["npm", "view"]:
            stdout = json.dumps(metadata)
        elif command[:3] == ["npm", "audit", "signatures"]:
            stdout = json.dumps(
                {
                    "invalid": [],
                    "missing": [],
                    "verified": [{"name": "example-package", "version": "1.2.3"}],
                }
            )
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
            "--source-repository",
            "https://github.com/owner/repo",
            "--tag",
            "v1.2.3",
            "--commit",
            "a" * 40,
            "--repository-dir",
            str(repository_dir),
            "--signer-fingerprint",
            "C" * 40,
            "--output",
            str(output),
        ],
    )
    assert published_package_verifier.main() == 0
    result = json.loads(output.read_text())
    assert result["declared_registry_signatures"] == 1
    assert result["verified_attestations"] == 0
    assert result["byte_identical"] is True
    assert result["provenance"] == {"status": "unavailable"}
    assert result["release_tag"] == tag_result
    # Every npm subprocess must receive the scrubbed environment; without this
    # assertion, dropping env= from a call site is invisible.
    npm_envs = [env for env in seen_env if isinstance(env, dict)]
    assert npm_envs
    for env in npm_envs:
        assert "NPM_CONFIG_USERCONFIG" in env
        assert "NPM_CONFIG_CACHE" in env


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
        elif command[:3] == ["git", "remote", "get-url"]:
            stdout = "git@github.com:Owner/Repo.git"
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
    # Owner/Repo vs owner/repo also proves the comparison is case-insensitive.
    assert published_package_verifier.verify_release_tag(
        tmp_path, "origin", "v1.2.3", commit, fingerprint, "https://github.com/owner/repo"
    ) == {"object": tag_object, "signer_fingerprint": fingerprint, "target": commit}


def test_release_tag_verification_rejects_unrelated_repository(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Tag facts must not be collected from a clone of a different repository."""

    def fake_run(command: list[str], *_args: object, **_kwargs: object) -> object:
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            stdout = str(tmp_path)
        elif command[:3] == ["git", "remote", "get-url"]:
            stdout = "https://github.com/attacker/lookalike.git"
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(published_package_verifier, "run", fake_run)
    with pytest.raises(RuntimeError, match="does not match the expected source repository"):
        published_package_verifier.verify_release_tag(
            tmp_path,
            "origin",
            "v1.2.3",
            "a" * 40,
            "C" * 40,
            "https://github.com/owner/repo",
        )


def test_release_tag_verification_rejects_unapproved_signer(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Deleting the fingerprint membership check must fail this test."""

    def fake_run(command: list[str], *_args: object, **_kwargs: object) -> object:
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            stdout = str(tmp_path)
        elif command[:3] == ["git", "remote", "get-url"]:
            stdout = "https://github.com/owner/repo.git"
        elif command[:3] == ["git", "cat-file", "-t"]:
            stdout = "tag"
        elif command[:2] == ["git", "verify-tag"]:
            return subprocess.CompletedProcess(
                command, 0, "", "[GNUPG:] VALIDSIG " + "D" * 40 + " 0 0 0"
            )
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(published_package_verifier, "run", fake_run)
    with pytest.raises(RuntimeError, match="does not match approved fingerprint"):
        published_package_verifier.verify_release_tag(
            tmp_path,
            "origin",
            "v1.2.3",
            "a" * 40,
            "C" * 40,
            "https://github.com/owner/repo",
        )


def _self_signed_der(tmp_path: Path, identity: str) -> bytes:
    """Build a real DER certificate carrying a SAN URI, for the parser tests."""
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.der"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-outform",
            "DER",
            "-days",
            "1",
            "-subj",
            "/CN=test",
            "-addext",
            f"subjectAltName=URI:{identity}",
        ],
        check=True,
        capture_output=True,
    )
    return cert.read_bytes()


@pytest.mark.parametrize("layout", ["certificate", "x509CertificateChain"])
def test_certificate_identities_supports_both_bundle_layouts(
    published_package_verifier: ModuleType,
    tmp_path: Path,
    layout: str,
) -> None:
    """npm currently emits v0.2 bundles using x509CertificateChain, not certificate."""
    identity = "https://github.com/owner/repo/.github/workflows/publish.yml@refs/tags/v1.2.3"
    encoded = base64.b64encode(_self_signed_der(tmp_path, identity)).decode("ascii")
    if layout == "certificate":
        material: dict[str, Any] = {"certificate": {"rawBytes": encoded}}
    else:
        material = {"x509CertificateChain": {"certificates": [{"rawBytes": encoded}]}}
    assert published_package_verifier.certificate_identities(
        {"verificationMaterial": material}
    ) == {identity}


def test_certificate_identities_rejects_unknown_bundle_layout(
    published_package_verifier: ModuleType,
) -> None:
    with pytest.raises(RuntimeError, match="supported layout"):
        published_package_verifier.certificate_identities({"verificationMaterial": {}})


def test_unavailable_provenance_rejects_present_attestation(
    published_package_verifier: ModuleType,
) -> None:
    """A present-but-unverified attestation must not be downgraded to 'unavailable'."""
    entry = {
        "name": "example-package",
        "version": "1.2.3",
        "attestationBundles": [
            {"predicateType": published_package_verifier.SLSA_PROVENANCE_V1, "bundle": {}}
        ],
    }
    with pytest.raises(RuntimeError, match="rerun with --provenance required"):
        published_package_verifier.assert_no_provenance(entry)


def test_target_verification_requires_the_exact_verified_entry(
    published_package_verifier: ModuleType,
) -> None:
    audit = {"invalid": [], "missing": [], "verified": [{"name": "other", "version": "1.2.3"}]}
    with pytest.raises(RuntimeError, match="did not verify the exact target"):
        published_package_verifier.target_verification(audit, "example-package", "1.2.3")


@pytest.mark.parametrize("field", ["invalid", "missing", "verified"])
def test_target_verification_rejects_missing_audit_lists(
    published_package_verifier: ModuleType,
    field: str,
) -> None:
    """An absent key must fail closed rather than read as a clean result."""
    audit: dict[str, Any] = {
        "invalid": [],
        "missing": [],
        "verified": [{"name": "example-package", "version": "1.2.3"}],
    }
    del audit[field]
    with pytest.raises(RuntimeError, match=f"does not contain a {field} list"):
        published_package_verifier.target_verification(audit, "example-package", "1.2.3")


def test_credential_free_env_drops_tls_and_injection_variables(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scrubbed = (
        "NODE_OPTIONS",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "HTTPS_PROXY",
        "NPM_CONFIG_OTP",
        "NODE_AUTH_TOKEN",
    )
    for name in scrubbed:
        monkeypatch.setenv(name, "attacker-controlled")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = published_package_verifier.credential_free_npm_env(tmp_path)
    for name in scrubbed:
        assert name not in env
    # The allowlist must still yield a usable environment.
    assert env["PATH"] == "/usr/bin"
    assert env["NPM_CONFIG_CACHE"].startswith(str(tmp_path))


def test_preflight_rejects_tar_entry_outside_package_root(
    release_preflight: ModuleType,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "package.tgz"
    manifest = json.dumps({"name": "example-package", "version": "1.2.3"}).encode()
    with tarfile.open(artifact, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        stray = tarfile.TarInfo("elsewhere/extra.txt")
        stray.size = 0
        archive.addfile(stray, io.BytesIO(b""))
    with pytest.raises(RuntimeError, match="outside the package root"):
        release_preflight.inspect_tarball(artifact, "example-package", "1.2.3")


def test_preflight_rejects_publish_config_access_mismatch(
    release_preflight: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "example-package",
                "version": "1.2.3",
                "publishConfig": {"access": "restricted"},
                "repository": "https://github.com/owner/repo",
            }
        ),
        encoding="utf-8",
    )
    artifact = tmp_path / "package.tgz"
    _tarball(artifact)
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
        ],
    )
    with pytest.raises(RuntimeError, match="publishConfig.access is restricted"):
        release_preflight.main()


def test_github_repository_comparison_is_case_insensitive(
    release_preflight: ModuleType,
    published_package_verifier: ModuleType,
) -> None:
    assert release_preflight.github_repository(
        "git@github.com:Owner/Repo.git"
    ) == release_preflight.github_repository("https://github.com/owner/repo")
    assert published_package_verifier.github_repository(
        "git+https://github.com/OWNER/REPO.git"
    ) == published_package_verifier.github_repository("https://github.com/owner/repo")


def test_subprocess_helpers_pass_the_timeout_constant(
    release_preflight: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Removing timeout= from the call site must fail this test."""
    seen: dict[str, Any] = {}

    def capture(*_args: object, **kwargs: Any) -> object:
        seen.update(kwargs)
        return subprocess.CompletedProcess(["git"], 0, "", "")

    monkeypatch.setattr(subprocess, "run", capture)
    release_preflight.run(["git", "status"], tmp_path)
    assert seen["timeout"] == release_preflight.COMMAND_TIMEOUT_SECONDS
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
