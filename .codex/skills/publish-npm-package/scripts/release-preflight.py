#!/usr/bin/env python3
"""Fail-closed preflight for an already-built npm release tarball and Git tag."""

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
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

COMMAND_TIMEOUT_SECONDS = 300
MINIMUM_NPM_VERSION = (11, 12, 0)
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
    # Isolate the cache so version absence is proven by a real registry read.
    env["NPM_CONFIG_CACHE"] = str(config_dir / "cache")
    return env


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read JSON from {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"expected a JSON object in {path}")
    return value


def digest(path: Path) -> dict[str, str]:
    data = path.read_bytes()
    sha512 = hashlib.sha512(data)
    return {
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "sha512": sha512.hexdigest(),
        "integrity": "sha512-" + base64.b64encode(sha512.digest()).decode("ascii"),
        "bytes": str(len(data)),
    }


def github_repository(url: str) -> str:
    value = url.removeprefix("git+")
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
        if "?" in path or "#" in path:
            fail(f"repository URL contains unsupported query or fragment: {url}")
    else:
        parsed = urllib.parse.urlparse(value)
        if (
            parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            fail(f"repository URL is not hosted on github.com: {url}")
        path = parsed.path.lstrip("/")
    path = path.rstrip("/").removesuffix(".git")
    if path.count("/") != 1:
        fail(f"repository URL must identify one GitHub owner/repository: {url}")
    # GitHub owner/repository names are case-insensitive; casing must not decide a release.
    return f"github.com/{path.casefold()}"


def inspect_tarball(path: Path, expected_name: str, expected_version: str) -> dict[str, Any]:
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                member_path = PurePosixPath(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    fail(f"unsafe tar entry: {member.name}")
                if not member.isfile() and not member.isdir():
                    fail(f"unsafe tar entry type: {member.name}")
                # npm only unpacks package/; anything outside it is uninspected content.
                if member_path.parts[:1] != ("package",):
                    fail(f"tar entry outside the package root: {member.name}")
            manifests = [member for member in members if member.name == "package/package.json"]
            if len(manifests) != 1:
                fail("tarball must contain exactly one package/package.json")
            if not manifests[0].isfile():
                fail("package/package.json must be a regular file")
            extracted = archive.extractfile(manifests[0])
            if extracted is None:
                fail("cannot read package/package.json from tarball")
            with extracted:
                embedded = json.loads(extracted.read().decode("utf-8"))
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"cannot inspect {path}: {exc}")
    if embedded.get("name") != expected_name or embedded.get("version") != expected_version:
        fail(
            "tarball package identity mismatch: "
            f"expected {expected_name}@{expected_version}, got "
            f"{embedded.get('name')}@{embedded.get('version')}"
        )
    return {"entries": len(members), "name": expected_name, "version": expected_version}


def registry_version_absent(package: str, version: str, registry: str) -> None:
    with tempfile.TemporaryDirectory(prefix="npm-release-preflight-") as temp_dir:
        npm_dir = Path(temp_dir)
        result = run(
            ["npm", "view", f"{package}@{version}", "version", "--json", f"--registry={registry}"],
            npm_dir,
            check=False,
            env=credential_free_npm_env(npm_dir),
        )
    if result.returncode == 0:
        fail(f"registry already contains immutable version {package}@{version}")
    try:
        error = json.loads(result.stdout)["error"]
        error_code = error["code"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        fail(f"could not parse npm's structured version-absence response: {exc}")
    if error_code != "E404":
        fail(f"could not prove registry version absence; npm view returned {error_code}")


def normalize_fingerprint(value: str) -> str:
    compact = value.replace(" ", "")
    if compact.lower().startswith("sha256:"):
        return "SHA256:" + compact.split(":", 1)[1]
    return compact.upper()


def verified_fingerprints(result: subprocess.CompletedProcess[str]) -> set[str]:
    fingerprints: set[str] = set()
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
            for candidate in (fields[2], fields[-1]):
                if re.fullmatch(r"[0-9A-Fa-f]{40,}", candidate):
                    fingerprints.add(normalize_fingerprint(candidate))
        ssh_signature = re.search(
            r'Good "git" signature .* with [^\r\n]* key (SHA256:[A-Za-z0-9+/=]+)$',
            line,
        )
        if ssh_signature:
            fingerprints.add(normalize_fingerprint(ssh_signature.group(1)))
    return fingerprints


def npm_version(cwd: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="npm-version-check-") as temp_dir:
        config_dir = Path(temp_dir)
        value = run(
            ["npm", "--version"], cwd, env=credential_free_npm_env(config_dir)
        ).stdout.strip()
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value)
    if not match:
        fail(f"could not parse npm version: {value!r}")
    parsed = tuple(int(part) for part in match.groups())
    if parsed < MINIMUM_NPM_VERSION:
        required = ".".join(str(part) for part in MINIMUM_NPM_VERSION)
        fail(f"npm {required} or newer is required for attestation verification")
    return value


def validate_release_tag(tag: str, cwd: Path) -> None:
    run(["git", "check-ref-format", f"refs/tags/{tag}"], cwd)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", tag):
        fail("release tag contains characters that are unsafe in displayed shell commands")


def verify_tag_signer(tag: str, expected_fingerprint: str, cwd: Path) -> str:
    result = run(["git", "verify-tag", "--raw", tag], cwd)
    fingerprints = verified_fingerprints(result)
    expected = normalize_fingerprint(expected_fingerprint)
    if expected not in fingerprints:
        fail(f"release tag signer does not match approved fingerprint: {expected_fingerprint}")
    return expected


def remote_tag_object(remote: str, tag: str, cwd: Path) -> str | None:
    result = run(
        ["git", "ls-remote", "--exit-code", "--tags", remote, f"refs/tags/{tag}"],
        cwd,
        check=False,
    )
    if result.returncode == 0:
        fields = result.stdout.split()
        if len(fields) != 2:
            fail("could not parse remote tag result")
        return fields[0]
    if result.returncode != 2:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        fail(f"could not prove remote tag absence: {detail}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--phase", choices=("prepare", "tag", "publish"), default="prepare")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--registry", default="https://registry.npmjs.org")
    parser.add_argument("--access", choices=("public",), required=True)
    parser.add_argument("--signer-fingerprint")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    package_dir = args.package_dir.resolve()
    artifact = args.artifact.resolve()
    if not artifact.is_file():
        fail(f"build-once artifact does not exist: {artifact}")
    output = args.output.resolve() if args.output else None
    if output == artifact:
        fail("output JSON must not overwrite the build-once artifact")
    package_json = load_json(package_dir / "package.json")
    package = package_json.get("name")
    version = package_json.get("version")
    if not isinstance(package, str) or not package or not isinstance(version, str) or not version:
        fail("package.json must contain non-empty string name and version fields")
    if package.startswith("-") or any(character.isspace() for character in package):
        fail("invalid npm package name")
    if version.startswith("-") or any(character.isspace() for character in version):
        fail("invalid npm package version")
    if args.remote.startswith("-"):
        fail("invalid Git remote name")
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
    if package_json.get("private") is True:
        fail("package.json is private and cannot be published")
    publish_config = package_json.get("publishConfig")
    declared_access = publish_config.get("access") if isinstance(publish_config, dict) else None
    if declared_access is not None and declared_access != args.access:
        fail(
            f"package.json publishConfig.access is {declared_access}, "
            f"not the requested {args.access}"
        )
    repository = package_json.get("repository")
    repository_url = repository.get("url") if isinstance(repository, dict) else repository
    if not isinstance(repository_url, str) or not repository_url.strip():
        fail("package.json must declare a repository URL for provenance")

    # Resolve so the in-tree comparison holds when the checkout is reached
    # through a symlinked parent; git reports the physical path.
    repo_root = Path(
        run(["git", "rev-parse", "--show-toplevel"], package_dir).stdout.strip()
    ).resolve()
    if artifact.is_relative_to(repo_root):
        fail("build-once artifact must be outside the Git worktree")
    if output is not None and output.is_relative_to(repo_root):
        fail("preflight JSON output must be outside the Git worktree")
    remote_url = run(["git", "remote", "get-url", args.remote], repo_root).stdout.strip()
    if github_repository(repository_url) != github_repository(remote_url):
        fail("package.json repository does not exactly match the Git release remote")
    dirty = run(["git", "status", "--porcelain", "--untracked-files=all"], repo_root).stdout
    if dirty:
        fail("release worktree is dirty; commit or remove every change before preflight")
    head = run(["git", "rev-parse", "HEAD"], repo_root).stdout.strip()
    validate_release_tag(args.tag, repo_root)
    selected_npm_version = npm_version(repo_root)
    local_tag = run(["git", "show-ref", "--verify", "--quiet", f"refs/tags/{args.tag}"], repo_root, False)
    if local_tag.returncode not in (0, 1):
        detail = local_tag.stderr.strip() or f"exit {local_tag.returncode}"
        fail(f"could not determine local tag state: {detail}")
    remote_tag = remote_tag_object(args.remote, args.tag, repo_root)

    if args.phase == "prepare":
        if local_tag.returncode == 0:
            fail(f"prospective tag already exists locally: {args.tag}")
        if remote_tag is not None:
            fail(f"remote tag already exists and must never be moved: {args.remote}/{args.tag}")
    else:
        if not args.signer_fingerprint:
            fail("tag and publish phases require --signer-fingerprint")
        if local_tag.returncode != 0:
            fail(f"{args.phase} phase requires a local tag: {args.tag}")
        tag_type = run(["git", "cat-file", "-t", f"refs/tags/{args.tag}"], repo_root).stdout.strip()
        if tag_type != "tag":
            fail(f"release tag is not annotated: {args.tag}")
        signer_fingerprint = verify_tag_signer(args.tag, args.signer_fingerprint, repo_root)
        target = run(["git", "rev-list", "-n", "1", args.tag], repo_root).stdout.strip()
        if target != head:
            fail(f"release tag targets {target}, not current HEAD {head}")
        local_tag_object = run(["git", "rev-parse", f"refs/tags/{args.tag}"], repo_root).stdout.strip()
        if args.phase == "tag" and remote_tag is not None:
            fail(f"remote tag already exists and must never be moved: {args.remote}/{args.tag}")
        if args.phase == "publish" and remote_tag != local_tag_object:
            fail("remote release tag does not match the verified local annotated tag object")

    registry_version_absent(package, version, args.registry)
    tarball = inspect_tarball(artifact, package, version)
    result: dict[str, Any] = {
        "artifact": str(artifact),
        "commit": head,
        "digests": digest(artifact),
        "package": package,
        "phase": args.phase,
        "registry": args.registry,
        "npm_version": selected_npm_version,
        "repository": "https://" + github_repository(repository_url),
        "tag": args.tag,
        "tarball": tarball,
        "version": version,
    }
    if args.phase != "prepare":
        result["signer_fingerprint"] = signer_fingerprint
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output:
        output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"release-preflight: {exc}", file=sys.stderr)
        raise SystemExit(1)
