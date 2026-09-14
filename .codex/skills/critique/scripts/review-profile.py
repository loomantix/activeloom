#!/usr/bin/env python3
"""Per-user review profile: the model, effort and engine order local reviews use.

Stdlib only, and the only writer of the profile file. Nothing is defaulted
silently: launchers and the review-chain runner resolve settings through this
script, and a missing profile is an error that points at setup. `init` writes
the recommended defaults only when explicitly asked to.

Exit status: 0 success, 1 refused operation, 2 invalid input or profile,
3 no profile configured.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

SCHEMA_VERSION = 1
ENGINES = ("claude", "codex", "gemini")
TIERS = ("lean", "deep")
CLIS = {"claude": "claude", "codex": "codex", "gemini": "agy"}
# Values each engine CLI accepts for its effort flag.
EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("minimal", "low", "medium", "high", "xhigh", "max"),
    "gemini": ("low", "medium", "high"),
}
# `inherit` omits the model flag so the engine CLI's own configured default
# applies. Gemini's launcher needs an explicit model for its skill preflight.
INHERIT = "inherit"
INHERIT_ENGINES = ("claude", "codex")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
DEFAULTS_PATH = Path(__file__).resolve().with_name("review-profile.defaults.json")
PIN_MODEL = "ACTIVELOOM_REVIEW_MODEL"
PIN_EFFORT = "ACTIVELOOM_REVIEW_EFFORT"

EXIT_REFUSED = 1
EXIT_INVALID = 2
EXIT_UNCONFIGURED = 3


class ProfileError(Exception):
    def __init__(self, message: str, status: int = EXIT_INVALID):
        super().__init__(message)
        self.status = status


def profile_path() -> Path:
    explicit = os.environ.get("ACTIVELOOM_REVIEW_PROFILE")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            raise ProfileError("ACTIVELOOM_REVIEW_PROFILE must be an absolute path")
        return path
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "activeloom" / "review-profile.json"


def _fail(message: str, status: int = EXIT_INVALID) -> NoReturn:
    raise ProfileError(message, status)


def validate_engine_settings(
    engine: str, settings: Any, *, partial: bool = False
) -> None:
    if engine not in ENGINES:
        _fail(f"unknown engine {engine!r}; expected one of {', '.join(ENGINES)}")
    if not isinstance(settings, dict):
        _fail(f"{engine}: settings must be an object")
    allowed = (
        {"model", "effort", "fallback"} if engine == "codex" else {"model", "effort"}
    )
    unknown = set(settings) - allowed
    if unknown:
        _fail(f"{engine}: unknown keys {sorted(unknown)}")
    if not partial and not {"model", "effort"} <= set(settings):
        _fail(f"{engine}: model and effort are both required")
    fallback = settings.get("fallback")
    if fallback is not None:
        if not isinstance(fallback, dict) or set(fallback) != {"model", "effort"}:
            _fail(f"{engine}: fallback requires both model and effort")
        validate_engine_settings(engine, fallback)
        if fallback["model"] == INHERIT:
            _fail(f"{engine}: fallback requires an explicit model")
    if "model" in settings:
        model = settings["model"]
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            _fail(f"{engine}: invalid model identifier {model!r}")
        if model == INHERIT and engine not in INHERIT_ENGINES:
            _fail(
                f"{engine}: an explicit model is required; {INHERIT!r} is not supported"
            )
    if "effort" in settings:
        effort = settings["effort"]
        if effort not in EFFORTS[engine]:
            _fail(
                f"{engine}: invalid effort {effort!r}; "
                f"expected one of {', '.join(EFFORTS[engine])}"
            )


def validate_order(tier: str, order: Any) -> None:
    if tier not in TIERS:
        _fail(f"unknown tier {tier!r}; expected one of {', '.join(TIERS)}")
    if (
        not isinstance(order, list)
        or not 1 <= len(order) <= len(ENGINES)
        or len(set(order)) != len(order)
        or any(engine not in ENGINES for engine in order)
    ):
        _fail(f"order.{tier}: expected 1-3 distinct engines from {', '.join(ENGINES)}")


def validate_profile(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        _fail("profile must be a JSON object")
    required = {
        "schema_version",
        "defaults_version",
        "confirmed_at",
        "engines",
        "order",
    }
    unknown = set(document) - required - {"repos"}
    if unknown:
        _fail(f"profile has unknown keys {sorted(unknown)}")
    missing = required - set(document)
    if missing:
        _fail(f"profile is missing {sorted(missing)}")
    if document["schema_version"] != SCHEMA_VERSION:
        _fail(f"unsupported profile schema_version {document['schema_version']!r}")
    for key in ("defaults_version", "confirmed_at"):
        if not isinstance(document[key], str) or not document[key]:
            _fail(f"profile {key} must be a nonempty string")
    engines = document["engines"]
    if not isinstance(engines, dict) or set(engines) != set(ENGINES):
        _fail(f"profile engines must configure exactly {', '.join(ENGINES)}")
    for engine, settings in engines.items():
        validate_engine_settings(engine, settings)
    order = document["order"]
    if not isinstance(order, dict) or set(order) != set(TIERS):
        _fail(f"profile order must define exactly {', '.join(TIERS)}")
    for tier, engines_in_order in order.items():
        validate_order(tier, engines_in_order)
    repos = document.get("repos", {})
    if not isinstance(repos, dict):
        _fail("profile repos must be an object")
    for repo, override in repos.items():
        if not REPO_RE.fullmatch(repo):
            _fail(f"repos: invalid repository {repo!r}; expected OWNER/REPO")
        if not isinstance(override, dict) or not override:
            _fail(f"repos.{repo}: override must be a nonempty object")
        if set(override) - {"engines", "order"}:
            _fail(f"repos.{repo}: only engines and order may be overridden")
        for engine, settings in override.get("engines", {}).items():
            validate_engine_settings(engine, settings, partial=True)
        for tier, engines_in_order in override.get("order", {}).items():
            validate_order(tier, engines_in_order)
    return document


def load_defaults() -> dict[str, Any]:
    try:
        document = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"recommended defaults are unreadable: {error}")
    if not isinstance(document, dict):
        _fail("recommended defaults must be a JSON object")
    # `notes` is explanatory text for setup; it is never stored in a profile.
    settings = {key: value for key, value in document.items() if key != "notes"}
    validate_profile({**settings, "confirmed_at": "defaults"})
    return dict(document)


def load_profile() -> dict[str, Any] | None:
    path = profile_path()
    if path.is_symlink():
        _fail(f"review profile cannot be a symlink: {path}")
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"review profile is unreadable: {path}: {error}")
    return validate_profile(document)


def require_profile() -> dict[str, Any]:
    document = load_profile()
    if document is None:
        _fail(
            f"no review profile at {profile_path()}. Run the review-setup skill "
            "(or `npx activeloom review-config init`) and confirm the model, effort "
            "and engine order before launching reviewers.",
            EXIT_UNCONFIGURED,
        )
    return document


def save_profile(document: dict[str, Any]) -> Path:
    validate_profile(document)
    path = profile_path()
    if path.is_symlink() or path.parent.is_symlink():
        _fail(f"review profile path cannot be a symlink: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".review-profile-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def effective(document: dict[str, Any], repo: str | None) -> dict[str, Any]:
    engines = {
        engine: dict(settings) for engine, settings in document["engines"].items()
    }
    order = {tier: list(value) for tier, value in document["order"].items()}
    override = document.get("repos", {}).get(repo) if repo else None
    if override:
        for engine, settings in override.get("engines", {}).items():
            engines[engine].update(settings)
        order.update(
            {tier: list(value) for tier, value in override.get("order", {}).items()}
        )
    return {"engines": engines, "order": order, "repo_override": bool(override)}


def resolve(engine: str, repo: str | None) -> dict[str, Any]:
    if engine not in ENGINES:
        _fail(f"unknown engine {engine!r}")
    pinned_model = os.environ.get(PIN_MODEL)
    pinned_effort = os.environ.get(PIN_EFFORT)
    if pinned_model is not None or pinned_effort is not None:
        # The review-chain runner pins a run's settings when it starts, so a
        # profile edit during the run cannot change the reviewers it launches.
        if pinned_model is None or pinned_effort is None:
            _fail(f"{PIN_MODEL} and {PIN_EFFORT} must be set together")
        settings = {"model": pinned_model, "effort": pinned_effort}
        validate_engine_settings(engine, settings)
        return {"engine": engine, **settings, "source": "run-pinned"}
    merged = effective(require_profile(), repo)
    source = "repository override" if merged["repo_override"] else "user profile"
    return {"engine": engine, **merged["engines"][engine], "source": source}


def parse_assignment(assignment: str) -> tuple[str, str, str]:
    key, separator, value = assignment.partition("=")
    section, dot, field = key.partition(".")
    if not separator or not dot or not value:
        _fail(f"invalid assignment {assignment!r}; expected e.g. claude.effort=medium")
    return section, field, value


def apply_assignments(target: dict[str, Any], assignments: list[str]) -> None:
    for assignment in assignments:
        section, field, value = parse_assignment(assignment)
        if section == "order":
            order = [engine.strip() for engine in value.split(",")]
            validate_order(field, order)
            target.setdefault("order", {})[field] = order
        elif section in ENGINES and field in ("model", "effort"):
            validate_engine_settings(section, {field: value}, partial=True)
            target.setdefault("engines", {}).setdefault(section, {})[field] = value
        elif section == "codex" and field == "fallback" and value == "none":
            target.setdefault("engines", {}).setdefault(section, {})[field] = None
        elif section == "codex" and field in ("fallback.model", "fallback.effort"):
            settings = target.setdefault("engines", {}).setdefault(section, {})
            if settings.get("fallback") is None:
                settings["fallback"] = {}
            settings["fallback"][field.split(".")[1]] = value
        else:
            _fail(f"unknown setting {section}.{field}")


def command_show(args: argparse.Namespace) -> None:
    defaults = load_defaults()
    document = load_profile()
    report: dict[str, Any] = {
        "path": str(profile_path()),
        "configured": document is not None,
        "current_defaults_version": defaults["defaults_version"],
    }
    if document is not None:
        report.update(
            defaults_version=document["defaults_version"],
            confirmed_at=document["confirmed_at"],
            **effective(document, args.repo),
        )
    print(json.dumps(report, indent=2, sort_keys=True))


def command_init(args: argparse.Namespace) -> None:
    # --replace must also recover from a profile that no longer validates.
    if not args.replace and load_profile() is not None:
        _fail(
            f"a review profile already exists at {profile_path()}; use set to change it",
            EXIT_REFUSED,
        )
    defaults = load_defaults()
    if args.from_file:
        try:
            source = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            _fail(f"cannot read {args.from_file}: {error}")
        if not isinstance(source, dict):
            _fail("the source file must hold a JSON object")
        document = {**source, "schema_version": SCHEMA_VERSION}
        document.setdefault("defaults_version", defaults["defaults_version"])
    else:
        document = {
            key: defaults[key]
            for key in ("schema_version", "defaults_version", "engines", "order")
        }
    document["confirmed_at"] = now()
    apply_assignments(document, args.assignments)
    print(save_profile(document))


def command_set(args: argparse.Namespace) -> None:
    document = require_profile()
    if args.repo:
        if not REPO_RE.fullmatch(args.repo):
            _fail(f"invalid repository {args.repo!r}; expected OWNER/REPO")
        override = document.setdefault("repos", {}).setdefault(args.repo, {})
        apply_assignments(override, args.assignments)
    else:
        apply_assignments(document, args.assignments)
    document["confirmed_at"] = now()
    save_profile(document)
    print(json.dumps(effective(document, args.repo), indent=2, sort_keys=True))


def command_unset(args: argparse.Namespace) -> None:
    document = require_profile()
    repos = document.get("repos", {})
    if args.repo not in repos:
        _fail(f"no override for {args.repo}", EXIT_REFUSED)
    if not args.keys:
        del repos[args.repo]
    for key in args.keys:
        section, dot, field = key.partition(".")
        container = repos[args.repo].get(
            "order" if section == "order" else "engines", {}
        )
        if section == "order" and field in container:
            del container[field]
        elif section in container and field in container[section]:
            del container[section][field]
            if not container[section]:
                del container[section]
        else:
            _fail(f"{args.repo} has no override for {key}", EXIT_REFUSED)
    if args.repo in repos:
        override = repos[args.repo]
        for name in ("engines", "order"):
            if name in override and not override[name]:
                del override[name]
        if not override:
            del repos[args.repo]
    if not repos:
        document.pop("repos", None)
    document["confirmed_at"] = now()
    save_profile(document)
    print(json.dumps(effective(document, args.repo), indent=2, sort_keys=True))


def command_resolve(args: argparse.Namespace) -> None:
    print(json.dumps(resolve(args.engine, args.repo), sort_keys=True))


def command_launch_args(args: argparse.Namespace) -> None:
    settings = resolve(args.engine, args.repo)
    print(settings["model"])
    print(settings["effort"])


def command_order(args: argparse.Namespace) -> None:
    print(",".join(effective(require_profile(), args.repo)["order"][args.tier]))


def command_detect(args: argparse.Namespace) -> None:
    report = {
        engine: {"cli": cli, "installed": shutil.which(cli) is not None}
        for engine, cli in CLIS.items()
    }
    print(json.dumps(report, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("path", help="print the profile path")
    commands.add_parser("defaults", help="print the recommended defaults")
    commands.add_parser("detect", help="report which engine CLIs are on PATH")
    show = commands.add_parser("show", help="print the stored profile, if any")
    show.add_argument("--repo")
    init = commands.add_parser("init", help="write a first profile")
    source = init.add_mutually_exclusive_group(required=True)
    source.add_argument("--accept-defaults", action="store_true")
    source.add_argument("--from-file")
    init.add_argument("--replace", action="store_true")
    init.add_argument("assignments", nargs="*")
    set_parser = commands.add_parser("set", help="change stored settings")
    set_parser.add_argument("--repo")
    set_parser.add_argument("assignments", nargs="+")
    unset = commands.add_parser("unset", help="remove a repository override")
    unset.add_argument("--repo", required=True)
    unset.add_argument("keys", nargs="*")
    for name in ("resolve", "launch-args"):
        command = commands.add_parser(name, help="settings one engine launches with")
        command.add_argument("--engine", required=True, choices=ENGINES)
        command.add_argument("--repo")
    order = commands.add_parser("order", help="engine order for a tier")
    order.add_argument("--tier", required=True, choices=TIERS)
    order.add_argument("--repo")
    return root


HANDLERS = {
    "path": lambda args: print(profile_path()),
    "defaults": lambda args: print(
        json.dumps(load_defaults(), indent=2, sort_keys=True)
    ),
    "detect": command_detect,
    "show": command_show,
    "init": command_init,
    "set": command_set,
    "unset": command_unset,
    "resolve": command_resolve,
    "launch-args": command_launch_args,
    "order": command_order,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        HANDLERS[args.command](args)
    except ProfileError as error:
        print(f"review profile: {error}", file=sys.stderr)
        return error.status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
