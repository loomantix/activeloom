#!/usr/bin/env python3
"""Compatibility preflight for consumer agent-loop configuration.

The check is non-mutating: it names every line to change and never edits the
config itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class DoctorError(RuntimeError):
    """An incompatible consumer configuration."""


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    configured = os.environ.get("AGENT_LOOP_REAL_GIT")
    if not configured:
        raise DoctorError("trusted Git executable is unavailable")
    configured_path = Path(configured)
    if not configured_path.is_absolute():
        raise DoctorError("trusted Git executable is invalid")
    resolved = configured_path.resolve(strict=True)
    if not os.access(resolved, os.X_OK):
        raise DoctorError("trusted Git executable is invalid")
    return subprocess.run(
        [
            str(resolved),
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.excludesFile=/dev/null",
            "--no-optional-locks",
            "-C",
            str(project),
            *args,
        ],
        capture_output=True,
        check=False,
    )


# Keys whose settings now come from the per-user review profile.
RETIRED_KEYS = ("claude_effort_policy", "worker_model", "worker_fallback_model", "worker_effort")
# Review hooks whose model and effort flags read the pinned reviewer settings.
REVIEW_HOOK_ENGINES = {"claude_review_hook": "claude", "codex_review_hook": "codex"}
PROFILE_HELPER = ".codex/skills/critique/scripts/review-profile.py"

# One shell word: a quoted string or an unquoted run up to whitespace,
# quotes, redirection, or grouping.
_WORD = r"""(?:"[^"]*"|'[^']*'|[^\s'"<>()]+)"""
_FLAG_PATTERNS = {
    "claude": (
        ("model", re.compile(rf"(?<![\w-])(?P<flag>--model)(?:=|\s+)(?P<value>{_WORD})")),
        ("effort", re.compile(rf"(?<![\w-])(?P<flag>--effort)(?:=|\s+)(?P<value>{_WORD})")),
    ),
    "codex": (
        (
            "model",
            re.compile(rf"(?<![\w-])(?P<flag>-m\s+|--model(?:=|\s+))(?P<value>{_WORD})"),
        ),
    ),
}
# `codex -c key=value`, with the token optionally quoted and the TOML value
# optionally quoted inside it.
_CODEX_CONFIG = re.compile(
    r"""(?<![\w-])(?:-c|--config)(?:=|\s+)"""
    r"""(?P<token>(?P<q>['"]?)(?P<key>model|model_reasoning_effort)="""
    r"""(?P<value>\\"[^"\\]*\\"|"[^"]*"|'[^']*'|[^\s'"<>()\\]+)(?P=q))"""
)
_CODEX_CONFIG_FIELDS = {"model": "model", "model_reasoning_effort": "effort"}


@dataclass(frozen=True)
class HookLiteral:
    engine: str
    field: str
    flag: str
    value: str

    @property
    def variable(self) -> str:
        return f"AGENT_LOOP_{self.engine.upper()}_{self.field.upper()}"


def _command_segments(hook: str) -> tuple[list[tuple[int, int]], set[int]]:
    """Spans of the simple commands in a hook, and the offsets inside quotes.

    Commands split at unquoted ; & | and newlines outside `$(...)`.
    """
    segments: list[tuple[int, int]] = []
    quoted: set[int] = set()
    start = 0
    quote = ""
    depth = 0
    index = 0
    while index < len(hook):
        char = hook[index]
        if char == "\\" and quote != "'":
            if quote:
                quoted.update((index, index + 1))
            index += 2
            continue
        if quote:
            quoted.add(index)
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif hook.startswith("$(", index):
            depth += 1
            index += 1
        elif char == ")" and depth:
            depth -= 1
        elif char in ";&|\n" and not depth:
            segments.append((start, index))
            start = index + 1
        index += 1
    segments.append((start, len(hook)))
    return segments, quoted


def _unquote(word: str) -> str:
    if len(word) >= 4 and word.startswith('\\"') and word.endswith('\\"'):
        return word[2:-2]
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        return word[1:-1]
    return word


def _hook_literals(key: str, hook: str) -> list[HookLiteral]:
    """Model and effort flags with a literal value, in commands that run the hook's engine CLI.

    A value that expands a variable is not a literal and is left alone.
    """
    engine = REVIEW_HOOK_ENGINES[key]
    program = re.compile(rf"(?:^|(?<=[\s(]))(?:[^\s'\"]*/)?{engine}(?=\s|$)")
    segments, quoted = _command_segments(hook)
    literals: list[HookLiteral] = []
    for seg_start, seg_end in segments:
        segment = hook[seg_start:seg_end]
        launch = next(
            (m for m in program.finditer(segment) if seg_start + m.start() not in quoted),
            None,
        )
        if launch is None:
            continue
        # (field, flag, start of the match, raw value)
        found: list[tuple[str, str, int, str]] = []
        for field, pattern in _FLAG_PATTERNS[engine]:
            for match in pattern.finditer(segment, launch.end()):
                flag = match["flag"].strip().rstrip("=")
                found.append((field, flag, match.start(), match["value"]))
        if engine == "codex":
            for match in _CODEX_CONFIG.finditer(segment, launch.end()):
                field = _CODEX_CONFIG_FIELDS[match["key"]]
                found.append((field, f"-c {match['key']}", match.start(), match["value"]))
        for field, flag, start, raw in sorted(found, key=lambda item: item[2]):
            value = _unquote(raw)
            # Flag-like text inside a quoted argument — a prompt, typically — is
            # not a flag on the command line.
            if seg_start + start in quoted:
                continue
            if not value or any(char in value for char in "$`\\"):
                continue
            literals.append(HookLiteral(engine, field, flag, value))
    return literals


def _warn(message: str) -> None:
    print(f"agent-loop config doctor: warning: {message}", file=sys.stderr)


def _config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-z_]+)\s*=\s*(.*)", line)
        if match is None:
            raise DoctorError(f"invalid config line: {raw}")
        key, value = match.groups()
        if key in values:
            raise DoctorError(f"duplicate config key: {key}")
        values[key] = value.rstrip()
    return values


def _version(command: list[str], label: str) -> str:
    # `command[0]` may be a PATH lookup (`node`) rather than an interpreter we
    # know exists, so a missing runtime must read as a doctor failure instead
    # of an uncaught OSError traceback.
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise DoctorError(f"{label} could not be executed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or "<no stderr>"
        raise DoctorError(
            f"{label} compatibility query failed (exit {result.returncode}): {detail}"
        )
    return result.stdout.strip()


def _hash_object(project: Path, path: Path, label: str) -> str:
    # Must go through `_git` like every other Git call here: the controller
    # exports AGENT_LOOP_REAL_GIT so the pin cannot be steered by PATH, and a
    # bare "git" would let a shim choose the OID this check compares against —
    # while the expected side is read with the trusted binary.
    result = _git(project, "hash-object", "--no-filters", str(path))
    if result.returncode != 0:
        detail = (
            result.stderr.decode("utf-8", errors="replace").strip() or "<no stderr>"
        )
        raise DoctorError(f"{label} could not be hashed: {detail}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _require_base_blob(project: Path, base_ref: str, relative: str) -> tuple[str, str]:
    result = _git(project, "ls-tree", "-z", base_ref, "--", relative)
    if result.returncode != 0:
        detail = (
            result.stderr.decode("utf-8", errors="replace").strip() or "<no stderr>"
        )
        raise DoctorError(f"could not inspect pinned base review surface: {detail}")
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1:
        raise DoctorError(f"pinned base review surface is missing: {relative}")
    metadata, separator, path = records[0].partition(b"\t")
    fields = metadata.split()
    if (
        separator != b"\t"
        or path != relative.encode()
        or len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
    ):
        raise DoctorError(
            f"pinned base review surface is not a regular blob: {relative}"
        )
    return fields[0].decode(), fields[2].decode()


def _base_blob(project: Path, oid: str, label: str) -> bytes:
    result = _git(project, "cat-file", "blob", oid)
    if result.returncode != 0:
        detail = (
            result.stderr.decode("utf-8", errors="replace").strip() or "<no stderr>"
        )
        raise DoctorError(f"could not materialize pinned {label}: {detail}")
    return result.stdout


def _verify_claude_ledger_protocol(
    project: Path, ledger_oid: str, package_oid: str
) -> None:
    with tempfile.TemporaryDirectory(prefix="agent-loop-claude-ledger-") as directory:
        root = Path(directory)
        ledger = root / "review-ledger.js"
        package = root / "package.json"
        ledger.write_bytes(_base_blob(project, ledger_oid, "Claude review ledger"))
        package.write_bytes(_base_blob(project, package_oid, "Claude ledger package"))
        if (
            _version(
                ["node", str(ledger), "--protocol-version"], "Claude review ledger"
            )
            != "3"
        ):
            raise DoctorError(
                "Claude review-ledger protocol is incompatible with contract v4"
            )


def _verify_protocols(ledger: Path, state: Path, review_push: Path) -> None:
    if _version(["node", str(ledger), "--protocol-version"], "review ledger") != "3":
        raise DoctorError("review-ledger protocol is incompatible with contract v3")
    if (
        _version([sys.executable, "-I", str(state), "--state-version"], "run state")
        != "2"
    ):
        raise DoctorError("agent-loop state protocol is incompatible")
    if (
        _version(
            [sys.executable, "-I", str(state), "--batch-state-version"],
            "batch state",
        )
        != "1"
    ):
        raise DoctorError("agent-loop batch state protocol is incompatible")
    if _version([str(review_push), "--protocol-version"], "review push") != "2":
        raise DoctorError("review-push protocol is incompatible")


@dataclass(frozen=True)
class ReviewerSettings:
    model: str
    effort: str
    source: str


def _origin_repo(root: Path) -> str | None:
    """owner/name of a GitHub origin remote, or None."""
    try:
        result = subprocess.run(
            [os.environ.get("AGENT_LOOP_REAL_GIT") or "git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", result.stdout.strip())
    return match.group(1) if result.returncode == 0 and match else None


class _SettingsResolver:
    """The reviewer settings a hook's literals are checked against.

    The wrapper passes `--settings-from-env` after pinning the run, so the
    doctor checks the values the run launches with. A standalone run resolves
    the per-user review profile the way the wrapper's pin step does.
    """

    def __init__(self, root: Path, repo: str | None, from_env: bool) -> None:
        self.root = root
        self.repo = repo
        self.from_env = from_env
        self.cache: dict[str, ReviewerSettings] = {}

    def get(self, engine: str) -> ReviewerSettings:
        if engine not in self.cache:
            self.cache[engine] = self._env(engine) if self.from_env else self._profile(engine)
        return self.cache[engine]

    @staticmethod
    def _env(engine: str) -> ReviewerSettings:
        prefix = f"AGENT_LOOP_{engine.upper()}"
        model = os.environ.get(f"{prefix}_MODEL", "")
        effort = os.environ.get(f"{prefix}_EFFORT", "")
        if not model or not effort:
            raise DoctorError(
                f"--settings-from-env needs {prefix}_MODEL and {prefix}_EFFORT, "
                "which the wrapper exports after pinning the run"
            )
        return ReviewerSettings(model, effort, "the run's pinned settings")

    def _profile(self, engine: str) -> ReviewerSettings:
        helper = self.root / PROFILE_HELPER
        if not helper.is_file():
            raise DoctorError(f"required review profile helper is missing: {PROFILE_HELPER}")
        command = [sys.executable, "-I", str(helper), "resolve", "--engine", engine]
        repo = self.repo or _origin_repo(self.root)
        if repo:
            command += ["--repo", repo]
        # The profile is what a new run pins; a review-chain run pin in this
        # shell does not describe it.
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in ("ACTIVELOOM_REVIEW_MODEL", "ACTIVELOOM_REVIEW_EFFORT")
        }
        result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            reason = detail[-1] if detail else f"exit {result.returncode}"
            raise DoctorError(
                f"cannot resolve {engine} reviewer settings to check the review hook literals "
                f"against ({reason}); run the review-setup skill, or edit the hooks so they "
                "read the pinned settings instead"
            )
        try:
            resolved = json.loads(result.stdout)
            source = f"the {resolved['source']}"
            return ReviewerSettings(resolved["model"], resolved["effort"], source)
        except (ValueError, KeyError, TypeError) as error:
            raise DoctorError(f"review profile helper printed unexpected output: {error}") from error


def _check_retired_keys(values: dict[str, str]) -> None:
    for key in RETIRED_KEYS:
        if key not in values:
            continue
        if values[key]:
            raise DoctorError(
                f"{key} is retired: reviewer and worker settings come from the per-user review "
                "profile. Remove it from the config"
            )
        _warn(f"{key} is retired and has no effect; remove it from the config")


def _check_hook_literals(values: dict[str, str], resolver: _SettingsResolver) -> None:
    """Refuse a review-hook model or effort literal that differs from the resolved settings."""
    for key in REVIEW_HOOK_ENGINES:
        for literal in _hook_literals(key, values.get(key, "")):
            settings = resolver.get(literal.engine)
            expected = settings.model if literal.field == "model" else settings.effort
            if literal.value != expected:
                raise DoctorError(
                    f"{key} passes {literal.flag} {literal.value}, but {settings.source} set "
                    f"{literal.engine} {literal.field} {expected}. Edit the hook so it reads "
                    f"${literal.variable}"
                )
            _warn(
                f"{key} passes {literal.flag} {literal.value} literally; it matches "
                f"{settings.source}, but a developer whose profile differs is refused. "
                f"Edit the hook so it reads ${literal.variable}"
            )


def doctor(
    project: Path,
    base_ref: str | None,
    *,
    repo: str | None = None,
    settings_from_env: bool = False,
) -> None:
    root = project.resolve()
    skill = root / ".codex/skills/agent-loop"
    config_path = skill / "agent-loop.config"
    prompt_path = skill / "prompt.txt"
    instructions_path = root / "agent-loop-instructions.md"
    ledger = root / ".codex/skills/critique/scripts/review-ledger.js"
    state = skill / "scripts/agent-loop-state.py"
    review_push = skill / "scripts/review-push.sh"
    review_launcher = skill / "scripts/run-codex-review.sh"
    for path in (
        config_path,
        prompt_path,
        instructions_path,
        ledger,
        state,
        review_push,
        review_launcher,
    ):
        if not path.is_file() or path.is_symlink():
            raise DoctorError(
                f"required agent-loop file is missing: {path.relative_to(root)}"
            )
    values = _config(config_path)
    _check_retired_keys(values)
    contract = values.get("review_contract_version")
    if contract not in {"3", "4"}:
        raise DoctorError("review_contract_version must be 3 or 4")
    prompt = prompt_path.read_text(encoding="utf-8")
    instructions = instructions_path.read_text(encoding="utf-8")
    for token in ("AGENT_LOOP_ISSUE_TITLE", "AGENT_LOOP_ISSUE_BODY"):
        if token not in prompt:
            raise DoctorError(f"worker prompt must read {token}")
    if re.search(r"\bgh\s+(?:api|issue|pr|repo)\b", prompt + "\n" + instructions):
        raise DoctorError("worker prompt or instructions require masked gh")
    if "local commit" not in prompt.lower() or "do not push" not in prompt.lower():
        raise DoctorError("worker prompt must require a local commit and forbid push")
    if (
        "AGENT_LOOP_ISSUE_TITLE" not in instructions
        or "AGENT_LOOP_ISSUE_BODY" not in instructions
    ):
        raise DoctorError(
            "worker instructions must describe wrapper-provided issue context"
        )

    hooks = {
        "codex": values.get("codex_review_hook", ""),
        "claude": values.get("claude_review_hook", ""),
    }
    if contract == "4":
        if not base_ref:
            raise DoctorError("contract v4 compatibility requires --base-ref")
        launcher_relative = ".codex/skills/agent-loop/scripts/run-codex-review.sh"
        launcher_mode, launcher_oid = _require_base_blob(
            root, base_ref, launcher_relative
        )
        if launcher_mode != "100755":
            raise DoctorError("pinned review launcher is not executable")
        local_launcher_oid = _hash_object(root, review_launcher, "review launcher")
        if local_launcher_oid != launcher_oid:
            raise DoctorError("review launcher differs from the pinned base blob")
        expected_hooks = {
            "codex": '"$AGENT_LOOP_CODEX_REVIEW_LAUNCHER" --engine codex',
            "claude": '"$AGENT_LOOP_CODEX_REVIEW_LAUNCHER" --engine claude',
        }
        for engine, expected in expected_hooks.items():
            if hooks[engine] != expected:
                raise DoctorError(
                    f"{engine}_review_hook must use the dedicated contract-v4 review launcher"
                )
        if (
            _version([str(review_launcher), "--contract-version"], "review launcher")
            != "4"
        ):
            raise DoctorError("review launcher is incompatible with contract v4")
        wrapper_paths = _version(
            [str(review_launcher), "--wrapper-paths"], "wrapper review tools"
        ).splitlines()
        if not wrapper_paths or len(wrapper_paths) != len(set(wrapper_paths)):
            raise DoctorError("wrapper required review tools are invalid")
        for relative in wrapper_paths:
            if not (
                relative.startswith(".codex/skills/agent-loop/scripts/")
                or relative == ".codex/skills/issues/scripts/ready.py"
                or relative == ".codex/skills/critique/scripts/review-ledger.js"
            ):
                raise DoctorError(
                    f"wrapper required review path is invalid: {relative}"
                )
            local_path = root / relative
            if not local_path.is_file() or local_path.is_symlink():
                raise DoctorError(f"required review tool is missing: {relative}")
            mode, oid = _require_base_blob(root, base_ref, relative)
            expected_mode = (
                "100644"
                if relative == ".codex/skills/critique/scripts/review-ledger.js"
                else "100755"
            )
            if mode != expected_mode:
                raise DoctorError(f"pinned review tool has an unsafe mode: {relative}")
            local_oid = _hash_object(root, local_path, f"review tool {relative}")
            if local_oid != oid:
                raise DoctorError(
                    f"review tool differs from the pinned base blob: {relative}"
                )
        _verify_protocols(ledger, state, review_push)
        supervisor = root / ".codex/skills/agent-loop/scripts/process-supervisor.py"
        if _version(
            [sys.executable, "-I", str(supervisor), "--self-test"],
            "hook process supervisor",
        ) != ("linux-subreaper-v1"):
            raise DoctorError("hook process supervisor self-test failed")
        claude_surface_oids: dict[str, str] = {}
        for engine, prefix in (("codex", ".codex/"), ("claude", ".claude/")):
            required_paths = _version(
                [str(review_launcher), "--required-paths", engine],
                f"{engine} review surface",
            ).splitlines()
            if not required_paths or len(required_paths) != len(set(required_paths)):
                raise DoctorError(f"{engine} required review surface is invalid")
            for relative in required_paths:
                if not relative.startswith(prefix):
                    raise DoctorError(
                        f"{engine} required review path is invalid: {relative}"
                    )
                _, oid = _require_base_blob(root, base_ref, relative)
                if engine == "claude":
                    claude_surface_oids[relative] = oid
        _verify_claude_ledger_protocol(
            root,
            claude_surface_oids[".claude/skills/critique/scripts/review-ledger.js"],
            claude_surface_oids[".claude/skills/critique/scripts/package.json"],
        )
    else:
        _verify_protocols(ledger, state, review_push)
        obsolete = (
            "AGENT_LOOP_REVIEW_OUTCOME_FILE",
            "local-review-pass:v1",
            "local-review-complete:v1",
            "local-review-disposition:v1",
            "review-ledger.py",
        )
        for engine, hook in hooks.items():
            if any(token in hook for token in obsolete):
                raise DoctorError(
                    f"{engine}_review_hook contains obsolete review ownership"
                )
            if (
                "AGENT_LOOP_REVIEW_RESULT_FILE" not in hook
                or "write-result" not in hook
            ):
                raise DoctorError(
                    f"{engine}_review_hook must preserve the contract-v3 result and push helper contract"
                )
            # v4 pins both hook strings byte-for-byte, so this stays the only
            # thing standing between a v3 consumer and a hook that pushes
            # directly instead of through the wrapper-owned helper.
            if "AGENT_LOOP_REVIEW_PUSH_HELPER" not in hook or re.search(
                r"\bgit\s+push\b", hook
            ):
                raise DoctorError(
                    f"{engine}_review_hook must preserve the contract-v3 result and push helper contract"
                )
            # Accept any shell delimiter after the skill name; the point is to
            # reject a different skill (a retired `grill` name, or a
            # `deepcritique`-prefixed word), not to enumerate separators.
            if not re.search(r"(?:^|[ /])deepcritique(?![\w-])", hook):
                raise DoctorError(f"{engine}_review_hook must invoke deepcritique")
    _check_hook_literals(values, _SettingsResolver(root, repo, settings_from_env))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--base-ref")
    parser.add_argument(
        "--repo", help="owner/name for repository overrides; default: the origin remote"
    )
    parser.add_argument(
        "--settings-from-env",
        action="store_true",
        help="check hook literals against the AGENT_LOOP_* settings the wrapper pinned",
    )
    args = parser.parse_args()
    doctor(
        Path(args.project_dir),
        args.base_ref,
        repo=args.repo,
        settings_from_env=args.settings_from_env,
    )
    print("agent-loop config doctor: compatible")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DoctorError, OSError) as error:
        print(f"agent-loop config doctor: {error}", file=sys.stderr)
        raise SystemExit(1) from error
