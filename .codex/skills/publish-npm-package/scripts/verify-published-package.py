#!/usr/bin/env python3
"""Compare an npm registry tarball with a build-once artifact and verify attestations."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"


def fail(message: str) -> None:
    raise RuntimeError(message)


def run(
    command: list[str],
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=env)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        fail(f"command failed: {' '.join(command)}: {detail}")
    return result


def credential_free_npm_env(config_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        lowered = key.lower()
        if key.upper() in {"NODE_AUTH_TOKEN", "NPM_TOKEN"} or (
            lowered.startswith("npm_config_")
            and any(fragment in lowered for fragment in ("auth", "password", "token"))
        ):
            env.pop(key)
    user_config = config_dir / "empty-user.npmrc"
    global_config = config_dir / "empty-global.npmrc"
    user_config.write_text("", encoding="utf-8")
    global_config.write_text("", encoding="utf-8")
    env["NPM_CONFIG_USERCONFIG"] = str(user_config)
    env["NPM_CONFIG_GLOBALCONFIG"] = str(global_config)
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
                extracted = archive.extractfile(member)
                if extracted is None:
                    fail("cannot read package/package.json from registry tarball")
                with extracted:
                    package_json = json.loads(extracted.read().decode("utf-8"))
        except (KeyError, OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail(f"cannot inspect registry tarball: {exc}")
    return package_json.get("name"), package_json.get("version")


def target_verification(audit: dict[str, Any], package: str, version: str) -> dict[str, Any]:
    for field in ("invalid", "missing"):
        entries = audit.get(field, [])
        if any(entry.get("name") == package and entry.get("version") == version for entry in entries):
            fail(f"target package appears in npm audit signatures {field} results")
    matches = [
        entry
        for entry in audit.get("verified", [])
        if entry.get("name") == package and entry.get("version") == version
    ]
    if not matches:
        fail("npm audit signatures did not verify the exact target package and version")
    return matches[0]


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
    return f"https://github.com/{path}"


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
) -> dict[str, str]:
    bundles = entry.get("attestationBundles", [])
    for attestation_bundle in bundles:
        if attestation_bundle.get("predicateType") != SLSA_PROVENANCE_V1:
            continue
        try:
            encoded = attestation_bundle["bundle"]["dsseEnvelope"]["payload"]
            statement = json.loads(base64.b64decode(encoded, validate=True))
            subject = statement["subject"]
            predicate = statement["predicate"]
            workflow = predicate["buildDefinition"]["externalParameters"]["workflow"]
            dependencies = predicate["buildDefinition"]["resolvedDependencies"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            fail(f"cannot decode SLSA provenance payload: {exc}")
        if statement.get("predicateType") != SLSA_PROVENANCE_V1:
            continue
        if not any(
            subject_entry.get("digest", {}).get("sha512") == artifact_sha512
            for subject_entry in subject
        ):
            fail("SLSA subject SHA-512 does not match the published artifact")
        expected_subject = f"pkg:npm/{urllib.parse.quote(package, safe='/')}@{version}"
        if not any(subject_entry.get("name") == expected_subject for subject_entry in subject):
            fail("SLSA subject does not identify the target npm package and version")
        expected_repository = github_repository(repository)
        if github_repository(workflow.get("repository", "")) != expected_repository:
            fail("SLSA workflow repository does not match the expected source")
        if workflow.get("path") != workflow_path:
            fail("SLSA workflow path does not match the expected publish workflow")
        if workflow.get("ref") != f"refs/tags/{tag}":
            fail("SLSA workflow ref does not match the expected release tag")
        if not any(
            dependency.get("digest", {}).get("gitCommit") == commit
            for dependency in dependencies
        ):
            fail("SLSA resolved dependencies do not contain the expected release commit")
        return {
            "commit": commit,
            "predicate_type": SLSA_PROVENANCE_V1,
            "repository": expected_repository,
            "tag": tag,
            "workflow_path": workflow_path,
        }
    fail("npm audit signatures did not return a SLSA provenance bundle for the exact target")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--registry", default="https://registry.npmjs.org")
    parser.add_argument("--provenance", choices=("required", "unavailable"), default="required")
    parser.add_argument("--source-repository")
    parser.add_argument("--workflow-path")
    parser.add_argument("--tag")
    parser.add_argument("--commit")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    artifact = args.artifact.resolve()
    if not artifact.is_file():
        fail(f"build-once artifact does not exist: {artifact}")
    if args.package.startswith("-") or any(character.isspace() for character in args.package):
        fail("invalid npm package name")
    if args.version.startswith("-") or any(character.isspace() for character in args.version):
        fail("invalid npm package version")
    if args.provenance == "required":
        required = {
            "--source-repository": args.source_repository,
            "--workflow-path": args.workflow_path,
            "--tag": args.tag,
            "--commit": args.commit,
        }
        missing = [flag for flag, value in required.items() if not value]
        if missing:
            fail(f"required provenance binding arguments are missing: {', '.join(missing)}")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", args.commit):
            fail("release commit must be a full 40-character Git object ID")
    parsed_registry = urllib.parse.urlparse(args.registry)
    if (
        parsed_registry.scheme != "https"
        or not parsed_registry.hostname
        or parsed_registry.username
        or parsed_registry.password
        or parsed_registry.query
        or parsed_registry.fragment
    ):
        fail("registry must be an HTTPS URL without credentials, query, or fragment")
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
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        fail(f"npm view returned incomplete distribution metadata: {exc}")

    registry_host = parsed_registry.hostname
    parsed_tarball = urllib.parse.urlparse(tarball_url)
    if parsed_tarball.scheme != "https" or parsed_tarball.hostname != registry_host:
        fail(f"refusing unexpected registry tarball URL: {tarball_url}")
    try:
        with urllib.request.urlopen(tarball_url, timeout=30) as response:
            final_host = urllib.parse.urlparse(response.geturl()).hostname
            if final_host != registry_host:
                fail(f"refusing registry tarball redirect to unexpected host: {response.geturl()}")
            registry_data = response.read()
    except OSError as exc:
        fail(f"could not download registry tarball: {exc}")
    if artifact_data != registry_data:
        fail("build-once artifact is not byte-identical to the registry tarball")
    registry_hashes = artifact_hashes.copy()
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
    target = target_verification(audit, args.package, args.version)
    provenance: dict[str, str] | None = None
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

    result: dict[str, Any] = {
        "artifact": str(artifact),
        "artifact_digests": artifact_hashes,
        "byte_identical": True,
        "npm_dist": {"integrity": integrity, "shasum": shasum, "tarball": tarball_url},
        "package": args.package,
        "provenance": provenance if provenance else {"status": "unavailable"},
        "registry": args.registry,
        "registry_digests": registry_hashes,
        "verified_attestations": int(provenance is not None),
        "verified_registry_signatures": 1,
        "version": args.version,
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"verify-published-package: {exc}", file=sys.stderr)
        raise SystemExit(1)
