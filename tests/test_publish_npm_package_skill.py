"""Regression tests for the deterministic npm release helpers."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / ".codex/skills/publish-npm-package/scripts"


def _load_script(name: str) -> ModuleType:
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tarball(path: Path, name: str = "example-package", version: str = "1.2.3") -> bytes:
    manifest = json.dumps({"name": name, "version": version}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
    return path.read_bytes()


def test_release_preflight_inspects_identity_and_emits_stable_digests(tmp_path: Path) -> None:
    preflight = _load_script("release-preflight.py")
    artifact = tmp_path / "package.tgz"
    data = _tarball(artifact)

    assert preflight.inspect_tarball(artifact, "example-package", "1.2.3") == {
        "entries": 1,
        "name": "example-package",
        "version": "1.2.3",
    }
    result = preflight.digest(artifact)
    assert result["bytes"] == str(len(data))
    assert len(result["sha1"]) == 40
    assert len(result["sha256"]) == 64
    assert len(result["sha512"]) == 128
    assert result["integrity"].startswith("sha512-")


def test_release_preflight_rejects_embedded_identity_mismatch(tmp_path: Path) -> None:
    preflight = _load_script("release-preflight.py")
    artifact = tmp_path / "package.tgz"
    _tarball(artifact)

    try:
        preflight.inspect_tarball(artifact, "other-package", "1.2.3")
    except RuntimeError as exc:
        assert "identity mismatch" in str(exc)
    else:
        raise AssertionError("identity mismatch must fail closed")


def test_release_preflight_normalizes_supported_github_repository_urls() -> None:
    preflight = _load_script("release-preflight.py")

    assert (
        preflight.github_repository("git+https://github.com/example/example-package.git")
        == "github.com/example/example-package"
    )


def test_helpers_remove_ambient_npm_credentials(monkeypatch, tmp_path: Path) -> None:
    preflight = _load_script("release-preflight.py")
    verifier = _load_script("verify-published-package.py")
    monkeypatch.setenv("NODE_AUTH_TOKEN", "do-not-use")
    monkeypatch.setenv("NPM_TOKEN", "do-not-use")
    monkeypatch.setenv("NPM_CONFIG__AUTH", "do-not-use")
    monkeypatch.setenv("NPM_CONFIG_CAFILE", "/example/ca.pem")

    for module in (preflight, verifier):
        config_dir = tmp_path / module.__name__
        config_dir.mkdir()
        env = module.credential_free_npm_env(config_dir)
        assert "NODE_AUTH_TOKEN" not in env
        assert "NPM_TOKEN" not in env
        assert "NPM_CONFIG__AUTH" not in env
        assert env["NPM_CONFIG_CAFILE"] == "/example/ca.pem"
        assert Path(env["NPM_CONFIG_USERCONFIG"]).read_text() == ""
        assert Path(env["NPM_CONFIG_GLOBALCONFIG"]).read_text() == ""
        assert env["NPM_CONFIG_USERCONFIG"] != env["NPM_CONFIG_GLOBALCONFIG"]
    assert (
        preflight.github_repository("git@github.com:example/example-package.git")
        == "github.com/example/example-package"
    )


def test_signature_audit_requires_exact_target_and_slsa_predicate() -> None:
    verifier = _load_script("verify-published-package.py")
    audit = {
        "invalid": [],
        "missing": [],
        "verified": [
            {
                "name": "example-package",
                "version": "1.2.3",
                "attestations": {
                    "provenance": {"predicateType": "https://slsa.dev/provenance/v1"}
                },
            },
            {"name": "signed-dependency", "version": "9.9.9"},
        ],
    }

    assert verifier.target_verification(audit, "example-package", "1.2.3") == audit["verified"][0]


def test_signature_audit_does_not_accept_a_verified_dependency_for_target() -> None:
    verifier = _load_script("verify-published-package.py")
    audit = {
        "invalid": [],
        "missing": [],
        "verified": [{"name": "signed-dependency", "version": "9.9.9"}],
    }

    try:
        verifier.target_verification(audit, "example-package", "1.2.3")
    except RuntimeError as exc:
        assert "exact target" in str(exc)
    else:
        raise AssertionError("a verified dependency must not satisfy target verification")


def test_slsa_verification_binds_artifact_source_workflow_tag_and_commit() -> None:
    verifier = _load_script("verify-published-package.py")
    commit = "a" * 40
    payload = {
        "subject": [
            {
                "name": "pkg:npm/%40example/example-package@1.2.3",
                "digest": {"sha512": "artifact-digest"},
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "externalParameters": {
                    "workflow": {
                        "repository": "https://github.com/example/packages",
                        "path": ".github/workflows/publish.yml",
                        "ref": "refs/tags/example-package-v1.2.3",
                    }
                },
                "resolvedDependencies": [{"digest": {"gitCommit": commit}}],
            }
        },
    }
    entry = {
        "attestationBundles": [
            {
                "predicateType": "https://slsa.dev/provenance/v1",
                "bundle": {
                    "dsseEnvelope": {
                        "payload": base64.b64encode(json.dumps(payload).encode()).decode()
                    }
                },
            }
        ]
    }

    assert verifier.verify_slsa(
        entry,
        artifact_sha512="artifact-digest",
        package="@example/example-package",
        version="1.2.3",
        repository="https://github.com/example/packages.git",
        workflow_path=".github/workflows/publish.yml",
        tag="example-package-v1.2.3",
        commit=commit,
    ) == {
        "commit": commit,
        "predicate_type": "https://slsa.dev/provenance/v1",
        "repository": "https://github.com/example/packages",
        "tag": "example-package-v1.2.3",
        "workflow_path": ".github/workflows/publish.yml",
    }


def test_verifier_hashes_match_npm_integrity_shape(tmp_path: Path) -> None:
    verifier = _load_script("verify-published-package.py")
    artifact = tmp_path / "package.tgz"
    data = _tarball(artifact)

    result = verifier.hashes(data)
    assert result["bytes"] == str(len(data))
    assert result["integrity"].startswith("sha512-")
    assert len(result["sha512"]) == 128
