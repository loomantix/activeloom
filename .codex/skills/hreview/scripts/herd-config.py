#!/usr/bin/env python3
"""Read and validate herd orchestration configuration.

Two files configure the herdr orchestration skills (hreview, auto-glance,
hgrill, horch, hpromote):

- `.activeloom-herd.json`, owned by the consumer repository. It is read from the
  integration branch at the remote, never from a pull request head, so a pull
  request cannot loosen its own gate-fix, glance or ship rules.
- `~/.config/activeloom/herd.json`, owned by the user: per-engine launch
  arguments, the worktree root, and the push-notification preference.

The preflight also reads two things outside both files: the user's shell
aliases for each engine CLI, and, when Claude Code runs in auto mode, the Claude
settings files. It never writes either.

A third, user-owned file family holds restart approvals: one record per
`<repo>#<pr>` at an exact head, created by `approve-restart`, which the user
runs in their own terminal. A herder claims a record (single use) before it
restarts an ended review run; an orchestrator may print the command but never
runs it. The terminal prompt guards against accidents; it is not a barrier
against a process running as the same user, so the skills' rule that agents
never approve or write a record is the control.

Commands print JSON on stdout. Exit codes: 0 ok, 1 invalid configuration,
2 usage or git error, 3 lock already held, 4 no valid restart approval.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
REPO_FILE = ".activeloom-herd.json"
USER_ENV = "ACTIVELOOM_HERD_USER_CONFIG"
ENGINES = ("claude", "codex", "gemini")
ON_SHIP = ("label", "close", "none")
CHECKS = ("required", "all-runs")
TELEMETRY_STORES = ("pr", "issue")
# The review ledger reads its metrics destination from this variable. Herd sets
# it on every pane it opens, so the repository decides, not the engine harness.
TELEMETRY_STORE_ENV = "LOOM_REVIEW_TELEMETRY_STORE"
DEFAULT_HOLD_LABEL = "agent-bail: grill-review"
# The generic recipe-driven skill; a repository may name its own instead.
DEFAULT_PROMOTION_SKILL = "promote"

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
    "telemetry",
    "manual_tests",
)
USER_SECTIONS = ("schema_version", "worktree_root", "push_notifications", "engines", "promotion")

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


class ApprovalError(Exception):
    """No restart approval that matches and is still valid."""


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
        skill = promotion.get("skill", DEFAULT_PROMOTION_SKILL)
        if not isinstance(skill, str) or not SKILL_RE.match(skill):
            errors.append("promotion.skill: must be a skill name")
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

    telemetry = _section(config, "telemetry", errors) or {}
    _check_keys(telemetry, ("store",), "telemetry", errors)
    store = telemetry.get("store")
    if store is not None and store not in TELEMETRY_STORES:
        errors.append(f"telemetry.store: must be one of {', '.join(TELEMETRY_STORES)}")
        store = None
    effective["telemetry"] = {"store": store}

    manual = _section(config, "manual_tests", errors) or {}
    _check_keys(manual, ("ux_globs",), "manual_tests", errors)
    effective["manual_tests"] = {"ux_globs": _globs(manual.get("ux_globs", []), "manual_tests.ux_globs", errors)}

    if errors:
        raise ConfigError(errors)
    return effective


def pane_env(config: dict[str, Any]) -> list[str]:
    """`KEY=VALUE` entries herd passes as `--env` to every pane it opens, any engine."""
    store = config["telemetry"]["store"]
    return [] if store is None else [f"{TELEMETRY_STORE_ENV}={store}"]


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


def ux_paths(manual_tests: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """Changed paths that match the repository's manual-test UX globs."""
    patterns = [glob_to_regex(glob) for glob in manual_tests["ux_globs"]]
    matches = [path for path in paths if any(p.match(path) for p in patterns)]
    return {"ux_globs": list(manual_tests["ux_globs"]), "matches": matches, "match": bool(matches)}


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
    promotion = config.get("promotion", {})
    if not isinstance(promotion, dict):
        errors.append("promotion: must be an object")
        promotion = {}
    _check_keys(promotion, ("offer_manual_tests",), "promotion", errors)
    offer = promotion.get("offer_manual_tests", False)
    if not isinstance(offer, bool):
        errors.append("promotion.offer_manual_tests: must be true or false")
        offer = False
    if errors:
        raise ConfigError(errors)
    return {
        "schema_version": SCHEMA_VERSION,
        "worktree_root": str(Path(root).expanduser()),
        "push_notifications": push,
        "engines": engines,
        "promotion": {"offer_manual_tests": offer},
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


# Flags each CLI documents as changing a safety check, by value. Each value maps
# to (what it turns off, whether sessions stop asking); None for what it turns
# off marks a value that turns nothing off, so `-s read-only` is recognized, not
# a risk. A flag with no value is keyed by None. Codex `-c key=value` overrides
# are classified by key. The tables only classify flags and launch nothing; they
# are locked so that any edit to them is reviewed
# (.claude/claude-cli-invocations.allowlist).
# claude-cli-invocations:start
FlagValues = dict[str | None, tuple[str | None, bool]]
CODEX_APPROVAL: FlagValues = {
    "never": ("approval prompts", True),
    "on-request": (None, False),
    "on-failure": (None, False),
    "untrusted": (None, False),
}
CODEX_SANDBOX: FlagValues = {
    "danger-full-access": ("the sandbox", False),
    "workspace-write": (None, False),
    "read-only": (None, False),
}
CODEX_BYPASS: FlagValues = {None: ("approval prompts and the sandbox", True)}
PERMISSION_FLAGS: dict[str, dict[str, FlagValues]] = {
    "claude": {
        "--dangerously-skip-permissions": {None: ("every permission check", True)},
        "--permission-mode": {
            "bypassPermissions": ("every permission check", True),
            "auto": ("approval prompts; a classifier allows or refuses each action instead", True),
            "dontAsk": ("approval prompts; anything not allowed in advance is refused", True),
            "acceptEdits": ("approval prompts for file edits", False),
            "manual": (None, False),
            "plan": (None, False),
        },
    },
    "codex": {
        "--dangerously-bypass-approvals-and-sandbox": CODEX_BYPASS,
        "--yolo": CODEX_BYPASS,
        "--approve-for-me": {
            None: ("approval prompts; an automatic reviewer decides each request to leave the sandbox", True)
        },
        # Removed from current CLIs; older ones read it as sandboxed auto-approval.
        "--full-auto": {None: ("approval prompts for commands the workspace-write sandbox allows", False)},
        "-a": CODEX_APPROVAL,
        "--ask-for-approval": CODEX_APPROVAL,
        "-s": CODEX_SANDBOX,
        "--sandbox": CODEX_SANDBOX,
    },
    "gemini": {
        "--dangerously-skip-permissions": {None: ("every permission check", True)},
        "--mode": {
            "accept-edits": ("approval prompts for file edits", False),
            "plan": (None, False),
        },
    },
}
CODEX_CONFIG_KEYS: dict[str, FlagValues] = {
    "approval_policy": CODEX_APPROVAL,
    "sandbox_mode": CODEX_SANDBOX,
    "approvals_reviewer": {
        "auto_review": ("approval prompts; an automatic reviewer decides each request to leave the sandbox", True),
        "user": (None, False),
    },
    "sandbox_workspace_write.network_access": {
        "true": ("the sandbox's network block", False),
        "false": (None, False),
    },
}
CODEX_CONFIG_FLAGS = ("-c", "--config")
# Flags every herd launch passes itself, outside herd.json.
HERD_FLAGS = {
    "claude": ("--model", "--effort"),
    "codex": ("-m",),
    "gemini": ("--model", "--effort"),
}
HERD_READ_ONLY_FLAGS = {
    "claude": ("--permission-mode",),
    "codex": ("-s",),
    "gemini": ("--mode",),
}
# Two spellings of one flag; the CLI counts them as the same flag.
FLAG_SPELLINGS = {
    "codex": {
        "--yolo": "--dangerously-bypass-approvals-and-sandbox",
        "--ask-for-approval": "-a",
        "--sandbox": "-s",
        "--model": "-m",
    },
}
# Codex accepts `-c` more than once, so it never counts as a repeat.
REPEATABLE = {"codex": CODEX_CONFIG_FLAGS}
# The Claude mode whose classifier needs the herd's settings entries.
CLAUDE_AUTO_MODE = ("--permission-mode", "auto")
# claude-cli-invocations:end
PERMISSION_SCOPE = (
    "herd.json, plus `type <cli>` in the user's shell for aliases; engine config files such as "
    "~/.codex/config.toml and Claude settings are not read (claude-settings checks Claude settings for auto mode)"
)


def _classify(engine: str, flag: str, value: str | None) -> tuple[str | None, bool] | None:
    """What `flag value` turns off, or None when the table does not know it."""
    if engine == "codex" and flag in CODEX_CONFIG_FLAGS and value is not None:
        key, has_value, raw = value.partition("=")
        if not has_value:
            return None
        # The CLI parses the value as TOML, so a quoted string is the same setting.
        literal = raw.strip()
        if len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in "\"'":
            literal = literal[1:-1]
        return CODEX_CONFIG_KEYS.get(key.strip(), {}).get(literal)
    return PERMISSION_FLAGS[engine].get(flag, {}).get(value)


def _takes_value(engine: str, flag: str) -> bool:
    if engine == "codex" and flag in CODEX_CONFIG_FLAGS:
        return True
    values = PERMISSION_FLAGS[engine].get(flag)
    return values is not None and None not in values


def _turned_off(engine: str, args: list[str]) -> tuple[list[dict[str, str]], list[str], bool]:
    """Flags that turn a check off, flags the table cannot classify, and
    whether the recognized flags stop sessions asking."""
    found: list[dict[str, str]] = []
    unrecognized: list[str] = []
    ends_prompts = False
    index = 0
    while index < len(args):
        arg = args[index]
        flag, has_inline, inline = arg.partition("=")
        value: str | None = None
        shown = arg
        index += 1
        if has_inline:
            value = inline
        elif _takes_value(engine, flag):
            if index < len(args):
                value = args[index]
                shown = f"{arg} {value}"
                index += 1
        known = None if value is None and _takes_value(engine, flag) else _classify(engine, flag, value)
        if known is None:
            unrecognized.append(shown)
            continue
        turns_off, ends = known
        ends_prompts = ends_prompts or ends
        if turns_off is not None:
            found.append({"arg": shown, "turns_off": turns_off})
    return found, unrecognized, ends_prompts


def permission_report(
    loaded: dict[str, Any],
    engines: list[str],
    type_texts: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """What a session that edits will and won't be asked about, per engine.

    `launch_args` reach read-only sessions too, so a flag there that turns a
    check off is reported separately: it defeats the read-only launch. So does
    a shell alias that adds one, and an alias flag the herd also passes makes
    the CLI exit.
    """
    report: dict[str, Any] = {}
    for engine in engines:
        entry = loaded["config"]["engines"][engine]
        turned_off, unrecognized, ends_off = _turned_off(engine, entry["permission_off_args"])
        in_launch, unknown_launch, ends_launch = _turned_off(engine, entry["launch_args"])
        report[engine] = {
            "stops_at_prompts": not (ends_off or ends_launch),
            "turned_off": turned_off,
            "unrecognized": unknown_launch + unrecognized,
            "turned_off_in_read_only": in_launch,
            "alias": alias_report(engine, entry, (type_texts or {}).get(engine)),
        }
    return {"source": loaded["source"], "scope": PERMISSION_SCOPE, "engines": report}


# --- shell aliases ---------------------------------------------------------------

ENGINE_CLIS = {"claude": "claude", "codex": "codex", "gemini": "agy"}
ALIAS_PATTERNS = (
    re.compile(r"^(?P<cli>\S+) is aliased to [`'‘](?P<body>.*)['’]$"),  # bash
    re.compile(r"^(?P<cli>\S+) is an alias for (?P<body>.*)$"),  # zsh
)
FUNCTION_RE = re.compile(r"^(?P<cli>\S+) is (?:a|a shell) function\b")


def _canonical(engine: str, arg: str) -> str:
    flag = arg.split("=", 1)[0]
    return FLAG_SPELLINGS.get(engine, {}).get(flag, flag)


def parse_type_output(cli: str, text: str) -> dict[str, Any]:
    """What `type <cli>` reports: an alias and its words, a function, or neither.

    `kind` is None when no line describes `cli`: the shell exited early, loaded
    no aliases, or printed `type` in another language. That is not a check.
    """
    described = False
    for raw in text.splitlines():
        line = raw.strip()
        for pattern in ALIAS_PATTERNS:
            match = pattern.match(line)
            if match and match.group("cli") == cli:
                body = match.group("body")
                try:
                    words = shlex.split(body)
                except ValueError:
                    words = body.split()
                return {"kind": "alias", "definition": body, "words": words}
        match = FUNCTION_RE.match(line)
        if match and match.group("cli") == cli:
            return {"kind": "function", "definition": None, "words": []}
        described = described or line.startswith(f"{cli} is ")
    return {"kind": "none" if described else None, "definition": None, "words": []}


def probe_type(cli: str, shell: str) -> str | None:
    """`type <cli>` in an interactive shell, which loads the user's aliases."""
    try:
        result = subprocess.run(
            [shell, "-ic", f"type {shlex.quote(cli)}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout


def alias_report(
    engine: str, entry: dict[str, list[str]], type_text: str | None
) -> dict[str, Any]:
    """Flags an alias adds to every launch, and those the herd also passes."""
    cli = ENGINE_CLIS[engine]
    parsed = parse_type_output(cli, type_text) if type_text is not None else None
    if parsed is None or parsed["kind"] is None:
        return {
            "cli": cli,
            "checked": False,
            "kind": None,
            "flags": [],
            "repeats": [],
            "read_only_repeats": [],
            "turned_off": [],
        }
    words = parsed["words"]
    first_flag = next(
        (index for index, word in enumerate(words) if word.startswith("-")), len(words)
    )
    alias_args = words[first_flag:]
    repeatable = REPEATABLE.get(engine, ())
    configured = {
        _canonical(engine, arg)
        for arg in [
            *entry["launch_args"],
            *entry["permission_off_args"],
            *HERD_FLAGS[engine],
        ]
        if arg.startswith("-")
    }
    read_only_configured = {
        _canonical(engine, arg)
        for arg in [
            *entry["launch_args"],
            *HERD_FLAGS[engine],
            *HERD_READ_ONLY_FLAGS[engine],
        ]
        if arg.startswith("-")
    }
    added = [
        arg
        for arg in alias_args
        if arg.startswith("-") and arg.split("=", 1)[0] not in repeatable
    ]
    repeats = sorted({_canonical(engine, arg) for arg in added} & configured)
    turned_off, _, _ = _turned_off(engine, alias_args)
    return {
        "cli": cli,
        "checked": True,
        "kind": parsed["kind"],
        # Flag names only: an alias body can carry inline credentials
        # (`KEY=... claude`), and flag values can too.
        "flags": sorted({arg.split("=", 1)[0] for arg in alias_args if arg.startswith("-")}),
        "repeats": repeats,
        "read_only_repeats": sorted(
            {_canonical(engine, arg) for arg in added} & read_only_configured
        ),
        "turned_off": turned_off,
    }


# --- Claude settings for auto mode ----------------------------------------------------

# Claude Code ignores autoMode in shared project settings.
AUTO_MODE_SCOPES = ("user", "local")
AUTO_MODE_SCOPE = (
    "Claude user, project and local settings; autoMode is read from user and local settings only, "
    "as Claude Code ignores it in shared project settings; managed settings are not read"
)
DIALOG_RULE = (
    "Reading a herdr pane this session started (`herdr agent read`) and answering its startup dialog with "
    "`herdr agent send-keys` is expected herd orchestration: trusting the folder when it is the primary checkout "
    "or a worktree this run created, and dismissing an update banner. This does not cover approving another "
    "session's permission or tool-use prompt."
)


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _home_relative(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


def _read_settings(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc}") from exc
    value = parse_json(text, str(path))
    if not isinstance(value, dict):
        raise ConfigError([f"{path}: top level must be a JSON object"])
    return value


def _string_entries(settings: dict[str, Any] | None, *keys: str) -> list[str]:
    value: Any = settings or {}
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def rule_covers(rule: str, command: str) -> bool:
    """Whether a permission rule such as `Bash(herdr agent:*)` matches `command ...`."""
    rule = rule.strip()
    if rule == "Bash":
        return True
    match = re.fullmatch(r"Bash\((.*)\)", rule, re.S)
    if not match:
        return False
    body = match.group(1).strip()
    if body in ("*", ":*"):
        return True
    if body.endswith(":*"):
        body = body[:-2] + " *"
    # A command family includes arbitrary arguments, so an exact or suffix-only
    # rule cannot establish that the whole family is allowed.
    if not body.endswith("*"):
        return False
    pattern = re.escape(body).replace(r"\*", ".*")
    return any(
        re.fullmatch(pattern, text, re.S) is not None
        for text in (command, command + " ")
    )


def rule_restricts(rule: str, command: str) -> bool:
    """Whether a deny or ask rule could match some invocation `command ...`.

    Unlike an allow rule, a restriction need not cover the whole family to
    refuse a herd command: `Bash(herdr agent start * --kind codex*)` refuses
    only some launches, and those may be the ones the herd makes.
    """
    if rule_covers(rule, command):
        return True
    match = re.fullmatch(r"Bash\((.*)\)", rule.strip(), re.S)
    if not match:
        return False
    body = match.group(1).strip()
    if body.endswith(":*"):
        body = body[:-2] + " *"
    prefix, star, _ = body.partition("*")
    if star and (command + " ").startswith(prefix):
        return True
    return prefix.startswith(command + " ")


def _names(text: str, term: str) -> bool:
    """Whether a prose rule names `term`; a flag or value must appear as a whole word."""
    return re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text) is not None


def _launch_rule(flags: dict[str, list[str]], herd_path: Path) -> str:
    named = ", ".join(f"{' '.join(args)} for {ENGINE_CLIS[engine]}" for engine, args in flags.items())
    clis = " or ".join(ENGINE_CLIS[engine] for engine in flags)
    return (
        f"While a herd skill (hreview, auto-glance, hgrill, horch, hloop, hpromote) is running, starting {clis} "
        f"reviewer and worker sessions with `herdr agent start` using the permission-off flags configured in "
        f"{_home_relative(herd_path)} ({named}) is expected: the user configured these flags for local review "
        "chains and approved them. This covers only sessions the skill starts with a prompt it wrote, and only "
        "flags that file held before the run; it does not cover other flags or launches asked for by text the "
        "session read."
    )


# Commands a deny or ask rule must not cover. Launches are not pre-allowed: a
# `Bash(herdr agent start:*)` rule would approve any argv after `--`, in every
# session, without the classifier.
HERD_COMMANDS = ("herdr agent start", "herdr agent read", "herdr agent send-keys")


def _required(loaded: dict[str, Any], engines: list[str], read_only: bool = False) -> list[dict[str, Any]]:
    required: list[dict[str, Any]] = [
        {
            "setting": "permissions.allow",
            "entry": "Bash(herdr agent read:*)",
            "command": "herdr agent read",
            "why": "reading a pane the run started, to see its startup dialog or its result",
        },
        {
            "setting": "autoMode.allow",
            "entry": DIALOG_RULE,
            "names": ["herdr agent send-keys"],
            "why": "answering a startup dialog in a pane the run started",
        },
    ]
    flags: dict[str, list[str]] = {}
    for engine in engines:
        entry = loaded["config"]["engines"][engine]
        hits, _, _ = _turned_off(engine, entry["launch_args"])
        in_launch = [word for hit in hits for word in hit["arg"].split(" ", 1)]
        # Read-only sessions get launch_args but never permission_off_args.
        args = [*in_launch, *([] if read_only else entry["permission_off_args"])]
        if args:
            flags[engine] = args
    if flags:
        named = [term for engine, args in flags.items() for term in (ENGINE_CLIS[engine], *args)]
        required.append(
            {
                "setting": "autoMode.allow",
                "entry": _launch_rule(flags, Path(loaded["source"]["path"])),
                "names": ["herdr agent start", *dict.fromkeys(named)],
                "why": "starting sessions with herd.json's permission-off flags; auto mode treats that as creating an unsafe agent",
            }
        )
    return required


def _auto_mode_reasons(
    session_mode: str | None,
    loaded: dict[str, Any],
    engines: list[str],
    settings: dict[str, dict[str, Any] | None],
    read_only: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if session_mode == "auto":
        reasons.append("this session runs in auto mode")
    if "claude" in engines:
        entry = loaded["config"]["engines"]["claude"]
        args = entry["launch_args"] + ([] if read_only else entry["permission_off_args"])
        flag, mode = CLAUDE_AUTO_MODE
        pairs = zip(args, [*args[1:], None])
        if any(arg == f"{flag}={mode}" or (arg, value) == CLAUDE_AUTO_MODE for arg, value in pairs):
            reasons.append("herd.json starts Claude sessions in auto mode")
    for scope, value in settings.items():
        if isinstance(value, dict) and isinstance(value.get("permissions"), dict):
            if value["permissions"].get("defaultMode") == "auto":
                reasons.append(f"{scope} Claude settings set permissions.defaultMode to auto")
    return reasons


def _private_file(path: Path, text: str) -> None:
    _private_dir(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)


def copy_command(draft: Path, target: Path, stamp: str) -> str:
    """Back up the settings file, validate the draft with jq, then copy it in."""
    quoted_target = shlex.quote(str(target))
    quoted_draft = shlex.quote(str(draft))
    backup = f"cp -p {quoted_target} {shlex.quote(f'{target}.bak-{stamp}')}"
    if not target.exists():
        backup = f"mkdir -p {shlex.quote(str(target.parent))}"
    return f"{backup} && jq -e . {quoted_draft} >/dev/null && cp {quoted_draft} {quoted_target}"


def auto_mode_report(
    loaded: dict[str, Any],
    engines: list[str],
    session_mode: str | None,
    project_dir: Path | None,
    draft_path: Path,
    read_only: bool = False,
) -> dict[str, Any]:
    """Which Claude settings entries the herd needs in auto mode, and a draft that adds the missing ones."""
    user_path = claude_config_dir() / "settings.json"
    paths: dict[str, Path] = {"user": user_path}
    if project_dir is not None:
        paths["project"] = project_dir / ".claude" / "settings.json"
        paths["local"] = project_dir / ".claude" / "settings.local.json"
    settings = {scope: _read_settings(path) for scope, path in paths.items()}
    reasons = _auto_mode_reasons(session_mode, loaded, engines, settings, read_only)

    allow = [rule for value in settings.values() for rule in _string_entries(value, "permissions", "allow")]
    restrictions = {
        kind: [rule for value in settings.values() for rule in _string_entries(value, "permissions", kind)]
        for kind in ("deny", "ask")
    }
    auto_allow = [
        rule for scope in AUTO_MODE_SCOPES for rule in _string_entries(settings.get(scope), "autoMode", "allow")
    ]

    missing: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    for command in HERD_COMMANDS:
        for kind, rules in restrictions.items():
            for rule in rules:
                if rule_covers(rule, command):
                    conflicts.append({"setting": f"permissions.{kind}", "entry": rule, "blocks": command})
                elif rule_restricts(rule, command):
                    partial.append({"setting": f"permissions.{kind}", "entry": rule, "may_block": command})
    for item in _required(loaded, engines, read_only):
        shown = {key: item[key] for key in ("setting", "entry", "why")}
        if item["setting"] == "permissions.allow":
            if not any(rule_covers(rule, item["command"]) for rule in allow):
                missing.append(shown)
        elif not any(all(_names(rule, name) for name in item["names"]) for rule in auto_allow):
            missing.append(shown)

    any_auto_allow = any(
        isinstance(value, dict)
        and isinstance(value.get("autoMode"), dict)
        and isinstance(value["autoMode"].get("allow"), list)
        for value in (settings.get(scope) for scope in AUTO_MODE_SCOPES)
    )
    defaults = "not set" if not any_auto_allow else ("kept" if "$defaults" in auto_allow else "replaced")

    in_use = bool(reasons)
    report: dict[str, Any] = {
        "scope": AUTO_MODE_SCOPE,
        "auto_mode": {"in_use": in_use, "because": reasons},
        "files": {scope: {"path": str(path), "present": settings[scope] is not None} for scope, path in paths.items()},
        "missing": missing,
        "conflicts": conflicts,
        "partial_conflicts": partial,
        "defaults": defaults,
        # A deny rule refuses the command in every permission mode; the rest
        # matter only to auto mode's classifier.
        "ready": not any(item["setting"] == "permissions.deny" for item in conflicts)
        and (not in_use or not (missing or conflicts)),
        "draft": None,
    }
    if not (in_use and missing):
        return report

    draft = json.loads(json.dumps(settings["user"] or {}))
    for item in missing:
        section, key = item["setting"].split(".")
        holder = draft.get(section)
        if not isinstance(holder, dict):
            holder = draft[section] = {}
        entries = holder.get(key)
        if not isinstance(entries, list):
            # A new autoMode.allow list replaces the built-in rules unless it names them;
            # keep them only where no list in scope has already left them out.
            entries = holder[key] = ["$defaults"] if section == "autoMode" and defaults == "not set" else []
        entries.append(item["entry"])
    _private_file(draft_path, json.dumps(draft, indent=2) + "\n")
    report["draft"] = {
        "path": str(draft_path),
        "target": str(user_path),
        "copy_command": copy_command(draft_path, user_path, time.strftime("%Y%m%d%H%M%S")),
    }
    return report


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
        "approvals": str(approvals_dir(repo)),
    }


# --- restart approvals ----------------------------------------------------------------

APPROVAL_VERSION = 1
APPROVAL_MAX_TTL_HOURS = 72
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
APPROVAL_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def approvals_dir(repo: str) -> Path:
    """User-owned, outside any checkout, under $XDG_CONFIG_HOME/activeloom."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "activeloom" / "herd-approvals" / flat_repo(repo)


def _approval_target(repo: str, pr: int, head: str) -> Path:
    if pr < 1:
        raise UsageError("--pr must be positive")
    if not SHA_RE.match(head):
        raise UsageError("--head must be a full 40-character lowercase SHA")
    return approvals_dir(repo) / f"pr-{pr}-{head[:12]}.json"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def approve_restart(
    repo: str,
    pr: int,
    head: str,
    ttl_hours: int,
    deep: bool,
    scope_decision: str | None,
    confirm: str,
) -> dict[str, Any]:
    if not 1 <= ttl_hours <= APPROVAL_MAX_TTL_HOURS:
        raise UsageError(f"--ttl-hours must be between 1 and {APPROVAL_MAX_TTL_HOURS}")
    if scope_decision == "split":
        raise UsageError("a split must be done before a restart; approve the new PR's head instead")
    target = _approval_target(repo, pr, head)
    if confirm.strip() != f"restart {pr}":
        raise UsageError("confirmation did not match; nothing written")
    created = _now()
    record = {
        "version": APPROVAL_VERSION,
        "id": secrets.token_hex(8),
        "repo": repo,
        "pr": pr,
        "head": head,
        "deep": deep,
        "scope_decision": scope_decision,
        "created": _iso(created),
        "expires": _iso(created + timedelta(hours=ttl_hours)),
        "used": None,
    }
    _private_dir(target.parent)
    temporary = target.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, target)
    return {"path": str(target), **record}


def _load_approval(path: Path, repo: str, pr: int, head: str) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ApprovalError(f"no restart approval for {repo}#{pr} at {head[:12]}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ApprovalError(f"{path}: not a regular file owned and writable only by this user")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApprovalError(f"{path}: unreadable approval record") from exc
    if not isinstance(record, dict) or record.get("version") != APPROVAL_VERSION:
        raise ApprovalError(f"{path}: unknown approval record version")
    for field, expected in (("repo", repo), ("pr", pr), ("head", head)):
        if record.get(field) != expected:
            raise ApprovalError(f"approval {field} does not match: recorded {record.get(field)!r}")
    if record.get("used") is not None:
        raise ApprovalError(f"approval {record.get('id')} was already used at {record['used']}")
    if (
        not isinstance(record.get("id"), str)
        or not APPROVAL_ID_RE.match(record["id"])
        or record.get("scope_decision") not in (None, "keep")
        or not isinstance(record.get("deep"), bool)
    ):
        raise ApprovalError(f"{path}: malformed approval record")
    created, expires = (_parse_moment(path, record.get(field)) for field in ("created", "expires"))
    now = _now()
    if created > now or expires <= created or expires - created > timedelta(hours=APPROVAL_MAX_TTL_HOURS):
        raise ApprovalError(f"{path}: approval times out of range")
    if expires <= now:
        raise ApprovalError(f"approval {record['id']} expired at {record['expires']}")
    return record


def _parse_moment(path: Path, value: Any) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalError(f"{path}: malformed timestamp") from exc
    if moment.tzinfo is None:
        raise ApprovalError(f"{path}: timestamp has no timezone")
    return moment


def check_restart(repo: str, pr: int, head: str) -> dict[str, Any]:
    target = _approval_target(repo, pr, head)
    return {"path": str(target), **_load_approval(target, repo, pr, head)}


def claim_restart(repo: str, pr: int, head: str) -> dict[str, Any]:
    """Verify, then consume: the rename is atomic, so only one claim can win."""
    target = _approval_target(repo, pr, head)
    record = _load_approval(target, repo, pr, head)
    used_path = target.with_name(f"{target.stem}.{record['id']}.used.json")
    try:
        os.rename(target, used_path)
    except FileNotFoundError as exc:
        raise ApprovalError(f"approval {record['id']} was claimed concurrently") from exc
    record["used"] = _iso(_now())
    descriptor = os.open(used_path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return {"path": str(used_path), **record}


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

    env = commands.add_parser("pane-env", help="environment entries for every pane herd opens")
    add_source(env)

    gate = commands.add_parser("gate-fix-paths", help="classify a gate fix's changed paths")
    add_source(gate)
    gate.add_argument("--base", required=True)
    gate.add_argument("--head", required=True)

    ux = commands.add_parser("ux-paths", help="changed paths matching manual_tests.ux_globs")
    add_source(ux)
    ux_from = ux.add_mutually_exclusive_group(required=True)
    ux_from.add_argument("--head", help="compare with --base (merge base) in the repository")
    ux_from.add_argument("--paths-file", type=Path, help="one changed path per line, e.g. from gh pr view --json files")
    ux.add_argument("--base")

    user = commands.add_parser("user", help="effective per-user config")
    user.add_argument("--path", type=Path)

    user_args = commands.add_parser("user-args", help="extra launch argv for one engine")
    user_args.add_argument("--engine", required=True, choices=ENGINES)
    user_args.add_argument("--read-only", action="store_true", help="omit permission-off arguments")
    user_args.add_argument("--path", type=Path)

    permissions = commands.add_parser("permissions", help="what editing sessions will and won't be asked, per engine")
    permissions.add_argument("--engine", required=True, choices=ENGINES, action="append")
    permissions.add_argument("--path", type=Path)
    permissions.add_argument(
        "--shell",
        default=os.environ.get("SHELL") or "/bin/sh",
        help="shell whose aliases to read",
    )
    permissions.add_argument("--no-alias-check", action="store_true")

    automode = commands.add_parser(
        "claude-settings",
        help="Claude settings the herd needs when auto mode is in use",
    )
    automode.add_argument("--engine", required=True, choices=ENGINES, action="append")
    automode.add_argument("--path", type=Path)
    automode.add_argument(
        "--session-mode",
        help="this session's own Claude permission mode, if it is Claude Code",
    )
    automode.add_argument(
        "--project-dir",
        type=Path,
        help="checkout whose .claude/settings*.json also apply",
    )
    automode.add_argument("--draft", type=Path, help="where to write the draft settings file")
    automode.add_argument(
        "--read-only",
        action="store_true",
        help="every session this run starts is read-only, so permission_off_args are never passed",
    )


    paths = commands.add_parser("paths", help="create and print per-repository state paths")
    lock = commands.add_parser("lock", help="take a per-repository lock (exit 3 if held)")
    unlock = commands.add_parser("unlock", help="release a per-repository lock")
    for command in (paths, lock, unlock):
        command.add_argument("--repo", required=True)
        command.add_argument("--skill", required=True)
        command.add_argument("--key")

    approve = commands.add_parser(
        "approve-restart",
        help="for you to run in your own terminal: approve one restart of an ended review run",
    )
    check = commands.add_parser("check-restart", help="read-only: is there a valid restart approval")
    claim = commands.add_parser("claim-restart", help="verify and consume a restart approval (single use)")
    for command in (approve, check, claim):
        command.add_argument("--repo", required=True)
        command.add_argument("--pr", required=True, type=int)
        command.add_argument("--head", required=True)
    approve.add_argument("--ttl-hours", type=int, default=4)
    approve.add_argument("--deep", action="store_true", help="the restart is a Deep review (trigger 6)")
    approve.add_argument("--scope-decision", choices=("keep", "split"))

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
        elif args.command == "pane-env":
            _emit(pane_env(read_repo(args.repo_dir, args.remote, args.default_branch, not args.no_fetch)["config"]))
        elif args.command == "gate-fix-paths":
            loaded = read_repo(args.repo_dir, args.remote, args.default_branch, not args.no_fetch)
            result = classify_paths(loaded["config"]["gate_fix"], changed_paths(args.repo_dir, args.base, args.head))
            _emit({"source": loaded["source"], **result})
        elif args.command == "ux-paths":
            loaded = read_repo(args.repo_dir, args.remote, args.default_branch, not args.no_fetch)
            if args.paths_file is not None:
                try:
                    lines = args.paths_file.read_text(encoding="utf-8").splitlines()
                except OSError as exc:
                    raise UsageError(f"cannot read {args.paths_file}: {exc}") from exc
                paths = [line.strip() for line in lines if line.strip()]
            elif args.base is None:
                raise UsageError("--head needs --base")
            else:
                paths = changed_paths(args.repo_dir, args.base, args.head)
            result = ux_paths(loaded["config"]["manual_tests"], paths)
            _emit({"source": loaded["source"], **result})
        elif args.command == "user":
            _emit(read_user(args.path or user_config_path()))
        elif args.command == "user-args":
            entry = read_user(args.path or user_config_path())["config"]["engines"][args.engine]
            _emit(entry["launch_args"] + ([] if args.read_only else entry["permission_off_args"]))
        elif args.command in ("approve-restart", "check-restart", "claim-restart"):
            if args.command == "check-restart":
                _emit(check_restart(args.repo, args.pr, args.head))
            elif args.command == "claim-restart":
                _emit(claim_restart(args.repo, args.pr, args.head))
            else:
                if not sys.stdin.isatty():
                    raise UsageError("approve-restart reads your confirmation from an interactive terminal; nothing written")
                print(
                    f"Approve one restart of the review run on {args.repo}#{args.pr} at {args.head[:12]}"
                    f" (expires in {args.ttl_hours}h). Type 'restart {args.pr}' to confirm: ",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
                confirm = sys.stdin.readline()
                _emit(
                    approve_restart(
                        args.repo, args.pr, args.head, args.ttl_hours, args.deep, args.scope_decision, confirm
                    )
                )
        elif args.command == "permissions":
            engines = list(dict.fromkeys(args.engine))
            texts = (
                {}
                if args.no_alias_check
                else {engine: probe_type(ENGINE_CLIS[engine], args.shell) for engine in engines}
            )
            _emit(permission_report(read_user(args.path or user_config_path()), engines, texts))
        elif args.command == "claude-settings":
            tmp = Path(os.environ.get("TMPDIR") or "/tmp")
            draft = args.draft or tmp / "activeloom-herd" / "claude-settings.draft.json"
            loaded = read_user(args.path or user_config_path())
            engines = list(dict.fromkeys(args.engine))
            _emit(auto_mode_report(loaded, engines, args.session_mode, args.project_dir, draft, args.read_only))
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
    except ApprovalError as exc:
        print(f"no valid restart approval: {exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
