#!/usr/bin/env python3
"""Resolve a run's reviewer and worker settings once, pin them, and switch to a fallback once.

Stdlib only. Settings come from `review-profile.py` beside this file; this
script never reads or writes the profile itself. A run pins each engine's
settings the first time it needs them and keeps them for its whole life, so a
profile edit applies to the next run. After a recognized capacity rejection
with unchanged code and review evidence, the caller switches an engine to its
pinned fallback exactly once; a second switch is refused.

The review-chain runner imports this file and keeps the pins in its
checkpoint. Shell callers use the command line, which keeps them in a pin file:

    review-settings.py pin --pin-file FILE [--repo OWNER/REPO]
        [--reviewer ENGINE ...] [--worker ENGINE ...] [--format env|json]
    review-settings.py fallback --pin-file FILE --engine ENGINE
        [--role reviewer|worker] [--format env|json]

`pin` resolves only the pairs the file does not hold yet and writes the file
only when every requested pair resolved. It prints every pinned pair with the
settings actually in use: with `--format env`, one shell-quoted `KEY=value`
line per setting, keys drawn only from `AGENT_LOOP_<ENGINE>_{MODEL,EFFORT,SOURCE}`
for reviewers and `AGENT_LOOP_<ENGINE>_WORKER_{MODEL,EFFORT,SOURCE}` for
workers. A newly pinned pair, and a fallback switch, is also logged once on
stderr.

Exit status: 0 success; 1 refused (an engine marked unavailable, no pinned
fallback, or the fallback already in use); 2 invalid input or pin file; 3 no
profile or missing keys, with JSON `{"missing": [...], "path": PROFILE}` on stdout
and one actionable line on stderr. Stdout is safe to evaluate only on exit 0.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

ENGINES = ("claude", "codex", "gemini")
ROLES = ("reviewer", "worker")
PAIR = ("model", "effort")
# Where each role keeps its pins. The reviewer keys are the review-chain
# runner's checkpoint keys, so its existing checkpoints read unchanged.
STATE_KEYS = {
    "reviewer": ("review_settings", "fallback_engines"),
    "worker": ("worker_settings", "worker_fallback_engines"),
}
PIN_FILE_VERSION = 1
FALLBACK_SOURCE = "capacity fallback"
PROFILE_SCRIPT = Path(__file__).resolve().with_name("review-profile.py")
PIN_ENVIRONMENT = ("ACTIVELOOM_REVIEW_MODEL", "ACTIVELOOM_REVIEW_EFFORT")

EXIT_REFUSED = 1
EXIT_INVALID = 2
EXIT_UNCONFIGURED = 3


class SettingsError(Exception):
    def __init__(
        self,
        message: str,
        status: int = EXIT_INVALID,
        missing: list[str] | None = None,
        path: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.missing = missing or []
        self.path = path


def _check_names(engine: str, role: str) -> None:
    if engine not in ENGINES:
        raise SettingsError(f"unknown engine {engine!r}")
    if role not in ROLES:
        raise SettingsError(f"unknown role {role!r}")


def _label(engine: str, role: str) -> str:
    return engine.capitalize() + (" worker" if role == "worker" else "")


def resolve(
    engine: str,
    role: str,
    repo: str | None,
    profile_script: Path = PROFILE_SCRIPT,
) -> dict[str, Any]:
    """Ask the profile helper for fresh settings, ignoring any inherited run pin."""
    _check_names(engine, role)
    env = {k: v for k, v in os.environ.items() if k not in PIN_ENVIRONMENT}
    argv = [sys.executable, "-I", str(profile_script), "resolve", "--engine", engine]
    if role == "worker":
        argv += ["--role", role]
    if repo:
        argv += ["--repo", repo]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    if result.returncode:
        missing: list[str] = []
        path = None
        try:
            report = json.loads(result.stdout)
            if isinstance(report, dict) and isinstance(report.get("missing"), list):
                missing = [str(key) for key in report["missing"]]
                path = str(report.get("path"))
        except ValueError:
            pass
        if result.returncode == EXIT_UNCONFIGURED and path is None:
            located = subprocess.run(
                [sys.executable, "-I", str(profile_script), "path"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            path = located.stdout.strip() or None
        # The profile helper's diagnostics name only the profile path and the
        # invalid setting, and the unconfigured case points at setup.
        raise SettingsError(
            result.stderr.strip() or f"review profile cannot resolve {engine}",
            result.returncode if result.returncode in (1, 2, 3) else EXIT_INVALID,
            missing,
            path,
        )
    settings = json.loads(result.stdout)
    _require_settings(settings, f"resolved {_label(engine, role)} settings")
    return dict(settings)


def _require_settings(settings: Any, label: str) -> None:
    if not isinstance(settings, dict) or not all(
        isinstance(settings.get(key), str) and settings[key] for key in PAIR
    ):
        raise SettingsError(f"{label} lack a model and effort")
    fallback = settings.get("fallback")
    if fallback is not None and not (
        isinstance(fallback, dict)
        and all(isinstance(fallback.get(key), str) and fallback[key] for key in PAIR)
    ):
        raise SettingsError(f"{label} hold an incomplete fallback")


def pinned(state: dict[str, Any], engine: str, role: str) -> dict[str, Any] | None:
    _check_names(engine, role)
    settings = state.get(STATE_KEYS[role][0], {}).get(engine)
    return dict(settings) if settings else None


def pin(
    state: dict[str, Any],
    engine: str,
    role: str,
    resolver: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Return the pinned settings, resolving them only on first use.

    The boolean says whether this call pinned them, so the caller persists and
    logs exactly once.
    """
    _check_names(engine, role)
    pins = state.setdefault(STATE_KEYS[role][0], {})
    if engine in pins:
        return dict(pins[engine]), False
    pins[engine] = resolver()
    return dict(pins[engine]), True


def switched(state: dict[str, Any], engine: str, role: str) -> bool:
    _check_names(engine, role)
    return engine in state.get(STATE_KEYS[role][1], [])


def selected(state: dict[str, Any], engine: str, role: str) -> dict[str, Any] | None:
    """The settings actually in use: the fallback once switched, else the pin."""
    settings = pinned(state, engine, role)
    if settings is None or not switched(state, engine, role):
        return settings
    fallback = settings.get("fallback")
    if not isinstance(fallback, dict):
        raise SettingsError("pinned fallback settings are missing")
    result = {**fallback, "engine": engine, "source": FALLBACK_SOURCE}
    if role == "worker":
        result["role"] = role
    return result


def check_fallback(
    state: dict[str, Any], engine: str, role: str, *, in_progress: bool = False
) -> dict[str, Any]:
    """Return the pinned fallback pair, or refuse a switch the run cannot make.

    `in_progress` marks a switch the caller already recorded as started, so a
    resumed switch is not mistaken for a second one.
    """
    settings = pinned(state, engine, role)
    fallback = settings.get("fallback") if settings else None
    if not isinstance(fallback, dict):
        raise SettingsError(
            "model is at capacity; no "
            f"{_label(engine, role)} fallback was pinned for this run",
            EXIT_REFUSED,
        )
    if switched(state, engine, role) and not in_progress:
        raise SettingsError(
            "configured fallback is also at capacity; no further retry", EXIT_REFUSED
        )
    return dict(fallback)


def switch_to_fallback(state: dict[str, Any], engine: str, role: str) -> dict[str, Any]:
    """Record the one switch; the caller persists it with its own run state."""
    check_fallback(state, engine, role)
    state.setdefault(STATE_KEYS[role][1], []).append(engine)
    result = selected(state, engine, role)
    assert result is not None
    return result


def describe(settings: dict[str, Any]) -> str:
    return (
        f"model {settings['model']}, effort {settings['effort']} "
        f"({settings['source']})"
    )


def environment(state: dict[str, Any]) -> dict[str, str]:
    """Every pinned pair as fixed-allowlist variable names."""
    values = {}
    for role in ROLES:
        infix = "_WORKER" if role == "worker" else ""
        for engine in ENGINES:
            settings = selected(state, engine, role)
            if settings is None:
                continue
            prefix = f"AGENT_LOOP_{engine.upper()}{infix}_"
            for key in (*PAIR, "source"):
                values[prefix + key.upper()] = str(settings[key])
    return values


def selections(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {role: {} for role in ROLES}
    for role in ROLES:
        for engine in ENGINES:
            settings = selected(state, engine, role)
            if settings is not None:
                report[role][engine] = settings
    return report


def load_pin_file(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise SettingsError(f"pin file cannot be a symlink: {path}")
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise SettingsError(f"pin file is unreadable: {path}: {error}") from error
    allowed = {"version", "repo", *(key for keys in STATE_KEYS.values() for key in keys)}
    if (
        not isinstance(document, dict)
        or document.get("version") != PIN_FILE_VERSION
        or set(document) - allowed
        or not (document.get("repo") is None or isinstance(document["repo"], str))
    ):
        raise SettingsError(f"pin file has an unsupported format: {path}")
    for role, (pins_key, fallback_key) in STATE_KEYS.items():
        pins = document.get(pins_key, {})
        switches = document.get(fallback_key, [])
        if not isinstance(pins, dict) or not isinstance(switches, list):
            raise SettingsError(f"pin file has an unsupported format: {path}")
        for engine, settings in pins.items():
            _check_names(engine, role)
            _require_settings(settings, f"pinned {_label(engine, role)} settings")
        for engine in switches:
            if engine not in pins or switches.count(engine) != 1:
                raise SettingsError(f"pin file has an unsupported format: {path}")
            selected(document, engine, role)
    return document


def save_pin_file(path: Path, document: dict[str, Any]) -> None:
    if path.parent.is_symlink():
        raise SettingsError(f"pin file directory cannot be a symlink: {path.parent}")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".review-settings-")
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


def emit(document: dict[str, Any], output: str) -> None:
    if output == "json":
        print(json.dumps(selections(document), indent=2, sort_keys=True))
        return
    for key, value in environment(document).items():
        print(f"{key}={shlex.quote(value)}")


def command_pin(args: argparse.Namespace) -> None:
    path = Path(args.pin_file)
    document = load_pin_file(path)
    if document is None:
        document = {"version": PIN_FILE_VERSION, "repo": args.repo}
    elif document.get("repo") != args.repo:
        raise SettingsError(
            f"pin file belongs to repository {document.get('repo')!r}, "
            f"not {args.repo!r}"
        )
    requests = [(engine, "reviewer") for engine in args.reviewer or []]
    requests += [(engine, "worker") for engine in args.worker or []]
    # Resolve every pair before writing anything, so a missing key or an
    # unavailable engine leaves no pin behind.
    resolved: dict[tuple[str, str], dict[str, Any]] = {}
    missing: list[str] = []
    messages: list[str] = []
    paths: list[str | None] = []
    for engine, role in dict.fromkeys(requests):
        if pinned(document, engine, role) is not None:
            continue
        try:
            resolved[(engine, role)] = resolve(engine, role, args.repo)
        except SettingsError as error:
            if error.status != EXIT_UNCONFIGURED:
                raise
            # Without a profile the helper names no keys; the pair needs both.
            prefix = f"{engine}.worker" if role == "worker" else engine
            keys = error.missing or [f"{prefix}.{key}" for key in PAIR]
            missing += [key for key in keys if key not in missing]
            messages.append(str(error))
            paths.append(error.path)
    if missing:
        # The profile helper's own line names the profile path and the fix.
        raise SettingsError(messages[0], EXIT_UNCONFIGURED, missing, paths[0])
    for (engine, role), settings in resolved.items():
        document.setdefault(STATE_KEYS[role][0], {})[engine] = settings
    if resolved:
        save_pin_file(path, document)
        for engine, role in resolved:
            current = selected(document, engine, role)
            assert current is not None
            print(
                f"Pinned {engine} {role} settings: {describe(current)}",
                file=sys.stderr,
            )
    emit(document, args.format)


def command_fallback(args: argparse.Namespace) -> None:
    path = Path(args.pin_file)
    document = load_pin_file(path)
    if document is None:
        raise SettingsError(f"no pin file at {path}; pin the run's settings first")
    current = switch_to_fallback(document, args.engine, args.role)
    save_pin_file(path, document)
    print(
        f"Capacity fallback: {args.engine} {args.role} {describe(current)}",
        file=sys.stderr,
    )
    emit(document, args.format)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = root.add_subparsers(dest="command", required=True)
    pin_parser = commands.add_parser("pin", help="pin settings and print them")
    pin_parser.add_argument("--reviewer", nargs="+", action="extend", choices=ENGINES)
    pin_parser.add_argument("--worker", nargs="+", action="extend", choices=ENGINES)
    pin_parser.add_argument("--repo")
    fallback = commands.add_parser("fallback", help="switch one pair to its fallback")
    fallback.add_argument("--engine", required=True, choices=ENGINES)
    fallback.add_argument("--role", choices=ROLES, default="reviewer")
    for command in (pin_parser, fallback):
        command.add_argument("--pin-file", required=True)
        command.add_argument("--format", choices=("env", "json"), default="env")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        (command_pin if args.command == "pin" else command_fallback)(args)
    except SettingsError as error:
        if error.status == EXIT_UNCONFIGURED:
            print(json.dumps({"missing": error.missing, "path": error.path}))
        print(f"review settings: {error}", file=sys.stderr)
        return error.status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
