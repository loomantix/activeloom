#!/usr/bin/env python3
"""Compare an npm registry tarball with a build-once artifact and verify attestations."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn

SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
GITHUB_ACTIONS_BUILD_TYPE = "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1"
GITHUB_HOSTED_BUILDER = "https://github.com/actions/runner/github-hosted"
COMMAND_TIMEOUT_SECONDS = 300
DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 5
# Registry bytes that differ in length from the local artifact can never satisfy
# the byte-identity check, so a generous multiple is enough to bound memory.
MAX_DOWNLOAD_MULTIPLE = 4
MAX_DOWNLOAD_FLOOR_BYTES = 8 * 1024 * 1024
# The child environment is built from this allowlist rather than by subtracting
# known-bad names: npm and Node read TLS trust, proxy, and code-injection
# settings (NODE_OPTIONS, NODE_EXTRA_CA_CERTS, SSL_CERT_FILE, *_PROXY) from an
# open-ended namespace that a denylist cannot enumerate.
ALLOWED_ENV_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
    }
)


def fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def run(
    command: list[str],
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy() if env is None else env.copy()
    if command and command[0] == "git":
        command_env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            env=command_env,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        fail(f"command timed out after {COMMAND_TIMEOUT_SECONDS}s: {' '.join(command)}")
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        fail(f"command failed: {' '.join(command)}: {detail}")
    return result


def credential_free_npm_env(config_dir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in ALLOWED_ENV_KEYS}
    user_config = config_dir / "empty-user.npmrc"
    global_config = config_dir / "empty-global.npmrc"
    user_config.write_text("", encoding="utf-8")
    global_config.write_text("", encoding="utf-8")
    env["NPM_CONFIG_USERCONFIG"] = str(user_config)
    env["NPM_CONFIG_GLOBALCONFIG"] = str(global_config)
    # Isolate the cache so every check is a real registry read, not a replay.
    env["NPM_CONFIG_CACHE"] = str(config_dir / "cache")
    return env


def hashes(data: bytes) -> dict[str, str]:
    sha512 = hashlib.sha512(data)
    return {
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "sha512": sha512.hexdigest(),
        "integrity": "sha512-" + base64.b64encode(sha512.digest()).decode("ascii"),
        "bytes": str(len(data)),
    }


def embedded_identity(data: bytes) -> tuple[str | None, str | None]:
    with tempfile.NamedTemporaryFile(suffix=".tgz") as handle:
        handle.write(data)
        handle.flush()
        try:
            with tarfile.open(handle.name, "r:gz") as archive:
                member = archive.getmember("package/package.json")
                if not member.isfile():
                    fail("package/package.json in registry tarball must be a regular file")
                extracted = archive.extractfile(member)
                if extracted is None:
                    fail("cannot read package/package.json from registry tarball")
                with extracted:
                    package_json = json.loads(extracted.read().decode("utf-8"))
        except (KeyError, OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail(f"cannot inspect registry tarball: {exc}")
    return package_json.get("name"), package_json.get("version")


def audit_entries(audit: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """Return one audit result list, refusing to read a missing key as clean."""
    if field not in audit:
        fail(f"npm audit signatures output does not contain a {field} list")
    entries = audit[field]
    if not isinstance(entries, list):
        fail(f"npm audit signatures {field} result is not a list")
    return entries


def target_verification(audit: dict[str, Any], package: str, version: str) -> dict[str, Any]:
    for field in ("invalid", "missing"):
        entries = audit_entries(audit, field)
        if any(
            isinstance(entry, dict) and entry.get("name") == package and entry.get("version") == version
            for entry in entries
        ):
            fail(f"target package appears in npm audit signatures {field} results")
    matches = [
        entry
        for entry in audit_entries(audit, "verified")
        if isinstance(entry, dict) and entry.get("name") == package and entry.get("version") == version
    ]
    if not matches:
        fail("npm audit signatures did not verify the exact target package and version")
    return matches[0]


def assert_no_provenance(entry: dict[str, Any]) -> None:
    """Refuse to downgrade a present-but-unverified attestation to 'unavailable'."""
    bundles = entry.get("attestationBundles") or []
    if any(
        isinstance(bundle, dict) and bundle.get("predicateType") == SLSA_PROVENANCE_V1
        for bundle in bundles
    ):
        fail(
            "package carries SLSA provenance; rerun with --provenance required "
            "instead of recording it as unavailable"
        )


def github_repository(url: str) -> str:
    value = url.removeprefix("git+")
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    else:
        parsed = urllib.parse.urlparse(value)
        if parsed.hostname != "github.com":
            fail(f"source repository is not hosted on github.com: {url}")
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").rstrip("/")
    if path.count("/") != 1:
        fail(f"source repository must identify one GitHub owner/repository: {url}")
    # GitHub owner/repository names are case-insensitive and the three sources
    # compared here are authored independently, so casing must not decide a release.
    return f"https://github.com/{path.casefold()}"


def normalize_fingerprint(value: str) -> str:
    compact = value.replace(" ", "")
    if compact.lower().startswith("sha256:"):
        return "SHA256:" + compact.split(":", 1)[1]
    return compact.upper()


def verified_fingerprints(result: subprocess.CompletedProcess[str]) -> set[str]:
    fingerprints: set[str] = set()
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        valid_signature = re.search(r"\[GNUPG:\] VALIDSIG ([0-9A-Fa-f]+)", line)
        if valid_signature:
            fingerprints.add(normalize_fingerprint(valid_signature.group(1)))
        for token in line.split():
            if token.startswith("SHA256:"):
                fingerprints.add(normalize_fingerprint(token))
    return fingerprints


def verify_tag_signer(tag: str, expected_fingerprint: str, cwd: Path) -> str:
    result = run(["git", "verify-tag", "--raw", tag], cwd)
    fingerprints = verified_fingerprints(result)
    expected = normalize_fingerprint(expected_fingerprint)
    if expected not in fingerprints:
        fail(f"release tag signer does not match approved fingerprint: {expected_fingerprint}")
    return expected


def remote_tag_object(remote: str, tag: str, cwd: Path) -> str:
    result = run(
        ["git", "ls-remote", "--exit-code", "--tags", remote, f"refs/tags/{tag}"], cwd
    )
    fields = result.stdout.split()
    if len(fields) != 2:
        fail("could not parse remote tag result")
    return fields[0]


def verify_release_tag(
    repository_dir: Path,
    remote: str,
    tag: str,
    commit: str,
    signer_fingerprint: str,
    source_repository: str,
) -> dict[str, str]:
    repo_root = Path(
        run(["git", "rev-parse", "--show-toplevel"], repository_dir).stdout.strip()
    ).resolve()
    # Bind the tag evidence to the published package's source repository, so tag
    # facts cannot be collected from an unrelated clone carrying the same tag.
    remote_url = run(["git", "remote", "get-url", remote], repo_root).stdout.strip()
    if github_repository(remote_url) != github_repository(source_repository):
        fail("release checkout remote does not match the expected source repository")
    tag_type = run(["git", "cat-file", "-t", f"refs/tags/{tag}"], repo_root).stdout.strip()
    if tag_type != "tag":
        fail(f"release tag is not annotated: {tag}")
    signer = verify_tag_signer(tag, signer_fingerprint, repo_root)
    target = run(["git", "rev-list", "-n", "1", tag], repo_root).stdout.strip()
    if target != commit:
        fail(f"release tag targets {target}, not expected commit {commit}")
    local_object = run(["git", "rev-parse", f"refs/tags/{tag}"], repo_root).stdout.strip()
    if remote_tag_object(remote, tag, repo_root) != local_object:
        fail("remote release tag does not match the verified local annotated tag object")
    return {"object": local_object, "signer_fingerprint": signer, "target": target}


def registry_origin(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        fail(f"invalid registry URL port: {exc}")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        fail("registry and tarball URLs must use HTTPS without credentials, query, or fragment")
    return parsed.hostname.lower(), port or 443


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def download_from_registry(tarball_url: str, registry: str, max_bytes: int) -> bytes:
    expected_origin = registry_origin(registry)
    current_url = tarball_url
    # Pin registry TLS trust to the system store so it cannot be redirected by
    # inherited SSL_CERT_FILE/SSL_CERT_DIR settings.
    opener = urllib.request.build_opener(
        NoRedirectHandler(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    for _ in range(MAX_REDIRECTS + 1):
        if registry_origin(current_url) != expected_origin:
            fail(f"refusing registry tarball URL outside the configured origin: {current_url}")
        try:
            with opener.open(current_url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                data: bytes = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    fail(f"registry tarball exceeds the {max_bytes}-byte verification bound")
                return data
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                fail(f"could not download registry tarball: HTTP {exc.code}")
            location = exc.headers.get("Location")
            if not location:
                fail("registry tarball redirect did not include a Location header")
            current_url = urllib.parse.urljoin(current_url, location)
        except OSError as exc:
            fail(f"could not download registry tarball: {exc}")
    fail(f"registry tarball exceeded {MAX_REDIRECTS} redirects")


def certificate_raw_bytes(material: dict[str, Any]) -> str:
    """Return the signing certificate from either supported Sigstore bundle layout.

    Bundle v0.3+ carries a singular ``certificate``; npm currently emits v0.2
    bundles whose provenance material uses ``x509CertificateChain``.
    """
    certificate = material.get("certificate")
    if isinstance(certificate, dict) and "rawBytes" in certificate:
        return str(certificate["rawBytes"])
    chain = material.get("x509CertificateChain")
    if isinstance(chain, dict):
        certificates = chain.get("certificates")
        if isinstance(certificates, list) and certificates:
            leaf = certificates[0]
            if isinstance(leaf, dict) and "rawBytes" in leaf:
                return str(leaf["rawBytes"])
    fail("provenance bundle does not carry a signing certificate in a supported layout")


def certificate_identities(bundle: dict[str, Any]) -> set[str]:
    try:
        encoded = certificate_raw_bytes(bundle["verificationMaterial"])
        certificate = base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        fail(f"cannot decode provenance signing certificate: {exc}")
    with tempfile.TemporaryDirectory(prefix="npm-provenance-cert-") as temp_dir:
        cert_path = Path(temp_dir) / "certificate.der"
        cert_path.write_bytes(certificate)
        result = run(
            ["openssl", "x509", "-inform", "DER", "-in", str(cert_path), "-noout", "-ext", "subjectAltName"],
            Path(temp_dir),
        )
    identities: set[str] = set()
    for part in result.stdout.replace("\n", ",").split(","):
        value = part.strip()
        if value.startswith("URI:"):
            identities.add(value.removeprefix("URI:"))
    return identities


def verify_slsa_candidate(
    attestation_bundle: dict[str, Any],
    *,
    artifact_sha512: str,
    package: str,
    version: str,
    repository: str,
    workflow_path: str,
    tag: str,
    commit: str,
) -> dict[str, Any]:
    try:
        bundle = attestation_bundle["bundle"]
        encoded = bundle["dsseEnvelope"]["payload"]
        statement = json.loads(base64.b64decode(encoded, validate=True))
        subject = statement["subject"]
        predicate = statement["predicate"]
        build_definition = predicate["buildDefinition"]
        workflow = build_definition["externalParameters"]["workflow"]
        dependencies = build_definition["resolvedDependencies"]
        builder = predicate["runDetails"]["builder"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        fail(f"cannot decode SLSA provenance payload: {exc}")
    if statement.get("predicateType") != SLSA_PROVENANCE_V1:
        fail("SLSA statement predicate type does not match provenance v1")
    expected_subject = f"pkg:npm/{urllib.parse.quote(package, safe='/')}@{version}"
    if not any(
        isinstance(subject_entry, dict)
        and subject_entry.get("name") == expected_subject
        and subject_entry.get("digest", {}).get("sha512") == artifact_sha512
        for subject_entry in subject
    ):
        fail("no SLSA subject binds the target package identity to the artifact SHA-512")
    expected_repository = github_repository(repository)
    expected_ref = f"refs/tags/{tag}"
    if github_repository(workflow.get("repository", "")) != expected_repository:
        fail("SLSA workflow repository does not match the expected source")
    if workflow.get("path") != workflow_path:
        fail("SLSA workflow path does not match the expected publish workflow")
    if workflow.get("ref") != expected_ref:
        fail("SLSA workflow ref does not match the expected release tag")
    expected_dependency = f"git+{expected_repository}@{expected_ref}"
    if not any(
        isinstance(dependency, dict)
        and dependency.get("uri") == expected_dependency
        and dependency.get("digest", {}).get("gitCommit") == commit
        for dependency in dependencies
    ):
        fail("no SLSA dependency binds the expected repository, tag, and release commit")
    if build_definition.get("buildType") != GITHUB_ACTIONS_BUILD_TYPE:
        fail("SLSA build type is not the supported GitHub Actions workflow type")
    if builder.get("id") != GITHUB_HOSTED_BUILDER:
        fail("SLSA builder is not the GitHub-hosted Actions runner")
    expected_identity = f"{expected_repository}/{workflow_path.lstrip('/')}@{expected_ref}"
    if expected_identity not in certificate_identities(bundle):
        fail("provenance signing certificate identity does not match the expected workflow and tag")
    return {
        "builder": GITHUB_HOSTED_BUILDER,
        "build_type": GITHUB_ACTIONS_BUILD_TYPE,
        "certificate_identity": expected_identity,
        "commit": commit,
        "predicate_type": SLSA_PROVENANCE_V1,
        "repository": expected_repository,
        "tag": tag,
        "workflow_path": workflow_path,
    }


def verify_slsa(
    entry: dict[str, Any],
    *,
    artifact_sha512: str,
    package: str,
    version: str,
    repository: str,
    workflow_path: str,
    tag: str,
    commit: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    for attestation_bundle in entry.get("attestationBundles", []):
        if attestation_bundle.get("predicateType") != SLSA_PROVENANCE_V1:
            continue
        try:
            matches.append(
                verify_slsa_candidate(
                    attestation_bundle,
                    artifact_sha512=artifact_sha512,
                    package=package,
                    version=version,
                    repository=repository,
                    workflow_path=workflow_path,
                    tag=tag,
                    commit=commit,
                )
            )
        except RuntimeError as exc:
            errors.append(str(exc))
    if not matches:
        detail = errors[-1] if errors else "no provenance-v1 bundle was returned"
        fail(f"npm audit signatures did not return matching SLSA provenance: {detail}")
    result = matches[0]
    result["matching_bundles"] = len(matches)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--registry", default="https://registry.npmjs.org")
    parser.add_argument("--access", choices=("public",), required=True)
    parser.add_argument("--provenance", choices=("required", "unavailable"), default="required")
    parser.add_argument("--source-repository")
    parser.add_argument("--workflow-path")
    parser.add_argument("--tag")
    parser.add_argument("--commit")
    parser.add_argument("--repository-dir", type=Path)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--signer-fingerprint")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    artifact = args.artifact.resolve()
    if not artifact.is_file():
        fail(f"build-once artifact does not exist: {artifact}")
    output = args.output.resolve() if args.output else None
    if output == artifact:
        fail("output JSON must not overwrite the build-once artifact")
    if args.package.startswith("-") or any(character.isspace() for character in args.package):
        fail("invalid npm package name")
    if args.version.startswith("-") or any(character.isspace() for character in args.version):
        fail("invalid npm package version")
    required = {
        "--tag": args.tag,
        "--commit": args.commit,
        "--repository-dir": args.repository_dir,
        "--signer-fingerprint": args.signer_fingerprint,
        # Always required: the tag evidence is bound to this repository in both
        # provenance modes.
        "--source-repository": args.source_repository,
    }
    if args.provenance == "required":
        required["--workflow-path"] = args.workflow_path
    missing = [flag for flag, value in required.items() if not value]
    if missing:
        fail(f"required release binding arguments are missing: {', '.join(missing)}")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.commit):
        fail("release commit must be a full 40-character Git object ID")
    registry_origin(args.registry)
    repository_dir = args.repository_dir.resolve()
    repo_root = Path(
        run(["git", "rev-parse", "--show-toplevel"], repository_dir).stdout.strip()
    ).resolve()
    if artifact.is_relative_to(repo_root):
        fail("build-once artifact must be outside the Git worktree")
    if output is not None and output.is_relative_to(repo_root):
        fail("verification JSON output must be outside the Git worktree")
    artifact_data = artifact.read_bytes()
    artifact_hashes = hashes(artifact_data)

    with tempfile.TemporaryDirectory(prefix="npm-view-") as temp_dir:
        npm_dir = Path(temp_dir)
        view = run(
            ["npm", "view", f"{args.package}@{args.version}", "--json", f"--registry={args.registry}"],
            npm_dir,
            env=credential_free_npm_env(npm_dir),
        )
    try:
        metadata = json.loads(view.stdout)
        dist = metadata["dist"]
        tarball_url = dist["tarball"]
        shasum = dist["shasum"]
        integrity = dist["integrity"]
        signatures = dist["signatures"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        fail(f"npm view returned incomplete distribution metadata: {exc}")
    if not isinstance(signatures, list) or not signatures:
        fail("npm distribution metadata does not contain registry signatures")

    registry_data = download_from_registry(
        tarball_url,
        args.registry,
        max(len(artifact_data) * MAX_DOWNLOAD_MULTIPLE, MAX_DOWNLOAD_FLOOR_BYTES),
    )
    registry_hashes = hashes(registry_data)
    if artifact_data != registry_data:
        fail("build-once artifact is not byte-identical to the registry tarball")
    if artifact_hashes["sha1"] != shasum:
        fail("artifact SHA-1 does not match npm dist.shasum")
    if artifact_hashes["integrity"] != integrity:
        fail("artifact SHA-512 does not match npm dist.integrity")
    embedded_name, embedded_version = embedded_identity(registry_data)
    if embedded_name != args.package or embedded_version != args.version:
        fail(
            "registry tarball identity mismatch: "
            f"expected {args.package}@{args.version}, got {embedded_name}@{embedded_version}"
        )

    with tempfile.TemporaryDirectory(prefix="npm-signature-check-") as temp_dir:
        audit_dir = Path(temp_dir)
        npm_env = credential_free_npm_env(audit_dir)
        (audit_dir / "package.json").write_text(
            json.dumps({"name": "npm-signature-check", "private": True, "version": "0.0.0"}) + "\n",
            encoding="utf-8",
        )
        run(
            [
                "npm",
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                f"--registry={args.registry}",
                f"{args.package}@{args.version}",
            ],
            audit_dir,
            env=npm_env,
        )
        audit_result = run(
            [
                "npm",
                "audit",
                "signatures",
                "--json",
                "--include-attestations",
                f"--registry={args.registry}",
            ],
            audit_dir,
            env=npm_env,
        )

    try:
        audit = json.loads(audit_result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"npm audit signatures returned invalid JSON: {exc}")
    # The exact target must be verified by npm in both modes; only the SLSA
    # attestation itself is optional under --provenance unavailable.
    target = target_verification(audit, args.package, args.version)
    provenance: dict[str, Any] | None = None
    if args.provenance == "required":
        provenance = verify_slsa(
            target,
            artifact_sha512=artifact_hashes["sha512"],
            package=args.package,
            version=args.version,
            repository=args.source_repository,
            workflow_path=args.workflow_path,
            tag=args.tag,
            commit=args.commit.lower(),
        )
    else:
        assert_no_provenance(target)
    tag_verification = verify_release_tag(
        repository_dir,
        args.remote,
        args.tag,
        args.commit.lower(),
        args.signer_fingerprint,
        args.source_repository,
    )

    result: dict[str, Any] = {
        # Every registry read above ran credential-free, so a successful
        # verification is itself the evidence for the asserted access mode.
        "access": args.access,
        "artifact": str(artifact),
        "artifact_digests": artifact_hashes,
        "byte_identical": artifact_data == registry_data,
        "declared_registry_signatures": len(signatures),
        "npm_dist": {"integrity": integrity, "shasum": shasum, "tarball": tarball_url},
        "package": args.package,
        "provenance": (
            {"status": "verified", **provenance} if provenance else {"status": "unavailable"}
        ),
        "registry": args.registry,
        "registry_digests": registry_hashes,
        "release_tag": tag_verification,
        "verified_attestations": provenance.get("matching_bundles", 0) if provenance else 0,
        "version": args.version,
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output:
        output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"verify-published-package: {exc}", file=sys.stderr)
        raise SystemExit(1)
