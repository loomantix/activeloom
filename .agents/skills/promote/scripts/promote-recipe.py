#!/usr/bin/env python3
"""Read, validate, lay out and run a repository's promotion recipe.

A promotion recipe, `.activeloom-promote.json`, is owned by the consumer
repository and read from its integration branch at the remote, never from a
pull request head. It names the integration and production branches and the
steps of a promotion, in three phases separated by fixed milestones:

    preflight  ->  open the promotion PR  ->  pre_merge  ->  human approval
    and merge  ->  post_merge

Inside a phase, steps are grouped into lanes. A lane runs its steps in order;
different lanes of one phase may run side by side. `layout` maps the lanes of
each phase onto at most five worker panes below the driver's pane, and `run`
executes one lane's mechanical steps (commands and watchers) with visible
output.

Commands print JSON on stdout. Exit codes: 0 ok, 1 invalid recipe, 2 usage or
git error, 4 no recipe file on the integration branch (`show` without `--herd`).
`run` exits 0 when every step in the lane passed or only warned, 3 otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
RECIPE_FILE = ".activeloom-promote.json"
MAX_PANES = 6
WORKER_SLOTS = MAX_PANES - 1
PHASES = ("preflight", "pre_merge", "post_merge")
# Phases that can raise a blocker keep one slot free for a fix session.
FIX_PHASES = ("preflight", "pre_merge")
KINDS = ("command", "task", "risk_review", "watch_checks", "watch_workflow")
SHELL_KINDS = ("command", "watch_checks", "watch_workflow")
LONG_KINDS = ("risk_review", "watch_checks", "watch_workflow")
KIND_PHASES = {
    "command": PHASES,
    "task": PHASES,
    "risk_review": ("pre_merge",),
    "watch_checks": ("pre_merge",),
    "watch_workflow": PHASES,
}
ON_FAIL = ("block", "hold", "warn")
IF_ABSENT = ("fail", "skip")
MERGE_METHODS = ("merge", "squash", "rebase")
CHECKS = ("required", "all")
ENGINES = ("claude", "codex", "gemini")
DEFAULT_TITLE = "Promote {integration} → {production} — {date}"
TITLE_FIELDS = ("integration", "production", "date")
APPEAR_GRACE_SECONDS = 300
POLL_SECONDS = 30

TOP_KEYS = (
    "schema_version",
    "integration_branch",
    "production_branch",
    "guide",
    "rollback",
    "blocker_issues",
    "pull_request",
    "steps",
)
PR_KEYS = ("title", "merge_method", "labels", "close_stale", "surface", "after_merge")
STEP_KEYS = {
    "command": ("run", "fix", "on_fail", "timeout_minutes"),
    "task": ("brief", "writes"),
    "risk_review": ("engine", "focus"),
    "watch_checks": ("checks", "rerun_flakes", "timeout_minutes"),
    "watch_workflow": ("workflow", "if_absent", "timeout_minutes"),
}
COMMON_STEP_KEYS = ("id", "kind", "phase", "lane")

BRANCH_RE = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)(?!.*\.lock$)[A-Za-z0-9._/-]{1,200}(?<!/)$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class RecipeError(Exception):
    """The recipe is invalid; `errors` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class UsageError(Exception):
    """A git, filesystem or argument problem unrelated to recipe validity."""


class NoRecipe(Exception):
    """The integration branch carries no recipe file."""


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


def _relative_path(value: Any, where: str, errors: list[str]) -> str | None:
    if not _is_text(value):
        errors.append(f"{where}: must be a non-empty single-line string")
        return None
    if value.startswith("/") or "\\" in value or ".." in value.split("/"):
        errors.append(f"{where}: {value!r} must be a relative POSIX path without '..'")
    return value


def _optional_path(config: dict[str, Any], key: str, where: str, errors: list[str]) -> str | None:
    if config.get(key) is None:
        return None
    return _relative_path(config[key], f"{where}{key}", errors)


def _globs(value: Any, where: str, errors: list[str]) -> list[str]:
    globs = _string_list(value, where, errors)
    if isinstance(value, list) and not value:
        errors.append(f"{where}: must name at least one glob")
    for glob in globs:
        if glob.startswith("/") or "\\" in glob or ".." in glob.split("/"):
            errors.append(f"{where}: {glob!r} must be a relative POSIX glob without '..'")
    return globs


def _choice(value: Any, choices: tuple[str, ...], where: str, errors: list[str]) -> Any:
    if value not in choices:
        errors.append(f"{where}: must be one of {', '.join(choices)}")
    return value


def _int(value: Any, low: int, high: int, where: str, errors: list[str]) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        errors.append(f"{where}: must be an integer from {low} to {high}")
        return low
    return value


def _bool(value: Any, where: str, errors: list[str]) -> bool:
    if not isinstance(value, bool):
        errors.append(f"{where}: must be true or false")
        return False
    return value


def _object(value: Any, where: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{where}: must be an object")
        return {}
    return value


# --- recipe validation --------------------------------------------------------


def _validate_pull_request(value: Any, errors: list[str]) -> dict[str, Any]:
    pr = _object(value, "pull_request", errors)
    _check_keys(pr, PR_KEYS, "pull_request", errors)
    title = pr.get("title", DEFAULT_TITLE)
    if not _is_text(title):
        errors.append("pull_request.title: must be a non-empty single-line string")
        title = DEFAULT_TITLE
    else:
        for field in re.findall(r"\{([^{}]*)\}", title):
            if field not in TITLE_FIELDS:
                errors.append(f"pull_request.title: unknown field {{{field}}} (expected {', '.join(TITLE_FIELDS)})")
        try:
            title.format(integration="", production="", date="")
        except (ValueError, KeyError, IndexError) as exc:
            errors.append(f"pull_request.title: invalid format string ({exc})")
    method = pr.get("merge_method")
    if method is not None:
        _choice(method, MERGE_METHODS, "pull_request.merge_method", errors)
    surface: list[dict[str, Any]] = []
    surface_value = pr.get("surface", [])
    if not isinstance(surface_value, list):
        errors.append("pull_request.surface: must be an array")
        surface_value = []
    names: set[str] = set()
    for index, entry in enumerate(surface_value):
        where = f"pull_request.surface[{index}]"
        entry = _object(entry, where, errors)
        _check_keys(entry, ("name", "globs"), where, errors)
        name = entry.get("name")
        if not isinstance(name, str) or not SLUG_RE.match(name):
            errors.append(f"{where}.name: required; must be a lower-case slug")
        elif name in names:
            errors.append(f"{where}.name: duplicate {name!r}")
        else:
            names.add(name)
        globs = _globs(entry["globs"], f"{where}.globs", errors) if "globs" in entry else []
        if "globs" not in entry:
            errors.append(f"{where}.globs: required")
        surface.append({"name": name, "globs": globs})
    return {
        "title": title,
        "merge_method": method,
        "labels": _string_list(pr.get("labels", []), "pull_request.labels", errors, unique=True),
        "close_stale": _bool(pr.get("close_stale", False), "pull_request.close_stale", errors),
        "surface": surface,
        "after_merge": _string_list(pr.get("after_merge", []), "pull_request.after_merge", errors),
    }


def default_lane(kind: str, step_id: str) -> str:
    """Long-running steps get a lane of their own; the rest share `main`."""
    return step_id if kind in LONG_KINDS or kind == "task" else "main"


def _validate_step(step: Any, where: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(step, dict):
        errors.append(f"{where}: must be an object")
        return None
    kind = step.get("kind")
    if kind not in KINDS:
        errors.append(f"{where}.kind: must be one of {', '.join(KINDS)}")
        return None
    _check_keys(step, COMMON_STEP_KEYS + STEP_KEYS[kind], where, errors)
    step_id = step.get("id")
    if not isinstance(step_id, str) or not SLUG_RE.match(step_id):
        errors.append(f"{where}.id: required; must be a lower-case slug")
        step_id = f"step-{where}"
    phase = step.get("phase")
    if phase not in PHASES:
        errors.append(f"{where}.phase: must be one of {', '.join(PHASES)}")
    elif phase not in KIND_PHASES[kind]:
        errors.append(f"{where}.phase: a {kind} step runs only in {', '.join(KIND_PHASES[kind])}")
    lane = step.get("lane", default_lane(kind, step_id))
    if not isinstance(lane, str) or not SLUG_RE.match(lane):
        errors.append(f"{where}.lane: must be a lower-case slug")
    out: dict[str, Any] = {"id": step_id, "kind": kind, "phase": phase, "lane": lane}
    if kind == "command":
        if "run" not in step:
            errors.append(f"{where}.run: required")
        out["run"] = _argv(step.get("run", ["missing"]), f"{where}.run", errors)
        out["fix"] = _argv(step["fix"], f"{where}.fix", errors) if step.get("fix") is not None else None
        if out["fix"] is not None and phase == "post_merge":
            errors.append(f"{where}.fix: post_merge steps cannot carry a fix; production is already deployed")
        out["on_fail"] = _choice(step.get("on_fail", "block"), ON_FAIL, f"{where}.on_fail", errors)
        out["timeout_minutes"] = _int(step.get("timeout_minutes", 30), 1, 240, f"{where}.timeout_minutes", errors)
    elif kind == "task":
        if "brief" not in step:
            errors.append(f"{where}.brief: required")
        out["brief"] = _relative_path(step.get("brief"), f"{where}.brief", errors) if "brief" in step else None
        out["writes"] = _bool(step.get("writes", False), f"{where}.writes", errors)
    elif kind == "risk_review":
        engine = step.get("engine")
        if engine is not None and engine not in ENGINES:
            errors.append(f"{where}.engine: unknown engine {engine!r}")
        out["engine"] = engine
        out["focus"] = _string_list(step.get("focus", []), f"{where}.focus", errors)
    elif kind == "watch_checks":
        out["checks"] = _choice(step.get("checks", "required"), CHECKS, f"{where}.checks", errors)
        out["rerun_flakes"] = _int(step.get("rerun_flakes", 1), 0, 2, f"{where}.rerun_flakes", errors)
        out["timeout_minutes"] = _int(step.get("timeout_minutes", 60), 1, 360, f"{where}.timeout_minutes", errors)
    else:
        if not _is_text(step.get("workflow")):
            errors.append(f"{where}.workflow: required; a workflow file name or name")
        out["workflow"] = step.get("workflow")
        out["if_absent"] = _choice(step.get("if_absent", "fail"), IF_ABSENT, f"{where}.if_absent", errors)
        out["timeout_minutes"] = _int(step.get("timeout_minutes", 60), 1, 360, f"{where}.timeout_minutes", errors)
    return out


def validate_recipe(config: Any) -> dict[str, Any]:
    """Validate a parsed recipe and return it with defaults applied."""
    errors: list[str] = []
    if not isinstance(config, dict):
        raise RecipeError(["top level: must be a JSON object"])
    _check_keys(config, TOP_KEYS, "top level", errors)
    if config.get("schema_version") != SCHEMA_VERSION or isinstance(config.get("schema_version"), bool):
        errors.append(f"schema_version: must be {SCHEMA_VERSION}")
    branches = {}
    for key in ("integration_branch", "production_branch"):
        value = config.get(key)
        if not isinstance(value, str) or not BRANCH_RE.match(value):
            errors.append(f"{key}: required; must be a branch name")
        branches[key] = value
    if branches["integration_branch"] == branches["production_branch"] and branches["integration_branch"]:
        errors.append("production_branch: must differ from integration_branch")

    issues = config.get("blocker_issues")
    blocker_issues = None
    if issues is not None:
        issues = _object(issues, "blocker_issues", errors)
        _check_keys(issues, ("labels",), "blocker_issues", errors)
        blocker_issues = {"labels": _string_list(issues.get("labels", []), "blocker_issues.labels", errors, unique=True)}

    steps: list[dict[str, Any]] = []
    steps_value = config.get("steps")
    if not isinstance(steps_value, list) or not steps_value:
        errors.append("steps: required; must be a non-empty array")
        steps_value = []
    seen: set[str] = set()
    for index, raw in enumerate(steps_value):
        step = _validate_step(raw, f"steps[{index}]", errors)
        if step is None:
            continue
        if step["id"] in seen:
            errors.append(f"steps[{index}].id: duplicate {step['id']!r}")
        seen.add(step["id"])
        steps.append(step)
    for kind in ("risk_review", "watch_checks"):
        if sum(step["kind"] == kind for step in steps) > 1:
            errors.append(f"steps: at most one {kind} step")
    # A lane belongs to one phase and one executor: a shell runs commands and
    # watchers, a session runs tasks and the risk review.
    lanes: dict[str, tuple[str, bool]] = {}
    for step in steps:
        shell = step["kind"] in SHELL_KINDS
        known = lanes.setdefault(step["lane"], (step["phase"], shell))
        if known[0] != step["phase"]:
            errors.append(f"steps: lane {step['lane']!r} spans phases {known[0]} and {step['phase']}; use one lane per phase")
        elif known[1] != shell:
            errors.append(
                f"steps: lane {step['lane']!r} mixes shell steps with session steps (task, risk_review); split it"
            )
    session_lanes = [lane for lane, (_, shell) in lanes.items() if not shell]
    for lane in session_lanes:
        if sum(step["lane"] == lane for step in steps) > 1:
            errors.append(f"steps: session lane {lane!r} holds more than one step; give each task its own lane")

    effective = {
        "schema_version": SCHEMA_VERSION,
        "integration_branch": branches["integration_branch"],
        "production_branch": branches["production_branch"],
        "guide": _optional_path(config, "guide", "", errors),
        "rollback": _optional_path(config, "rollback", "", errors),
        "blocker_issues": blocker_issues,
        "pull_request": _validate_pull_request(config.get("pull_request", {}), errors),
        "steps": steps,
    }
    if errors:
        raise RecipeError(errors)
    return effective


def parse_json(text: str, where: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecipeError([f"{where}: not valid JSON ({exc.msg} at line {exc.lineno})"]) from exc


# --- a recipe from the herd config's legacy promotion keys ------------------------


def _slug(text: str, fallback: str) -> str:
    base = re.sub(r"\.ya?ml$", "", text.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:28].strip("-")
    return slug or fallback


def from_herd(herd: dict[str, Any]) -> dict[str, Any]:
    """Synthesize a recipe from `herd-config.py repo` output.

    It reproduces what hpromote did before recipes: run the preflight commands,
    require each deploy workflow green on the integration head, review risk,
    watch the promotion PR's checks, then watch the deploy workflows on the
    merge commit and run the post-merge commands.
    """
    config = herd.get("config", herd)
    promotion = config.get("promotion") or {}
    if not promotion.get("enabled"):
        raise UsageError("the herd config has no promotion section")
    steps: list[dict[str, Any]] = []
    for index, argv in enumerate(promotion.get("preflight", []), 1):
        steps.append({"id": f"preflight-{index}", "kind": "command", "phase": "preflight", "lane": "preflight", "run": argv})
    workflows = promotion.get("deploy_workflows", [])
    used: set[str] = set()
    slugs = []
    for index, workflow in enumerate(workflows, 1):
        slug = _slug(workflow, f"workflow-{index}")
        if slug in used:
            slug = f"{slug[:24]}-{index}"
        used.add(slug)
        slugs.append(slug)
    for slug, workflow in zip(slugs, workflows):
        steps.append({"id": f"pre-{slug}", "kind": "watch_workflow", "phase": "preflight", "workflow": workflow})
    steps.append({"id": "risk", "kind": "risk_review", "phase": "pre_merge", "engine": promotion.get("risk_engine")})
    steps.append({"id": "ci", "kind": "watch_checks", "phase": "pre_merge"})
    for slug, workflow in zip(slugs, workflows):
        steps.append({"id": f"post-{slug}", "kind": "watch_workflow", "phase": "post_merge", "lane": "verify", "workflow": workflow})
    for index, argv in enumerate(promotion.get("post_merge", []), 1):
        steps.append({"id": f"post-merge-{index}", "kind": "command", "phase": "post_merge", "lane": "verify", "run": argv})
    return validate_recipe(
        {
            "schema_version": SCHEMA_VERSION,
            "integration_branch": config.get("integration_branch"),
            "production_branch": promotion.get("production_branch"),
            "steps": steps,
        }
    )


# --- reading from the integration branch at the remote -------------------------


def _git(repo_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True, check=False)
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


def _read_at(repo_dir: Path, remote: str, branch: str, fetch: bool) -> tuple[str, str | None]:
    if fetch:
        _git(repo_dir, "fetch", "--quiet", "--no-tags", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    ref = f"refs/remotes/{remote}/{branch}"
    commit = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if commit.returncode != 0:
        raise UsageError(f"{remote}/{branch} does not exist locally; fetch it or drop --no-fetch")
    shown = _git(repo_dir, "show", f"{ref}:{RECIPE_FILE}", check=False)
    return commit.stdout.strip(), (shown.stdout if shown.returncode == 0 else None)


def read_recipe(repo_dir: Path, remote: str, default: str | None, fetch: bool) -> dict[str, Any]:
    """Return the effective recipe and the commit it came from.

    The file is read from the default branch first. When it names a different
    integration branch, the copy on that branch is authoritative and must name
    the same integration branch, so neither branch can redirect the other.
    """
    if remote.startswith("-") or not KEY_RE.match(remote):
        raise UsageError(f"invalid remote name {remote!r}")
    branch = default or default_branch(repo_dir, remote)
    if not BRANCH_RE.match(branch):
        raise UsageError(f"invalid branch name {branch!r}")
    commit, text = _read_at(repo_dir, remote, branch, fetch)
    if text is None:
        raise NoRecipe(f"{remote}/{branch}:{RECIPE_FILE} is absent")
    where = f"{remote}/{branch}:{RECIPE_FILE}"
    recipe = validate_recipe(parse_json(text, where))
    integration = recipe["integration_branch"]
    if integration != branch:
        commit, text = _read_at(repo_dir, remote, integration, fetch)
        where = f"{remote}/{integration}:{RECIPE_FILE}"
        if text is None:
            raise RecipeError([f"{where}: missing; the integration branch must carry the recipe it is named in"])
        recipe = validate_recipe(parse_json(text, where))
        if recipe["integration_branch"] != integration:
            raise RecipeError([f"{where}: names integration branch {recipe['integration_branch']!r}, not {integration!r}"])
    return {"source": {"kind": "file", "ref": f"{remote}/{integration}", "commit": commit}, "recipe": recipe}


def resolve(args: argparse.Namespace) -> dict[str, Any]:
    """The recipe file, else one synthesized from the herd config given with --herd."""
    herd = _load_json_arg(args.herd) if args.herd else None
    config = herd.get("config", herd) if herd is not None else {}
    try:
        loaded: dict[str, Any] | None = read_recipe(args.repo_dir, args.remote, args.default_branch, not args.no_fetch)
    except NoRecipe:
        if herd is None:
            raise
        herd_branch = config.get("integration_branch")
        if args.default_branch is None and isinstance(herd_branch, str):
            try:
                loaded = read_recipe(args.repo_dir, args.remote, herd_branch, not args.no_fetch)
            except NoRecipe:
                loaded = None
        else:
            loaded = None
    if loaded is None:
        assert herd is not None
        source = herd.get("source", {})
        return {"source": {"kind": "herd", "ref": source.get("ref"), "commit": source.get("commit")}, "recipe": from_herd(herd)}
    if herd is not None:
        promotion = config.get("promotion") or {}
        recipe = loaded["recipe"]
        mismatches = [
            f"{key}: recipe {ours!r}, herd config {theirs!r}"
            for key, ours, theirs in (
                ("integration_branch", recipe["integration_branch"], config.get("integration_branch")),
                ("production_branch", recipe["production_branch"], promotion.get("production_branch")),
            )
            if theirs is not None and ours != theirs
        ]
        if mismatches:
            raise RecipeError([f"recipe and herd config disagree: {m}" for m in mismatches])
    return loaded


def _load_json_arg(value: str) -> dict[str, Any]:
    try:
        text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read {value}: {exc}") from exc
    parsed = parse_json(text, value)
    if not isinstance(parsed, dict):
        raise UsageError(f"{value}: must be a JSON object")
    return parsed


def load_recipe_file(value: str) -> dict[str, Any]:
    """A raw recipe, or `show` output carrying one under `recipe`."""
    parsed = _load_json_arg(value)
    return validate_recipe(parsed["recipe"] if "recipe" in parsed and "steps" not in parsed else parsed)


# --- pane layout -----------------------------------------------------------------


def _lanes(recipe: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    lanes: dict[str, dict[str, Any]] = {}
    for step in recipe["steps"]:
        if step["phase"] != phase:
            continue
        lane = lanes.setdefault(
            step["lane"],
            {"lane": step["lane"], "executor": "shell" if step["kind"] in SHELL_KINDS else "session", "steps": []},
        )
        lane["steps"].append(step["id"])
        lane["long"] = lane.get("long", False) or step["kind"] in LONG_KINDS
    return list(lanes.values())


def _pack(lanes: list[dict[str, Any]], cap: int) -> list[list[dict[str, Any]]]:
    """Group lanes into at most `cap` panes.

    Each lane starts in its own pane. While there are too many, the last pane of
    short lanes folds into the short pane before it; long-running lanes keep a
    pane of their own until only they are left to fold. Folded lanes run one
    after another in their pane, in recipe order.
    """
    panes = [[lane] for lane in lanes]
    while len(panes) > cap:
        short = [index for index, pane in enumerate(panes) if not any(lane["long"] for lane in pane)]
        pool = short if len(short) >= 2 else [index for index in range(len(panes)) if index not in short]
        if len(pool) < 2:
            pool = list(range(len(panes)))
        keep, fold = pool[-2], pool[-1]
        panes[keep] = panes[keep] + panes[fold]
        del panes[fold]
    return panes


def layout(recipe: dict[str, Any]) -> dict[str, Any]:
    """Deterministic mapping of the recipe's lanes onto at most MAX_PANES panes."""
    phases = []
    slots = 0
    for phase in PHASES:
        lanes = _lanes(recipe, phase)
        if not lanes:
            continue
        reserve = 1 if phase in FIX_PHASES else 0
        packed = _pack(lanes, WORKER_SLOTS - reserve)
        panes = []
        for slot, group in enumerate(packed, 1):
            title = "+".join(lane["lane"] for lane in group)
            panes.append(
                {
                    "slot": slot,
                    "title": title if len(title) <= 40 else title[:39] + "…",
                    "lanes": [{key: lane[key] for key in ("lane", "executor", "steps")} for lane in group],
                }
            )
        slots = max(slots, len(panes) + reserve)
        phases.append({"phase": phase, "panes": panes, "fix_slots": WORKER_SLOTS - len(panes) if reserve else 0})
    return {"max_panes": MAX_PANES, "worker_slots": slots, "phases": phases}


# --- running a lane ----------------------------------------------------------------


Runner = Callable[[list[str], Path, int | None], tuple[int, str]]


def _capture(argv: list[str], cwd: Path, timeout: int | None) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as exc:
        return 127, str(exc)
    return result.returncode, result.stdout or result.stderr


def _say(text: str) -> None:
    print(text, flush=True)


def run_command(step: dict[str, Any], cwd: Path) -> dict[str, Any]:
    _say(f"── {step['id']}: {' '.join(step['run'])}")
    started = time.monotonic()
    try:
        with subprocess.Popen(step["run"], cwd=cwd, start_new_session=os.name == "posix") as process:
            try:
                code = process.wait(timeout=step["timeout_minutes"] * 60)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
                code = 124
    except OSError as exc:
        _say(f"cannot start: {exc}")
        code = 127
    outcome = "ok" if code == 0 else {"block": "failed", "hold": "hold", "warn": "warn"}[step["on_fail"]]
    if code == 124:
        outcome = "timeout" if step["on_fail"] != "warn" else "warn"
    result = {"step": step["id"], "outcome": outcome, "exit": code, "seconds": round(time.monotonic() - started)}
    if code != 0 and step.get("fix"):
        result["fix"] = step["fix"]
    return result


def _workflow_runs(step: dict[str, Any], sha: str, cwd: Path, runner: Runner) -> list[dict[str, Any]] | None:
    argv = ["gh", "run", "list", "--workflow", step["workflow"], "--commit", sha, "--limit", "20",
            "--json", "databaseId,status,conclusion,createdAt,event,url"]
    code, out = runner(argv, cwd, 60)
    if code != 0:
        return None
    try:
        runs = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    return sorted(runs, key=lambda run: run.get("createdAt") or "")


def watch_workflow(step: dict[str, Any], sha: str, cwd: Path, runner: Runner = _capture,
                   sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Wait for the newest run of a workflow on `sha` to complete."""
    _say(f"── {step['id']}: watching {step['workflow']} on {sha[:12]}")
    started = clock()
    deadline = started + step["timeout_minutes"] * 60
    while True:
        runs = _workflow_runs(step, sha, cwd, runner)
        elapsed = clock() - started
        if runs is None:
            return {"step": step["id"], "outcome": "failed", "detail": "gh run list failed"}
        if runs:
            newest = runs[-1]
            if newest.get("status") == "completed":
                ok = newest.get("conclusion") in ("success", "skipped")
                _say(f"   {newest.get('conclusion')}: {newest.get('url')}")
                return {"step": step["id"], "outcome": "ok" if ok else "failed",
                        "conclusion": newest.get("conclusion"), "run": newest.get("databaseId"),
                        "url": newest.get("url"), "seconds": round(elapsed)}
            _say(f"   {newest.get('status')} ({round(elapsed)}s)")
        elif elapsed >= APPEAR_GRACE_SECONDS or clock() >= deadline:
            outcome = "skipped" if step["if_absent"] == "skip" else "absent"
            _say(f"   no run on {sha[:12]}: {outcome}")
            return {"step": step["id"], "outcome": outcome, "seconds": round(elapsed)}
        if clock() >= deadline:
            return {"step": step["id"], "outcome": "timeout", "seconds": round(elapsed)}
        sleep(POLL_SECONDS)


def watch_checks(step: dict[str, Any], pr: int, sha: str, cwd: Path, runner: Runner = _capture,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Wait for the promotion PR's checks to settle at `sha`."""
    _say(f"── {step['id']}: watching {step['checks']} checks on PR #{pr}")
    started = clock()
    deadline = started + step["timeout_minutes"] * 60
    argv = ["gh", "pr", "view", str(pr), "--json", "headRefOid"]
    while True:
        code, out = runner(argv, cwd, 60)
        try:
            head = json.loads(out).get("headRefOid") if code == 0 else None
        except json.JSONDecodeError:
            head = None
        if head != sha:
            return {"step": step["id"], "outcome": "failed", "detail": f"PR head is {head}, expected {sha}"}
        checks_argv = ["gh", "pr", "checks", str(pr), "--json", "name,bucket,link"]
        if step["checks"] == "required":
            checks_argv.append("--required")
        code, out = runner(checks_argv, cwd, 60)
        if code == 1 and re.match(r"^no (?:required )?checks reported on the ", out.strip()):
            checks: list[dict[str, Any]] | None = []
        else:
            try:
                parsed = json.loads(out)
                checks = parsed if isinstance(parsed, list) else None
            except json.JSONDecodeError:
                checks = None
        elapsed = clock() - started
        # `gh pr checks` exits 8 while checks are pending and 1 when one failed.
        if checks is None or code not in (0, 1, 8):
            return {"step": step["id"], "outcome": "failed", "detail": "gh pr checks failed"}
        pending = [c["name"] for c in checks if c.get("bucket") == "pending"]
        failing = [c["name"] for c in checks if c.get("bucket") not in ("pass", "skipping", "pending")]
        if not pending:
            if not checks:
                if elapsed < APPEAR_GRACE_SECONDS and clock() < deadline:
                    sleep(POLL_SECONDS)
                    continue
                return {"step": step["id"], "outcome": "absent", "seconds": round(elapsed)}
            _say(f"   {len(checks) - len(failing)} passed, {len(failing)} failed")
            return {"step": step["id"], "outcome": "failed" if failing else "ok", "failing": failing,
                    "rerun_flakes": step["rerun_flakes"], "seconds": round(elapsed)}
        _say(f"   {len(pending)} pending, {len(failing)} failed ({round(elapsed)}s)")
        if clock() >= deadline:
            return {"step": step["id"], "outcome": "timeout", "pending": pending, "failing": failing}
        sleep(POLL_SECONDS)


PASSING = ("ok", "warn", "skipped")


def run_lane(recipe: dict[str, Any], phase: str, lane: str, cwd: Path, sha: str, pr: int | None) -> dict[str, Any]:
    steps = [step for step in recipe["steps"] if step["phase"] == phase and step["lane"] == lane]
    if not steps:
        raise UsageError(f"no lane {lane!r} in phase {phase}")
    if any(step["kind"] not in SHELL_KINDS for step in steps):
        raise UsageError(f"lane {lane!r} runs in a session, not the shell runner")
    if any(step["kind"] == "watch_checks" for step in steps) and pr is None:
        raise UsageError("a watch_checks lane needs --pr")
    results: list[dict[str, Any]] = []
    for step in steps:
        if step["kind"] == "command":
            result = run_command(step, cwd)
        elif step["kind"] == "watch_workflow":
            result = watch_workflow(step, sha, cwd)
        else:
            result = watch_checks(step, pr or 0, sha, cwd)
        results.append(result)
        if result["outcome"] not in PASSING:
            # Steps in one lane are ordered: a later one may depend on this one.
            results.extend({"step": rest["id"], "outcome": "not-run"} for rest in steps[len(results):])
            break
    outcome = next((r["outcome"] for r in results if r["outcome"] not in PASSING), "ok")
    return {"phase": phase, "lane": lane, "sha": sha, "outcome": outcome, "steps": results}


# --- command line -------------------------------------------------------------------


def render_title(recipe: dict[str, Any], date: str) -> str:
    return recipe["pull_request"]["title"].format(
        integration=recipe["integration_branch"], production=recipe["production_branch"], date=date
    )


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate a working-tree recipe")
    validate.add_argument("file", nargs="?", default=RECIPE_FILE)

    show = commands.add_parser("show", help="effective recipe from the integration branch at the remote")
    show.add_argument("--repo-dir", default=".", type=Path)
    show.add_argument("--remote", default="origin")
    show.add_argument("--default-branch")
    show.add_argument("--no-fetch", action="store_true")
    show.add_argument("--herd", help="herd-config.py repo output (file or -): fallback and branch agreement check")

    lay = commands.add_parser("layout", help="map a recipe's lanes onto at most 6 panes")
    lay.add_argument("--recipe", required=True, help="recipe or show output (file or -)")

    title = commands.add_parser("title", help="the promotion PR title")
    title.add_argument("--recipe", required=True)
    title.add_argument("--date", default=dt.date.today().isoformat())

    run = commands.add_parser("run", help="run one shell lane with visible output")
    run.add_argument("--recipe", required=True)
    run.add_argument("--phase", required=True, choices=PHASES)
    run.add_argument("--lane", required=True)
    run.add_argument("--cwd", required=True, type=Path)
    run.add_argument("--sha", required=True)
    run.add_argument("--pr", type=int)
    run.add_argument("--out", type=Path, help="also write the result JSON here")

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            path = Path(args.file)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise UsageError(f"cannot read {path}: {exc}") from exc
            _emit(validate_recipe(parse_json(text, str(path))))
        elif args.command == "show":
            _emit(resolve(args))
        elif args.command == "layout":
            _emit(layout(load_recipe_file(args.recipe)))
        elif args.command == "title":
            print(render_title(load_recipe_file(args.recipe), args.date))
        else:
            if not SHA_RE.match(args.sha):
                raise UsageError("--sha must be a full 40-character SHA")
            if not args.cwd.is_dir():
                raise UsageError(f"--cwd {args.cwd} is not a directory")
            result = run_lane(load_recipe_file(args.recipe), args.phase, args.lane, args.cwd, args.sha, args.pr)
            if args.out:
                args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _emit(result)
            # The line the driver waits for in the pane.
            _say(f"PROMOTE-LANE {args.phase}/{args.lane} {result['outcome']}")
            return 0 if result["outcome"] == "ok" else 3
    except NoRecipe as exc:
        print(f"no recipe: {exc}", file=sys.stderr)
        return 4
    except RecipeError as exc:
        for error in exc.errors:
            print(f"invalid: {error}", file=sys.stderr)
        return 1
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
