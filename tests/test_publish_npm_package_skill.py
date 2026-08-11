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
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / ".codex/skills/publish-npm-package/scripts"
SKILL = Path(__file__).resolve().parent.parent / ".codex/skills/publish-npm-package/SKILL.md"


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


def _tarball(
    path: Path,
    name: str = "example-package",
    version: str = "1.2.3",
    extra_manifest: dict[str, Any] | None = None,
) -> bytes:
    package_json: dict[str, Any] = {"name": name, "version": version}
    package_json.update(extra_manifest or {})
    manifest = json.dumps(package_json).encode()
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


def _slsa_payload(entry: dict[str, object]) -> dict[str, Any]:
    bundle = entry["bundle"]
    assert isinstance(bundle, dict)
    envelope = bundle["dsseEnvelope"]
    assert isinstance(envelope, dict)
    payload = json.loads(base64.b64decode(str(envelope["payload"])))
    assert isinstance(payload, dict)
    return payload


def _replace_slsa_payload(entry: dict[str, object], payload: dict[str, Any]) -> None:
    bundle = entry["bundle"]
    assert isinstance(bundle, dict)
    envelope = bundle["dsseEnvelope"]
    assert isinstance(envelope, dict)
    envelope["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()


def test_skill_isolates_oidc_publish_authority() -> None:
    guidance = SKILL.read_text(encoding="utf-8")

    assert "Grant `id-token: write` only to the publish" in guidance
    assert "must not check out the repository" in guidance
    assert "exact tarball path with `--ignore-scripts`" in guidance
    assert "`--registry=<preflight-approved-registry>`" in guidance
    assert "separate verification job" in guidance
    assert (
        "npm publish <built-package.tgz> --ignore-scripts --access public"
        in guidance
    )


def test_release_preflight_inspects_identity_and_emits_stable_digests(
    release_preflight: ModuleType, tmp_path: Path
) -> None:
    artifact = tmp_path / "package.tgz"
    data = _tarball(artifact)

    assert release_preflight.inspect_tarball(
        artifact, "example-package", "1.2.3", "https://registry.npmjs.org"
    ) == {
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
        release_preflight.inspect_tarball(
            artifact, "other-package", "1.2.3", "https://registry.npmjs.org"
        )


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
    assert (
        release_preflight.github_repository("https://github.com/example/example-package.git/")
        == "github.com/example/example-package"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://secret@github.com/example/example-package.git",
        "https://github.com/example/example-package.git?token=secret",
        "https://github.com/example/example-package.git#secret",
    ],
)
def test_release_preflight_rejects_repository_url_secrets(
    release_preflight: ModuleType, url: str
) -> None:
    with pytest.raises(RuntimeError, match="github.com"):
        release_preflight.github_repository(url)


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


def test_release_tag_rejects_shell_metacharacters(
    release_preflight: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        release_preflight,
        "run",
        lambda command, cwd: subprocess.CompletedProcess(command, 0, "", ""),
    )
    with pytest.raises(RuntimeError, match="unsafe"):
        release_preflight.validate_release_tag("v1;touch${IFS}pwn", tmp_path)


@pytest.mark.parametrize(
    ("version", "error"),
    [("11.13.0", None), ("11.11.9", "11.12.0 or newer"), ("unknown", "could not parse")],
)
def test_release_preflight_enforces_verifier_compatible_npm_version(
    release_preflight: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    version: str,
    error: str | None,
) -> None:
    monkeypatch.setattr(
        release_preflight,
        "run",
        lambda command, cwd, env: subprocess.CompletedProcess(command, 0, version, ""),
    )
    if error:
        with pytest.raises(RuntimeError, match=error):
            release_preflight.npm_version(tmp_path)
    else:
        assert release_preflight.npm_version(tmp_path) == version


def test_release_preflight_load_json_rejects_invalid_shapes(
    release_preflight: ModuleType, tmp_path: Path
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json")
    with pytest.raises(RuntimeError, match="cannot read JSON"):
        release_preflight.load_json(invalid)
    array = tmp_path / "array.json"
    array.write_text("[]")
    with pytest.raises(RuntimeError, match="expected a JSON object"):
        release_preflight.load_json(array)


@pytest.mark.parametrize(
    ("returncode", "stdout", "error"),
    [(0, "only-one-field", "could not parse"), (3, "", "could not prove"), (2, "", None)],
)
def test_release_preflight_remote_tag_result_is_fail_closed(
    release_preflight: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    stdout: str,
    error: str | None,
) -> None:
    monkeypatch.setattr(
        release_preflight,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], returncode, stdout, ""),
    )
    if error:
        with pytest.raises(RuntimeError, match=error):
            release_preflight.remote_tag_object("origin", "v1.2.3", tmp_path)
    else:
        assert release_preflight.remote_tag_object("origin", "v1.2.3", tmp_path) is None


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
        published_package_verifier,
        "certificate_claims",
        lambda _bundle: (
            {identity},
            published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER,
        ),
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


@pytest.mark.parametrize(
    "mutation",
    ["repository", "workflow_path", "ref", "build_type", "builder", "identity", "issuer"],
)
def test_slsa_verification_rejects_each_trust_binding_mutation(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    commit = "a" * 40
    identity = (
        "https://github.com/example/packages/.github/workflows/publish.yml"
        "@refs/tags/example-package-v1.2.3"
    )
    entry = _slsa_entry(commit=commit)
    payload = _slsa_payload(entry)
    workflow = payload["predicate"]["buildDefinition"]["externalParameters"]["workflow"]
    claims = {identity}
    issuer = published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER
    if mutation == "repository":
        workflow["repository"] = "https://github.com/attacker/repository"
    elif mutation == "workflow_path":
        workflow["path"] = ".github/workflows/other.yml"
    elif mutation == "ref":
        workflow["ref"] = "refs/tags/other"
    elif mutation == "build_type":
        payload["predicate"]["buildDefinition"]["buildType"] = "https://example.invalid/build"
    elif mutation == "builder":
        payload["predicate"]["runDetails"]["builder"]["id"] = "https://example.invalid/runner"
    elif mutation == "identity":
        claims = {"https://github.com/attacker/repository/.github/workflows/publish.yml@refs/tags/example-package-v1.2.3"}
    else:
        issuer = "https://issuer.example.invalid"
    _replace_slsa_payload(entry, payload)
    monkeypatch.setattr(
        published_package_verifier, "certificate_claims", lambda _bundle: (claims, issuer)
    )

    with pytest.raises(RuntimeError):
        published_package_verifier.verify_slsa(
            {"attestationBundles": [entry]},
            artifact_sha512="artifact-digest",
            package="@example/example-package",
            version="1.2.3",
            repository="https://github.com/example/packages.git",
            workflow_path=".github/workflows/publish.yml",
            tag="example-package-v1.2.3",
            commit=commit,
        )


def test_slsa_verification_normalizes_repository_case_only(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40
    entry = _slsa_entry(commit=commit)
    payload = _slsa_payload(entry)
    dependency = payload["predicate"]["buildDefinition"]["resolvedDependencies"][0]
    dependency["uri"] = (
        "git+https://github.com/Example/Packages@refs/tags/example-package-v1.2.3"
    )
    _replace_slsa_payload(entry, payload)
    identity = (
        "https://github.com/Example/Packages/.github/workflows/publish.yml"
        "@refs/tags/example-package-v1.2.3"
    )
    monkeypatch.setattr(
        published_package_verifier,
        "certificate_claims",
        lambda _bundle: (
            {identity},
            published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER,
        ),
    )

    result = published_package_verifier.verify_slsa(
        {"attestationBundles": [entry]},
        artifact_sha512="artifact-digest",
        package="@example/example-package",
        version="1.2.3",
        repository="https://github.com/example/packages.git",
        workflow_path=".github/workflows/publish.yml",
        tag="example-package-v1.2.3",
        commit=commit,
    )
    assert result["repository"] == "https://github.com/example/packages"


def test_slsa_verification_ignores_unrelated_certificate_sans(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40
    identity = (
        "https://github.com/example/packages/.github/workflows/publish.yml"
        "@refs/tags/example-package-v1.2.3"
    )
    monkeypatch.setattr(
        published_package_verifier,
        "certificate_claims",
        lambda _bundle: (
            {"https://example.invalid/unrelated", identity},
            published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER,
        ),
    )

    result = published_package_verifier.verify_slsa(
        {"attestationBundles": [_slsa_entry(commit=commit)]},
        artifact_sha512="artifact-digest",
        package="@example/example-package",
        version="1.2.3",
        repository="https://github.com/example/packages.git",
        workflow_path=".github/workflows/publish.yml",
        tag="example-package-v1.2.3",
        commit=commit,
    )

    assert result["certificate_identity"] == identity


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
        published_package_verifier,
        "certificate_claims",
        lambda _bundle: (
            {identity},
            published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER,
        ),
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
        published_package_verifier,
        "certificate_claims",
        lambda _bundle: (
            {identity},
            published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER,
        ),
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


@pytest.mark.parametrize("bundles", ["not-a-list", ["not-an-object"]])
def test_slsa_verification_rejects_malformed_bundle_shapes(
    published_package_verifier: ModuleType, bundles: object
) -> None:
    with pytest.raises(RuntimeError, match="attestation"):
        published_package_verifier.verify_slsa(
            {"attestationBundles": bundles},
            artifact_sha512="artifact-digest",
            package="@example/example-package",
            version="1.2.3",
            repository="https://github.com/example/packages.git",
            workflow_path=".github/workflows/publish.yml",
            tag="example-package-v1.2.3",
            commit="a" * 40,
        )


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
        release_preflight.inspect_tarball(
            artifact, "example-package", "1.2.3", "https://registry.npmjs.org"
        )


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


def test_verifier_uses_compiled_tls_paths_not_ssl_environment(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    system_cafile = tmp_path / "system.pem"
    system_cafile.write_text("system")
    system_capath = tmp_path / "system-certs"
    system_capath.mkdir()
    attacker = tmp_path / "attacker.pem"
    attacker.write_text("attacker")
    monkeypatch.setenv("SSL_CERT_FILE", str(attacker))
    monkeypatch.setattr(
        published_package_verifier.ssl,
        "get_default_verify_paths",
        lambda: SimpleNamespace(
            cafile=str(attacker),
            capath=None,
            openssl_cafile=str(system_cafile),
            openssl_capath=str(system_capath),
        ),
    )
    loaded: list[tuple[str | None, str | None]] = []

    class FakeContext:
        def load_verify_locations(
            self, *, cafile: str | None, capath: str | None
        ) -> None:
            loaded.append((cafile, capath))

    monkeypatch.setattr(
        published_package_verifier.ssl, "SSLContext", lambda _protocol: FakeContext()
    )
    published_package_verifier.system_ssl_context()
    assert loaded == [(str(system_cafile), str(system_capath))]


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
        elif command == ["npm", "--version"]:
            stdout = "11.13.0"
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


@pytest.mark.parametrize("phase", ["tag", "publish"])
def test_release_preflight_main_tag_and_publish_success(
    release_preflight: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
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
    commit = "a" * 40
    tag_object = "b" * 40
    fingerprint = "C" * 40

    def fake_run(command: list[str], *_args: object, **_kwargs: object) -> object:
        key = tuple(command[:3])
        stdout = ""
        if key == ("git", "rev-parse", "--show-toplevel"):
            stdout = str(repo)
        elif key == ("git", "remote", "get-url"):
            stdout = "https://github.com/example/packages.git"
        elif key == ("git", "rev-parse", "HEAD"):
            stdout = commit
        elif key == ("git", "cat-file", "-t"):
            stdout = "tag"
        elif key == ("git", "rev-list", "-n"):
            stdout = commit
        elif command[:2] == ["git", "rev-parse"]:
            stdout = tag_object
        elif command == ["npm", "--version"]:
            stdout = "11.13.0"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(release_preflight, "run", fake_run)
    monkeypatch.setattr(
        release_preflight,
        "remote_tag_object",
        lambda *_args: tag_object if phase == "publish" else None,
    )
    monkeypatch.setattr(release_preflight, "registry_version_absent", lambda *_args: None)
    monkeypatch.setattr(
        release_preflight, "verify_tag_signer", lambda *_args: fingerprint
    )
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
            "--phase",
            phase,
            "--access",
            "public",
            "--signer-fingerprint",
            fingerprint,
        ],
    )
    assert release_preflight.main() == 0


@pytest.mark.parametrize(
    ("phase", "tag_type", "target", "remote_object", "signer", "error"),
    [
        ("tag", "tag", "head", "remote", "signer", "already exists"),
        ("publish", "tag", "head", "wrong", "signer", "does not match"),
        ("tag", "commit", "head", None, "signer", "not annotated"),
        ("tag", "tag", "wrong", None, "signer", "targets"),
        ("tag", "tag", "head", None, None, "require --signer-fingerprint"),
    ],
)
def test_release_preflight_main_rejects_invalid_tag_phase_state(
    release_preflight: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
    tag_type: str,
    target: str,
    remote_object: str | None,
    signer: str | None,
    error: str,
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
    commit = "a" * 40
    tag_object = "b" * 40

    def fake_run(command: list[str], *_args: object, **_kwargs: object) -> object:
        key = tuple(command[:3])
        stdout = ""
        if key == ("git", "rev-parse", "--show-toplevel"):
            stdout = str(repo)
        elif key == ("git", "remote", "get-url"):
            stdout = "https://github.com/example/packages.git"
        elif key == ("git", "rev-parse", "HEAD"):
            stdout = commit
        elif key == ("git", "cat-file", "-t"):
            stdout = tag_type
        elif key == ("git", "rev-list", "-n"):
            stdout = commit if target == "head" else "d" * 40
        elif command[:2] == ["git", "rev-parse"]:
            stdout = tag_object
        elif command == ["npm", "--version"]:
            stdout = "11.13.0"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(release_preflight, "run", fake_run)
    monkeypatch.setattr(
        release_preflight,
        "remote_tag_object",
        lambda *_args: tag_object if remote_object == "remote" else remote_object,
    )
    monkeypatch.setattr(release_preflight, "registry_version_absent", lambda *_args: None)
    monkeypatch.setattr(
        release_preflight, "verify_tag_signer", lambda *_args: "C" * 40
    )
    argv = [
        "release-preflight.py",
        "--package-dir",
        str(package_dir),
        "--artifact",
        str(artifact),
        "--tag",
        "v1.2.3",
        "--phase",
        phase,
        "--access",
        "public",
    ]
    if signer:
        argv.extend(["--signer-fingerprint", "C" * 40])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(RuntimeError, match=error):
        release_preflight.main()


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
    seen_calls: list[tuple[list[str], object]] = []

    def fake_run(
        command: list[str], *_args: object, env: object = None, **_kwargs: object
    ) -> object:
        seen_calls.append((command, env))
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
                    "verified": [],
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
    npm_calls = [(command, env) for command, env in seen_calls if command[0] == "npm"]
    assert len(npm_calls) == 3
    for _command, env in npm_calls:
        assert isinstance(env, dict)
        assert "NPM_CONFIG_USERCONFIG" in env
        assert "NPM_CONFIG_CACHE" in env
    install = next(command for command, _env in npm_calls if command[:2] == ["npm", "install"])
    assert "--ignore-scripts" in install
    assert "example-package@1.2.3" in install
    assert "--registry=https://registry.example" in install


def test_verifier_rejects_workflow_path_in_unavailable_mode(
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
            "--source-repository",
            "https://github.com/owner/repo",
            "--workflow-path",
            ".github/workflows/publish.yml",
            "--tag",
            "v1.2.3",
            "--commit",
            "a" * 40,
            "--repository-dir",
            str(tmp_path),
            "--signer-fingerprint",
            "C" * 40,
        ],
    )
    with pytest.raises(RuntimeError, match="only valid"):
        published_package_verifier.main()


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


def _self_signed_der(
    tmp_path: Path,
    identity: str,
    issuer: str = "https://token.actions.githubusercontent.com",
) -> bytes:
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
            "-addext",
            f"1.3.6.1.4.1.57264.1.1=ASN1:UTF8String:{issuer}",
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
    claims = published_package_verifier.certificate_claims({"verificationMaterial": material})
    assert claims == ({identity}, published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER)


def test_certificate_claims_rejects_wrong_oidc_issuer(
    published_package_verifier: ModuleType, tmp_path: Path
) -> None:
    identity = "https://github.com/owner/repo/.github/workflows/publish.yml@refs/tags/v1.2.3"
    encoded = base64.b64encode(
        _self_signed_der(tmp_path, identity, "https://issuer.example.invalid")
    ).decode("ascii")
    identities, issuer = published_package_verifier.certificate_claims(
        {"verificationMaterial": {"certificate": {"rawBytes": encoded}}}
    )
    assert identities == {identity}
    assert issuer == "https://issuer.example.invalid"


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


@pytest.mark.parametrize("bundles", ["not-a-list", {"predicateType": "wrong"}, ["bad"]])
def test_unavailable_provenance_rejects_malformed_attestation_bundles(
    published_package_verifier: ModuleType, bundles: object
) -> None:
    with pytest.raises(RuntimeError, match="attestation"):
        published_package_verifier.assert_no_provenance({"attestationBundles": bundles})


def test_target_verification_requires_the_exact_verified_entry(
    published_package_verifier: ModuleType,
) -> None:
    audit = {"invalid": [], "missing": [], "verified": [{"name": "other", "version": "1.2.3"}]}
    with pytest.raises(RuntimeError, match="did not verify the exact target"):
        published_package_verifier.target_verification(audit, "example-package", "1.2.3")


def test_unavailable_target_verification_accepts_clean_empty_verified_list(
    published_package_verifier: ModuleType,
) -> None:
    audit: dict[str, Any] = {"invalid": [], "missing": [], "verified": []}
    assert (
        published_package_verifier.target_verification(
            audit, "example-package", "1.2.3", required=False
        )
        is None
    )


def test_target_verification_rejects_duplicate_exact_entries(
    published_package_verifier: ModuleType,
) -> None:
    target = {"name": "example-package", "version": "1.2.3"}
    audit = {"invalid": [], "missing": [], "verified": [target, dict(target)]}
    with pytest.raises(RuntimeError, match="duplicate"):
        published_package_verifier.target_verification(audit, "example-package", "1.2.3")


@pytest.mark.parametrize("field", ["invalid", "missing", "verified"])
def test_target_verification_rejects_malformed_audit_entries(
    published_package_verifier: ModuleType, field: str
) -> None:
    audit: dict[str, Any] = {"invalid": [], "missing": [], "verified": []}
    audit[field] = ["not-an-object"]
    with pytest.raises(RuntimeError, match="malformed package entry"):
        published_package_verifier.target_verification(
            audit, "example-package", "1.2.3", required=False
        )


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


def test_verified_fingerprints_accepts_gpg_primary_and_subkey_only(
    release_preflight: ModuleType,
    published_package_verifier: ModuleType,
) -> None:
    subkey = "A" * 40
    primary = "B" * 40
    result = subprocess.CompletedProcess(
        ["git", "verify-tag"],
        0,
        "",
        f"[GNUPG:] VALIDSIG {subkey} 2026-01-01 0 4 0 1 10 00 {primary}\n",
    )
    for module in (release_preflight, published_package_verifier):
        assert module.verified_fingerprints(result) == {subkey, primary}


def test_verified_fingerprints_does_not_accept_ssh_principal_spoof(
    release_preflight: ModuleType,
    published_package_verifier: ModuleType,
) -> None:
    approved = "SHA256:approved"
    actual = "SHA256:actual"
    result = subprocess.CompletedProcess(
        ["git", "verify-tag"],
        0,
        "",
        f'Good "git" signature for {approved} with ED25519 key {actual}\n',
    )
    for module in (release_preflight, published_package_verifier):
        assert module.verified_fingerprints(result) == {actual}


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
        release_preflight.inspect_tarball(
            artifact, "example-package", "1.2.3", "https://registry.npmjs.org"
        )


def test_preflight_rejects_tarball_registry_override(
    release_preflight: ModuleType, tmp_path: Path
) -> None:
    artifact = tmp_path / "package.tgz"
    _tarball(
        artifact,
        extra_manifest={"publishConfig": {"registry": "https://attacker.invalid"}},
    )

    with pytest.raises(RuntimeError, match="tarball publishConfig.registry"):
        release_preflight.inspect_tarball(
            artifact, "example-package", "1.2.3", "https://registry.npmjs.org"
        )


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


def test_preflight_rejects_source_registry_override(
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
                "publishConfig": {"registry": "https://attacker.invalid"},
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

    with pytest.raises(RuntimeError, match="package.json publishConfig.registry"):
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


def _self_signed_der_with_crl_uri(tmp_path: Path, san: str, crl_uri: str) -> bytes:
    """A certificate whose workflow-shaped URI lives outside the SAN extension."""
    key = tmp_path / "crl-key.pem"
    cert = tmp_path / "crl-cert.der"
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
            f"subjectAltName=URI:{san}",
            "-addext",
            f"crlDistributionPoints=URI:{crl_uri}",
            "-addext",
            "1.3.6.1.4.1.57264.1.1=ASN1:UTF8String:"
            "https://token.actions.githubusercontent.com",
        ],
        check=True,
        capture_output=True,
    )
    return cert.read_bytes()


def test_certificate_claims_reads_identities_only_from_the_san(
    published_package_verifier: ModuleType, tmp_path: Path
) -> None:
    """A workflow URI outside the SAN must never satisfy the identity policy."""
    workflow = "https://github.com/owner/repo/.github/workflows/publish.yml@refs/tags/v1.2.3"
    encoded = base64.b64encode(
        _self_signed_der_with_crl_uri(tmp_path, "https://example.invalid/unrelated", workflow)
    ).decode("ascii")
    identities, _issuer = published_package_verifier.certificate_claims(
        {"verificationMaterial": {"certificate": {"rawBytes": encoded}}}
    )
    assert identities == {"https://example.invalid/unrelated"}
    assert workflow not in identities


def test_verify_slsa_skips_a_malformed_bundle_and_verifies_the_valid_one(
    published_package_verifier: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """attestationBundles ordering is registry-controlled; junk must not abort the loop."""
    commit = "a" * 40
    malformed = _slsa_entry(commit=commit)
    payload = _slsa_payload(malformed)
    payload["predicate"]["buildDefinition"]["externalParameters"]["workflow"] = "not-an-object"
    _replace_slsa_payload(malformed, payload)
    valid = _slsa_entry(commit=commit)
    identity = (
        "https://github.com/example/packages/.github/workflows/publish.yml"
        "@refs/tags/example-package-v1.2.3"
    )
    monkeypatch.setattr(
        published_package_verifier,
        "certificate_claims",
        lambda _bundle: ({identity}, published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER),
    )
    result = published_package_verifier.verify_slsa(
        {"attestationBundles": [malformed, valid]},
        artifact_sha512="artifact-digest",
        package="@example/example-package",
        version="1.2.3",
        repository="https://github.com/example/packages",
        workflow_path=".github/workflows/publish.yml",
        tag="example-package-v1.2.3",
        commit=commit,
    )
    assert result["matching_bundles"] == 1
    assert result["commit"] == commit


@pytest.mark.parametrize(
    "payload_mutation",
    [
        {"subject": "not-a-list"},
        {"predicate": "not-an-object"},
    ],
)
def test_verify_slsa_reports_malformed_payloads_as_clean_failures(
    published_package_verifier: ModuleType, payload_mutation: dict[str, Any]
) -> None:
    entry = _slsa_entry(commit="a" * 40)
    payload = _slsa_payload(entry)
    payload.update(payload_mutation)
    _replace_slsa_payload(entry, payload)
    with pytest.raises(RuntimeError):
        published_package_verifier.verify_slsa(
            {"attestationBundles": [entry]},
            artifact_sha512="artifact-digest",
            package="@example/example-package",
            version="1.2.3",
            repository="https://github.com/example/packages",
            workflow_path=".github/workflows/publish.yml",
            tag="example-package-v1.2.3",
            commit="a" * 40,
        )


@pytest.mark.parametrize(
    "registry",
    [
        "http://registry.example",
        "https://user:pass@registry.example",
        "https://registry.example?token=x",
        "https://registry.example#frag",
        "https://registry.example:notaport",
    ],
)
def test_verifier_registry_origin_rejects_unsafe_urls(
    published_package_verifier: ModuleType, registry: str
) -> None:
    """Deleting the HTTPS/credential check must fail here."""
    with pytest.raises(RuntimeError):
        published_package_verifier.registry_origin(registry)


def test_verifier_registry_origin_accepts_https_and_defaults_the_port(
    published_package_verifier: ModuleType,
) -> None:
    assert published_package_verifier.registry_origin("https://Registry.Example/path") == (
        "registry.example",
        443,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "ab cd ef 01 23 45 67 89 ab cd ef 01 23 45 67 89 ab cd ef 01",
            "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        ),
        ("abcdef0123456789abcdef0123456789abcdef01", "ABCDEF0123456789ABCDEF0123456789ABCDEF01"),
        ("sha256:AbC+/dEf=", "SHA256:AbC+/dEf="),
        ("SHA256:AbC+/dEf=", "SHA256:AbC+/dEf="),
    ],
)
def test_normalize_fingerprint_strips_spaces_and_folds_case(
    published_package_verifier: ModuleType,
    release_preflight: ModuleType,
    value: str,
    expected: str,
) -> None:
    """Replacing the body with `return value` must fail this test in both helpers."""
    assert published_package_verifier.normalize_fingerprint(value) == expected
    assert release_preflight.normalize_fingerprint(value) == expected


def test_embedded_identity_reads_the_archive_from_memory(
    published_package_verifier: ModuleType, tmp_path: Path
) -> None:
    data = _tarball(tmp_path / "package.tgz", "@example/example-package", "1.2.3")
    assert published_package_verifier.embedded_identity(data) == (
        "@example/example-package",
        "1.2.3",
    )


def test_embedded_identity_rejects_bytes_that_are_not_a_tarball(
    published_package_verifier: ModuleType,
) -> None:
    with pytest.raises(RuntimeError, match="cannot inspect registry tarball"):
        published_package_verifier.embedded_identity(b"not a tarball")


def _verifier_cli_harness(
    verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    package: str,
    audit: dict[str, Any],
    dist_extra: dict[str, Any] | None = None,
) -> tuple[Path, Path, dict[str, str]]:
    """Wire main() to fake subprocesses and return (artifact, repository_dir, digests)."""
    repository_dir = tmp_path / "checkout"
    repository_dir.mkdir(exist_ok=True)
    artifact = tmp_path / "package.tgz"
    data = _tarball(artifact, package, "1.2.3")
    digests = verifier.hashes(data)
    dist = {
        "integrity": digests["integrity"],
        "shasum": digests["sha1"],
        "signatures": [{"keyid": "test", "sig": "test"}],
        "tarball": "https://registry.example/package.tgz",
        **(dist_extra or {}),
    }

    def fake_run(
        command: list[str], *_args: object, env: object = None, **_kwargs: object
    ) -> object:
        stdout = ""
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            stdout = str(repository_dir)
        elif command[:2] == ["npm", "view"]:
            stdout = json.dumps({"dist": dist})
        elif command[:3] == ["npm", "audit", "signatures"]:
            stdout = json.dumps(audit)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(verifier, "run", fake_run)
    monkeypatch.setattr(verifier, "download_from_registry", lambda *_args: data)
    monkeypatch.setattr(
        verifier,
        "verify_release_tag",
        lambda *_args: {"object": "b" * 40, "signer_fingerprint": "C" * 40, "target": "a" * 40},
    )
    return artifact, repository_dir, digests


def _verifier_argv(
    artifact: Path, repository_dir: Path, output: Path, package: str, *extra: str
) -> list[str]:
    return [
        "verify-published-package.py",
        "--package",
        package,
        "--version",
        "1.2.3",
        "--artifact",
        str(artifact),
        "--registry",
        "https://registry.example",
        "--access",
        "public",
        "--tag",
        "example-package-v1.2.3",
        "--commit",
        "a" * 40,
        "--repository-dir",
        str(repository_dir),
        "--signer-fingerprint",
        "C" * 40,
        "--output",
        str(output),
        *extra,
    ]


def test_verifier_main_refuses_to_downgrade_a_package_declaring_attestations(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Deleting the dist.attestations guard must fail this test."""
    artifact, repository_dir, _digests = _verifier_cli_harness(
        published_package_verifier,
        monkeypatch,
        tmp_path,
        package="example-package",
        audit={"invalid": [], "missing": [], "verified": []},
        dist_extra={"attestations": {"url": "https://registry.example/attestations"}},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        _verifier_argv(
            artifact,
            repository_dir,
            tmp_path / "verification.json",
            "example-package",
            "--provenance",
            "unavailable",
            "--source-repository",
            "https://github.com/owner/repo",
        ),
    )
    with pytest.raises(RuntimeError, match="declares attestations"):
        published_package_verifier.main()


def test_verifier_main_required_provenance_success(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The mode every Trusted Publisher release uses needs an end-to-end backstop."""
    package = "@example/example-package"
    commit = "a" * 40
    artifact, repository_dir, digests = _verifier_cli_harness(
        published_package_verifier,
        monkeypatch,
        tmp_path,
        package=package,
        audit={"invalid": [], "missing": [], "verified": []},
    )
    entry = _slsa_entry(
        commit=commit,
        subjects=[
            {
                "name": "pkg:npm/%40example/example-package@1.2.3",
                "digest": {"sha512": digests["sha512"]},
            }
        ],
    )
    audit = {
        "invalid": [],
        "missing": [],
        "verified": [{"name": package, "version": "1.2.3", "attestationBundles": [entry]}],
    }
    artifact, repository_dir, digests = _verifier_cli_harness(
        published_package_verifier,
        monkeypatch,
        tmp_path,
        package=package,
        audit=audit,
    )
    identity = (
        "https://github.com/example/packages/.github/workflows/publish.yml"
        "@refs/tags/example-package-v1.2.3"
    )
    monkeypatch.setattr(
        published_package_verifier,
        "certificate_claims",
        lambda _bundle: ({identity}, published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER),
    )
    output = tmp_path / "verification.json"
    monkeypatch.setattr(
        sys,
        "argv",
        _verifier_argv(
            artifact,
            repository_dir,
            output,
            package,
            "--provenance",
            "required",
            "--source-repository",
            "https://github.com/example/packages",
            "--workflow-path",
            ".github/workflows/publish.yml",
        ),
    )
    assert published_package_verifier.main() == 0
    result = json.loads(output.read_text())
    assert result["provenance"]["status"] == "verified"
    assert result["provenance"]["repository"] == "https://github.com/example/packages"
    assert result["provenance"]["commit"] == commit
    assert result["verified_attestations"] == 1


def test_verifier_main_status_discriminant_is_not_shadowed_by_the_payload(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A provenance payload key named status must not overwrite the marker."""
    package = "example-package"
    artifact, repository_dir, _digests = _verifier_cli_harness(
        published_package_verifier,
        monkeypatch,
        tmp_path,
        package=package,
        audit={
            "invalid": [],
            "missing": [],
            "verified": [{"name": package, "version": "1.2.3", "attestationBundles": []}],
        },
    )
    monkeypatch.setattr(
        published_package_verifier,
        "verify_slsa",
        lambda *_args, **_kwargs: {"status": "attacker-controlled", "matching_bundles": 1},
    )
    output = tmp_path / "verification.json"
    monkeypatch.setattr(
        sys,
        "argv",
        _verifier_argv(
            artifact,
            repository_dir,
            output,
            package,
            "--provenance",
            "required",
            "--source-repository",
            "https://github.com/owner/repo",
            "--workflow-path",
            ".github/workflows/publish.yml",
        ),
    )
    assert published_package_verifier.main() == 0
    assert json.loads(output.read_text())["provenance"]["status"] == "verified"


@pytest.mark.parametrize(
    ("flag", "value", "error"),
    [
        ("--remote", "--upload-pack=touch owned", "invalid Git remote name"),
        ("--tag", "v1.2.3;rm -rf /", "unsafe in displayed shell commands"),
    ],
)
def test_verifier_main_rejects_option_shaped_remote_and_unsafe_tag(
    published_package_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
    value: str,
    error: str,
) -> None:
    """The verifier must apply the same argument-shape contract as the preflight."""
    artifact, repository_dir, _digests = _verifier_cli_harness(
        published_package_verifier,
        monkeypatch,
        tmp_path,
        package="example-package",
        audit={"invalid": [], "missing": [], "verified": []},
    )
    argv = _verifier_argv(
        artifact,
        repository_dir,
        tmp_path / "verification.json",
        "example-package",
        "--provenance",
        "unavailable",
        "--source-repository",
        "https://github.com/owner/repo",
    )
    if flag == "--tag":
        argv[argv.index("--tag") + 1] = value
    else:
        argv.extend([flag, value])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(RuntimeError, match=error):
        published_package_verifier.main()


@pytest.mark.parametrize(
    "registry",
    [
        "http://registry.example",
        "https://user:pass@registry.example",
        "https://registry.example?token=x",
        "https://registry.example#frag",
    ],
)
def test_release_preflight_main_rejects_unsafe_registry_urls(
    release_preflight: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registry: str,
) -> None:
    """Deleting the HTTPS/credential check in the preflight must fail this test."""
    package_dir = tmp_path / "repo" / "package"
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
            "--registry",
            registry,
        ],
    )
    with pytest.raises(RuntimeError, match="registry must be an HTTPS URL"):
        release_preflight.main()


def test_certificate_claims_returns_no_identities_without_a_san(
    published_package_verifier: ModuleType, tmp_path: Path
) -> None:
    """A certificate with no SAN must yield an empty identity set, not a parse error."""
    key = tmp_path / "nosan-key.pem"
    cert = tmp_path / "nosan-cert.der"
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
            "1.3.6.1.4.1.57264.1.1=ASN1:UTF8String:"
            "https://token.actions.githubusercontent.com",
        ],
        check=True,
        capture_output=True,
    )
    encoded = base64.b64encode(cert.read_bytes()).decode("ascii")
    identities, issuer = published_package_verifier.certificate_claims(
        {"verificationMaterial": {"certificate": {"rawBytes": encoded}}}
    )
    assert identities == set()
    assert issuer == published_package_verifier.GITHUB_ACTIONS_OIDC_ISSUER
