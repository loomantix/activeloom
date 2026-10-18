#!/usr/bin/env python3
"""Read and validate herd orchestration configuration.

Two files configure the herdr orchestration skills (hreview, auto-glance,
hgrill, horch, hpromote):

- `.activeloom-herd.json`, owned by the consumer repository. It is read from the
  integration branch at the remote, never from a pull request head, so a pull
  request cannot loosen its own gate-fix, glance or ship rules.
- `~/.config/activeloom/herd.json`, owned by the user: per-engine launch
  arguments, the worktree root, and the push-notification preference.

Commands print JSON on stdout. Exit codes: 0 ok, 1 invalid configuration,
2 usage or git error, 3 lock already held.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
REPO_FILE = ".activeloom-herd.json"
USER_ENV = "ACTIVELOOM_HERD_USER_CONFIG"
ENGINES = ("claude", "codex", "gemini")
ON_SHIP = ("label", "close", "none")
CHECKS = ("required", "all-runs")
DEFAULT_HOLD_LABEL = "agent-bail: grill-review"

REPO_SECTIONS = (
    "schema_version",
    "integration_branch",
    "worktree",
    "gate_fix",
    "glance",
    "ship",
    "builds",
    "grill",
    "production_lanes",
    "promotion",
)
USER_SECTIONS = ("schema_version", "worktree_root", "push_notifications", "engines")

BRANCH_RE = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)(?!.*\.lock$)[A-Za-z0-9._/-]{1,200}(?<!/)$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ConfigError(Exception):
    """The configuration is invalid; `errors` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class UsageError(Exception):
    """A git, filesystem or argument problem unrelated to configuration validity."""


# --- validation primitives ---------------------------------------------------


def _check_keys(value: dict[str, Any], allowed: tuple[str, ...], where: str, errors: list[str]) -> None:
    for key in sorted(set(value) - set(allowed)):
        errors.append(f"{where}: unknown key {key!r}")


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and value != "" and "\x00" not in value and "\n" not in value


def _string_list(value: Any, where: str, errors: list[str], *, unique: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(_is_text(item) for item in value):
        errors.append(f"{where}: must be an array of non-empty single-line strings")
        return []
    if unique and len(set(value)) != len(value):
        errors.append(f"{where}: entries must be unique")
    return list(value)


def _argv(value: Any, where: str, errors: list[str]) -> list[str]:
    """A command is an argv array, never a shell string."""
    argv = _string_list(value, where, errors)
    if isinstance(value, list) and not value:
        errors.append(f"{where}: a command needs at least one element")
    elif len(argv) == 1 and re.search(r"\s", argv[0]):
        errors.append(f"{where}: looks like a shell string; split it into an argv array")
    return argv


def _argv_list(value: Any, where: str, errors: list[str]) -> list[list[str]]:
    if not isinstance(value, list):
        errors.append(f"{where}: must be an array of argv arrays")
        return []
    return [_argv(item, f"{where}[{index}]", errors) for index, item in enumerate(value)]


def _globs(value: Any, where: str, errors: list[str]) -> list[str]:
    globs = _string_list(value, where, errors)
    for glob in globs:
        if glob.startswith("/") or "\\" in glob or ".." in glob.split("/"):
            errors.append(f"{where}: {glob!r} must be a relative POSIX glob without '..'")
    return globs


def _relative_path(value: Any, where: str, errors: list[str]) -> str | None:
    if not _is_text(value):
        errors.append(f"{where}: must be a non-empty single-line string")
        return None
    if value.startswith("/") or "\\" in value or ".." in value.split("/"):
        errors.append(f"{where}: {value!r} must be a relative POSIX path without '..'")
    return value


def _section(config: dict[str, Any], key: str, errors: list[str]) -> dict[str, Any] | None:
    if key not in config:
        return None
    value = config[key]
    if not isinstance(value, dict):
        errors.append(f"{key}: must be an object")
        return None
    return value


def _engines(value: Any, where: str, errors: list[str]) -> list[str]:
    engines = _string_list(value, where, errors, unique=True)
    if isinstance(value, list) and not value:
        errors.append(f"{where}: must name at least one engine")
    for engine in engines:
        if engine not in ENGINES:
            errors.append(f"{where}: unknown engine {engine!r} (expected one of {', '.join(ENGINES)})")
    return engines


def _optional_text(section: dict[str, Any], key: str, where: str, errors: list[str]) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not _is_text(value):
        errors.append(f"{where}.{key}: must be a non-empty single-line string or null")
        return None
    return value


# --- repository configuration --------------------------------------------------


def validate_repo(config: Any) -> dict[str, Any]:
    """Validate a parsed `.activeloom-herd.json` and return it with defaults applied."""
    errors: list[str] = []
    if not isinstance(config, dict):
        raise ConfigError(["top level: must be a JSON object"])
    _check_keys(config, REPO_SECTIONS, "top level", errors)
    if config.get("schema_version") != SCHEMA_VERSION or isinstance(config.get("schema_version"), bool):
        errors.append(f"schema_version: must be {SCHEMA_VERSION}")

    effective: dict[str, Any] = {"schema_version": SCHEMA_VERSION}

    branch = config.get("integration_branch")
    if branch is not None and (not isinstance(branch, str) or not BRANCH_RE.match(branch)):
        errors.append("integration_branch: must be a branch name")
        branch = None
    effective["integration_branch"] = branch

    worktree = _section(config, "worktree", errors) or {}
    _check_keys(worktree, ("bootstrap",), "worktree", errors)
    effective["worktree"] = {"bootstrap": _argv_list(worktree.get("bootstrap", []), "worktree.bootstrap", errors)}

    gate_fix = _section(config, "gate_fix", errors)
    gate_keys = ("product_globs", "tooling_globs", "test_globs", "off_limits_globs", "extra_checks")
    if gate_fix is None:
        effective["gate_fix"] = {"enabled": False, **{key: [] for key in gate_keys}}
    else:
        _check_keys(gate_fix, gate_keys, "gate_fix", errors)
        effective["gate_fix"] = {
            "enabled": True,
            **{key: _globs(gate_fix.get(key, []), f"gate_fix.{key}", errors) for key in gate_keys[:4]},
            "extra_checks": _argv_list(gate_fix.get("extra_checks", []), "gate_fix.extra_checks", errors),
        }

    glance = _section(config, "glance", errors) or {}
    _check_keys(glance, ("auto",), "glance", errors)
    auto = glance.get("auto", False)
    if not isinstance(auto, bool):
        errors.append("glance.auto: must be true or false")
        auto = False
    effective["glance"] = {"auto": auto}

    ship = _section(config, "ship", errors)
    if ship is None:
        effective["ship"] = {
            "enabled": False,
            "on_ship": "none",
            "label": None,
            "checks": "all-runs",
            "post_ship_checks": [],
        }
    else:
        _check_keys(ship, ("on_ship", "label", "checks", "post_ship_checks"), "ship", errors)
        on_ship = ship.get("on_ship", "none")
        if on_ship not in ON_SHIP:
            errors.append(f"ship.on_ship: must be one of {', '.join(ON_SHIP)}")
        label = _optional_text(ship, "label", "ship", errors)
        if on_ship == "label" and label is None:
            errors.append("ship.label: required when ship.on_ship is 'label'")
        checks = ship.get("checks", "all-runs")
        if checks not in CHECKS:
            errors.append(f"ship.checks: must be one of {', '.join(CHECKS)}")
        effective["ship"] = {
            "enabled": True,
            "on_ship": on_ship,
            "label": label,
            "checks": checks,
            "post_ship_checks": _argv_list(ship.get("post_ship_checks", []), "ship.post_ship_checks", errors),
        }

    builds = _section(config, "builds", errors) or {}
    _check_keys(builds, ("engines",), "builds", errors)
    effective["builds"] = {
        "engines": _engines(builds["engines"], "builds.engines", errors) if "engines" in builds else None
    }

    grill = _section(config, "grill", errors) or {}
    _check_keys(grill, ("engines", "hold_label", "verify_first_label", "persona"), "grill", errors)
    hold = grill.get("hold_label", DEFAULT_HOLD_LABEL)
    if not _is_text(hold):
        errors.append("grill.hold_label: must be a non-empty single-line string")
    persona = grill.get("persona")
    if persona is not None and (not isinstance(persona, str) or not persona.strip() or len(persona) > 600):
        errors.append("grill.persona: must be a non-empty string of at most 600 characters, or null")
    effective["grill"] = {
        "engines": _engines(grill["engines"], "grill.engines", errors) if "engines" in grill else list(ENGINES),
        "hold_label": hold,
        "verify_first_label": _optional_text(grill, "verify_first_label", "grill", errors),
        "persona": persona,
    }

    lanes_value = config.get("production_lanes", [])
    lanes: list[dict[str, Any]] = []
    if not isinstance(lanes_value, list):
        errors.append("production_lanes: must be an array")
    else:
        names: set[str] = set()
        for index, lane in enumerate(lanes_value):
            where = f"production_lanes[{index}]"
            if not isinstance(lane, dict):
                errors.append(f"{where}: must be an object")
                continue
            _check_keys(lane, ("name", "engine", "skill", "brief"), where, errors)
            for required in ("name", "engine", "skill", "brief"):
                if required not in lane:
                    errors.append(f"{where}.{required}: required")
            name = lane.get("name")
            if not isinstance(name, str) or not SLUG_RE.match(name):
                errors.append(f"{where}.name: must be a lower-case slug")
            elif name in names:
                errors.append(f"{where}.name: duplicate lane {name!r}")
            else:
                names.add(name)
            engine = lane.get("engine")
            if engine not in ENGINES:
                errors.append(f"{where}.engine: unknown engine {engine!r}")
            skill = lane.get("skill")
            if not isinstance(skill, str) or not SKILL_RE.match(skill):
                errors.append(f"{where}.skill: must be a skill name")
            brief = _relative_path(lane["brief"], f"{where}.brief", errors) if "brief" in lane else None
            lanes.append({"name": name, "engine": engine, "skill": skill, "brief": brief})
    effective["production_lanes"] = lanes

    promotion = _section(config, "promotion", errors)
    if promotion is None:
        effective["promotion"] = {
            "enabled": False,
            "skill": None,
            "production_branch": None,
            "risk_engine": None,
            "preflight": [],
            "deploy_workflows": [],
            "post_merge": [],
        }
    else:
        allowed = ("skill", "production_branch", "risk_engine", "preflight", "deploy_workflows", "post_merge")
        _check_keys(promotion, allowed, "promotion", errors)
        skill = promotion.get("skill")
        if not isinstance(skill, str) or not SKILL_RE.match(skill):
            errors.append("promotion.skill: required; must be a skill name")
        production = promotion.get("production_branch")
        if not isinstance(production, str) or not BRANCH_RE.match(production):
            errors.append("promotion.production_branch: required; must be a branch name")
        elif production == branch:
            errors.append("promotion.production_branch: must differ from integration_branch")
        risk = promotion.get("risk_engine")
        if risk is not None and risk not in ENGINES:
            errors.append(f"promotion.risk_engine: unknown engine {risk!r}")
        effective["promotion"] = {
            "enabled": True,
            "skill": skill,
            "production_branch": production,
            "risk_engine": risk,
            "preflight": _argv_list(promotion.get("preflight", []), "promotion.preflight", errors),
            "deploy_workflows": _string_list(
                promotion.get("deploy_workflows", []), "promotion.deploy_workflows", errors, unique=True
            ),
            "post_merge": _argv_list(promotion.get("post_merge", []), "promotion.post_merge", errors),
        }

    if errors:
        raise ConfigError(errors)
    return effective


def parse_json(text: str, where: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError([f"{where}: not valid JSON ({exc.msg} at line {exc.lineno})"]) from exc


# --- reading from the integration branch at the remote -------------------------


def _git(repo_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise UsageError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def default_branch(repo_dir: Path, remote: str) -> str:
    local = _git(repo_dir, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD", check=False)
    prefix = f"refs/remotes/{remote}/"
    if local.returncode == 0 and local.stdout.strip().startswith(prefix):
        return local.stdout.strip()[len(prefix) :]
    remote_head = _git(repo_dir, "ls-remote", "--symref", remote, "HEAD", check=False)
    for line in remote_head.stdout.splitlines():
        match = re.match(r"^ref: refs/heads/(\S+)\s+HEAD$", line)
        if match:
            return match.group(1)
    raise UsageError(f"cannot resolve the default branch of {remote!r}; pass --default-branch")


def _fetch(repo_dir: Path, remote: str, branch: str) -> None:
    _git(repo_dir, "fetch", "--quiet", "--no-tags", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")


def _read_at(repo_dir: Path, remote: str, branch: str) -> tuple[str, str | None]:
    ref = f"refs/remotes/{remote}/{branch}"
    commit = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if commit.returncode != 0:
        raise UsageError(f"{remote}/{branch} does not exist locally; fetch it or drop --no-fetch")
    shown = _git(repo_dir, "show", f"{ref}:{REPO_FILE}", check=False)
    return commit.stdout.strip(), (shown.stdout if shown.returncode == 0 else None)


def read_repo(repo_dir: Path, remote: str, default: str | None, fetch: bool) -> dict[str, Any]:
    """Return the effective repository configuration and the commit it came from.

    The file is read from the default branch first. When it names a different
    integration branch, the copy on that branch is authoritative and must name
    the same integration branch, so neither branch can redirect the other.
    """
    if remote.startswith("-") or not KEY_RE.match(remote):
        raise UsageError(f"invalid remote name {remote!r}")
    branch = default or default_branch(repo_dir, remote)
    if not BRANCH_RE.match(branch):
        raise UsageError(f"invalid branch name {branch!r}")
    if fetch:
        _fetch(repo_dir, remote, branch)
    commit, text = _read_at(repo_dir, remote, branch)
    if text is None:
        effective = validate_repo({"schema_version": SCHEMA_VERSION})
        effective["integration_branch"] = branch
        return {"source": {"ref": f"{remote}/{branch}", "commit": commit, "present": False}, "config": effective}
    where = f"{remote}/{branch}:{REPO_FILE}"
    effective = validate_repo(parse_json(text, where))
    integration = effective["integration_branch"] or branch
    if integration != branch:
        if fetch:
            _fetch(repo_dir, remote, integration)
        commit, text = _read_at(repo_dir, remote, integration)
        where = f"{remote}/{integration}:{REPO_FILE}"
        if text is None:
            raise ConfigError([f"{where}: missing; the integration branch must carry the configuration it is named in"])
        effective = validate_repo(parse_json(text, where))
        if (effective["integration_branch"] or integration) != integration:
            raise ConfigError([f"{where}: names integration branch {effective['integration_branch']!r}, not {integration!r}"])
        branch = integration
    if effective["promotion"]["production_branch"] == branch:
        raise ConfigError([f"{where}: promotion.production_branch: must differ from integration_branch"])
    effective["integration_branch"] = branch
    return {"source": {"ref": f"{remote}/{branch}", "commit": commit, "present": True}, "config": effective}


# --- gate-fix path classification ----------------------------------------------


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a path glob: `**` spans directories, `*` and `?` stay in one segment."""
    out: list[str] = []
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return re.compile("^" + "".join(out) + "$")


def classify_paths(gate_fix: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """Classify changed paths for a herder-authored gate fix. Fails closed."""
    order = (
        ("off_limits", gate_fix["off_limits_globs"]),
        ("test", gate_fix["test_globs"]),
        ("product", gate_fix["product_globs"]),
        ("tooling", gate_fix["tooling_globs"]),
    )
    compiled = [(name, [glob_to_regex(glob) for glob in globs]) for name, globs in order]
    files = []
    for path in paths:
        kind = next((name for name, patterns in compiled if any(p.match(path) for p in patterns)), "other")
        files.append({"path": path, "class": kind})
    reasons = []
    if not gate_fix["enabled"]:
        reasons.append("gate_fix is not configured for this repository")
    if not paths:
        reasons.append("the fix changes no files")
    for kind, why in (
        ("off_limits", "touches an off-limits path"),
        ("product", "touches product code"),
        ("other", "touches a path no tooling or test glob covers"),
    ):
        hits = [entry["path"] for entry in files if entry["class"] == kind]
        if hits:
            reasons.append(f"{why}: {', '.join(hits)}")
    return {"eligible": not reasons, "reasons": reasons, "files": files}


def changed_paths(repo_dir: Path, base: str, head: str) -> list[str]:
    for value in (base, head):
        if value.startswith("-"):
            raise UsageError(f"invalid revision {value!r}")
    result = _git(repo_dir, "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}")
    return [path for path in result.stdout.split("\x00") if path]


# --- per-user configuration ------------------------------------------------------


def user_config_path() -> Path:
    override = os.environ.get(USER_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "activeloom" / "herd.json"


def validate_user(config: Any) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(config, dict):
        raise ConfigError(["top level: must be a JSON object"])
    _check_keys(config, USER_SECTIONS, "top level", errors)
    if config.get("schema_version") != SCHEMA_VERSION or isinstance(config.get("schema_version"), bool):
        errors.append(f"schema_version: must be {SCHEMA_VERSION}")
    root = config.get("worktree_root", "~/wt")
    if not _is_text(root):
        errors.append("worktree_root: must be a non-empty single-line string")
        root = "~/wt"
    push = config.get("push_notifications", True)
    if not isinstance(push, bool):
        errors.append("push_notifications: must be true or false")
        push = True
    engines_value = config.get("engines", {})
    engines: dict[str, dict[str, list[str]]] = {}
    if not isinstance(engines_value, dict):
        errors.append("engines: must be an object")
        engines_value = {}
    _check_keys(engines_value, ENGINES, "engines", errors)
    for engine in ENGINES:
        entry = engines_value.get(engine, {})
        where = f"engines.{engine}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object")
            entry = {}
        _check_keys(entry, ("launch_args", "permission_off_args"), where, errors)
        launch = _string_list(entry.get("launch_args", []), f"{where}.launch_args", errors)
        permission = _string_list(entry.get("permission_off_args", []), f"{where}.permission_off_args", errors)
        flags = [arg.split("=", 1)[0] for arg in launch + permission if arg.startswith("-")]
        duplicates = sorted({flag for flag in flags if flags.count(flag) > 1})
        if duplicates:
            errors.append(f"{where}: flag passed more than once: {', '.join(duplicates)}")
        engines[engine] = {"launch_args": launch, "permission_off_args": permission}
    if errors:
        raise ConfigError(errors)
    return {
        "schema_version": SCHEMA_VERSION,
        "worktree_root": str(Path(root).expanduser()),
        "push_notifications": push,
        "engines": engines,
    }


def read_user(path: Path) -> dict[str, Any]:
    if not path.exists():
        effective = validate_user({"schema_version": SCHEMA_VERSION})
        return {"source": {"path": str(path), "present": False}, "config": effective}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc}") from exc
    effective = validate_user(parse_json(text, str(path)))
    return {"source": {"path": str(path), "present": True}, "config": effective}


# --- state paths and locks -------------------------------------------------------


def flat_repo(repo: str) -> str:
    if not REPO_RE.match(repo) or ".." in repo.split("/"):
        raise UsageError(f"repository must be owner/name, got {repo!r}")
    owner, name = repo.split("/")
    return f"{owner}__{name}"


def state_paths(repo: str, skill: str, key: str | None) -> dict[str, str]:
    if not SKILL_RE.match(skill):
        raise UsageError(f"invalid skill name {skill!r}")
    if key is not None and not KEY_RE.match(key):
        raise UsageError(f"invalid key {key!r}")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    tmp = Path(os.environ.get("TMPDIR") or "/tmp")
    parts = [flat_repo(repo), skill] + ([key] if key else [])
    return {
        "state": str(cache.joinpath("activeloom-herd", *parts)),
        "scratch": str(tmp.joinpath("activeloom-herd", *parts)),
        "lock": str(cache.joinpath("activeloom-herd", flat_repo(repo), "locks", f"{skill}-{key or 'all'}")),
    }


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


# --- command line -------------------------------------------------------------------


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate a working-tree .activeloom-herd.json")
    validate.add_argument("file", nargs="?", default=REPO_FILE)

    def add_source(command: argparse.ArgumentParser) -> None:
        command.add_argument("--repo-dir", default=".", type=Path)
        command.add_argument("--remote", default="origin")
        command.add_argument("--default-branch")
        command.add_argument("--no-fetch", action="store_true")

    repo = commands.add_parser("repo", help="effective repository config from the integration branch")
    add_source(repo)

    gate = commands.add_parser("gate-fix-paths", help="classify a gate fix's changed paths")
    add_source(gate)
    gate.add_argument("--base", required=True)
    gate.add_argument("--head", required=True)

    user = commands.add_parser("user", help="effective per-user config")
    user.add_argument("--path", type=Path)

    user_args = commands.add_parser("user-args", help="extra launch argv for one engine")
    user_args.add_argument("--engine", required=True, choices=ENGINES)
    user_args.add_argument("--read-only", action="store_true", help="omit permission-off arguments")
    user_args.add_argument("--path", type=Path)

    paths = commands.add_parser("paths", help="create and print per-repository state paths")
    lock = commands.add_parser("lock", help="take a per-repository lock (exit 3 if held)")
    unlock = commands.add_parser("unlock", help="release a per-repository lock")
    for command in (paths, lock, unlock):
        command.add_argument("--repo", required=True)
        command.add_argument("--skill", required=True)
        command.add_argument("--key")

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            path = Path(args.file)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise UsageError(f"cannot read {path}: {exc}") from exc
            _emit(validate_repo(parse_json(text, str(path))))
        elif args.command == "repo":
            _emit(read_repo(args.repo_dir, args.remote, args.default_branch, not args.no_fetch))
        elif args.command == "gate-fix-paths":
            loaded = read_repo(args.repo_dir, args.remote, args.default_branch, not args.no_fetch)
            result = classify_paths(loaded["config"]["gate_fix"], changed_paths(args.repo_dir, args.base, args.head))
            _emit({"source": loaded["source"], **result})
        elif args.command == "user":
            _emit(read_user(args.path or user_config_path()))
        elif args.command == "user-args":
            entry = read_user(args.path or user_config_path())["config"]["engines"][args.engine]
            _emit(entry["launch_args"] + ([] if args.read_only else entry["permission_off_args"]))
        else:
            resolved = state_paths(args.repo, args.skill, args.key)
            if args.command == "paths":
                _private_dir(Path(resolved["state"]))
                _private_dir(Path(resolved["scratch"]))
                _emit(resolved)
            elif args.command == "lock":
                lock_path = Path(resolved["lock"])
                _private_dir(lock_path.parent)
                try:
                    lock_path.mkdir(mode=0o700)
                except FileExistsError:
                    print(f"lock held: {lock_path}", file=sys.stderr)
                    return 3
                _emit({"lock": str(lock_path)})
            else:
                lock_path = Path(resolved["lock"])
                if lock_path.is_dir():
                    lock_path.rmdir()
                _emit({"released": str(lock_path)})
    except ConfigError as exc:
        for error in exc.errors:
            print(f"invalid: {error}", file=sys.stderr)
        return 1
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
