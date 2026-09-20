#!/usr/bin/env python3
"""Compatibility preflight for consumer agent-loop configuration.

The check is non-mutating. `--migrate` is the one mode that writes: it moves
review-hook model and effort literals to the pinned-settings variables and
removes retired keys.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class DoctorError(RuntimeError):
    """An incompatible consumer configuration."""


# Words that are shell syntax or builtins rather than a program. A hook that
# starts with one of these is composed inline and its launch surface sits
# further in, so static resolution stops rather than guessing.
_SHELL_WORDS = frozenset(
    {
        "if", "then", "for", "while", "until", "case", "{", "(", "!", ":", ".",
        "source", "eval", "exec", "cd", "export", "set", "test", "[", "[[",
        "command", "builtin", "time", "true", "false",
    }
)


def _hook_program(hook: str) -> str | None:
    """The first command word of a hook, or None when it cannot be resolved statically."""
    for token in hook.split():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue  # a leading environment assignment
        if token in _SHELL_WORDS or not re.fullmatch(r"[A-Za-z0-9_.+-]+", token):
            return None
        return token
    return None


# Keys whose settings now come from the per-user review profile.
RETIRED_KEYS = ("claude_effort_policy", "worker_model", "worker_fallback_model", "worker_effort")
# Review hooks whose model and effort flags read the pinned reviewer settings.
REVIEW_HOOK_ENGINES = {"claude_review_hook": "claude", "codex_review_hook": "codex"}
MIGRATE_COMMAND = "config-doctor.py --project-dir <repo> --migrate"
PROFILE_HELPER = ".claude/skills/critique/scripts/review-profile.py"

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
# optionally quoted inside it, including as `\"...\"`.
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
    start: int
    end: int
    replacement: str

    @property
    def variable(self) -> str:
        return _setting_variable(self.engine, self.field)


def _setting_variable(engine: str, field: str) -> str:
    return f"AGENT_LOOP_{engine.upper()}_{field.upper()}"


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

    A value that expands a variable is not a literal and is left alone, and so
    is flag-like text inside a quoted argument such as the prompt. A model flag
    is replaced whole by a form that drops it for a pinned model of `inherit`.
    """
    engine = REVIEW_HOOK_ENGINES[key]
    program = re.compile(rf"(?:^|(?<=[\s(]))(?:[^\s'\"]*/)?{engine}(?=\s|$)")
    segments, quoted = _command_segments(hook)
    literals: list[HookLiteral] = []
    for seg_start, seg_end in segments:
        segment = hook[seg_start:seg_end]
        launch = next(
            (m for m in program.finditer(segment) if seg_start + m.start() not in quoted), None
        )
        if launch is None:
            continue
        found: list[HookLiteral] = []

        def add(
            field: str, flag: str, option: str, match: re.Match[str], span: str, prefix: str
        ) -> None:
            """`option` prints the flag before a value; `prefix` precedes the value in `span`."""
            value = _unquote(match["value"])
            if seg_start + match.start() in quoted or not value or any(c in value for c in "$`\\"):
                return
            variable = _setting_variable(engine, field)
            if field == "model":
                start, end = match.span()
                replacement = (
                    f'$([ "${variable}" = inherit ] || printf -- \'{option}%s\' "${variable}")'
                )
            else:
                start, end = match.span(span)
                replacement = f'{prefix}"${variable}"'
            found.append(
                HookLiteral(engine, field, flag, value, seg_start + start, seg_start + end, replacement)
            )

        for field, pattern in _FLAG_PATTERNS[engine]:
            for match in pattern.finditer(segment, launch.end()):
                flag = match["flag"].strip().rstrip("=")
                add(field, flag, f"{flag} ", match, "value", "")
        if engine == "codex":
            for match in _CODEX_CONFIG.finditer(segment, launch.end()):
                name = match["key"]
                add(_CODEX_CONFIG_FIELDS[name], f"-c {name}", f"-c {name}=", match, "token", f"{name}=")
        literals += sorted(found, key=lambda literal: literal.start)
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


@dataclass(frozen=True)
class ReviewerSettings:
    model: str
    effort: str
    source: str


def _origin_repo(root: Path) -> str | None:
    """owner/name of a GitHub origin remote, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
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
        command = [sys.executable, str(helper), "resolve", "--engine", engine]
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
                f"against ({reason}); run the review-setup skill, or run {MIGRATE_COMMAND} "
                "so the hooks read the pinned settings instead"
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
                f"profile. Run {MIGRATE_COMMAND} to remove it"
            )
        _warn(f"{key} is retired and has no effect; run {MIGRATE_COMMAND} to remove it")


def _check_hook_literals(values: dict[str, str], resolver: _SettingsResolver) -> None:
    """Refuse a review-hook model or effort literal that differs from the resolved settings."""
    for key in REVIEW_HOOK_ENGINES:
        for literal in _hook_literals(key, values.get(key, "")):
            settings = resolver.get(literal.engine)
            expected = settings.model if literal.field == "model" else settings.effort
            if literal.value != expected:
                raise DoctorError(
                    f"{key} passes {literal.flag} {literal.value}, but {settings.source} set "
                    f"{literal.engine} {literal.field} {expected}. Run {MIGRATE_COMMAND} so the "
                    f"hook reads ${literal.variable}"
                )
            _warn(
                f"{key} passes {literal.flag} {literal.value} literally; it matches "
                f"{settings.source}, but a developer whose profile differs is refused. "
                f"Run {MIGRATE_COMMAND} so the hook reads ${literal.variable}"
            )


def doctor(project: Path, *, repo: str | None = None, settings_from_env: bool = False) -> None:
    root = project.resolve()
    skill = root / ".claude/skills/agent-loop"
    config_path = skill / "agent-loop.config"
    prompt_path = skill / "prompt.txt"
    instructions_path = root / "agent-loop-instructions.md"
    ledger = root / ".claude/skills/critique/scripts/review-ledger.js"
    state = skill / "scripts/agent-loop-state.py"
    review_push = skill / "scripts/review-push.sh"
    for path in (config_path, prompt_path, instructions_path, ledger, state, review_push):
        if not path.is_file():
            raise DoctorError(f"required agent-loop file is missing: {path.relative_to(root)}")
    values = _config(config_path)
    _check_retired_keys(values)
    if values.get("review_contract_version") != "3":
        raise DoctorError("review_contract_version must be 3")
    if _version(["node", str(ledger), "--protocol-version"], "review ledger") != "3":
        raise DoctorError("review-ledger protocol is incompatible with contract v3")
    if _version([sys.executable, str(state), "--state-version"], "run state") != "1":
        raise DoctorError("agent-loop state protocol is incompatible")
    if _version([str(review_push), "--protocol-version"], "review push") != "1":
        raise DoctorError("review-push protocol is incompatible")

    prompt = prompt_path.read_text(encoding="utf-8")
    instructions = instructions_path.read_text(encoding="utf-8")
    for token in ("AGENT_LOOP_ISSUE_TITLE", "AGENT_LOOP_ISSUE_BODY"):
        if token not in prompt:
            raise DoctorError(f"worker prompt must read {token}")
    if re.search(r"\bgh\s+(?:api|issue|pr|repo)\b", prompt + "\n" + instructions):
        raise DoctorError("worker prompt or instructions require masked gh")
    if "local commit" not in prompt.lower() or "do not push" not in prompt.lower():
        raise DoctorError("worker prompt must require a local commit and forbid push")
    if "AGENT_LOOP_ISSUE_TITLE" not in instructions or "AGENT_LOOP_ISSUE_BODY" not in instructions:
        raise DoctorError("worker instructions must describe wrapper-provided issue context")
    if "AGENT_LOOP_HANDOFF_FILE" not in prompt + "\n" + instructions:
        # The wrapper recognizes a bail by the handoff file. A worker told to
        # write its handoff anywhere else is reported as producing no commit.
        _warn(
            "worker prompt and instructions do not name AGENT_LOOP_HANDOFF_FILE; a worker bail "
            "that writes its handoff elsewhere is reported as 'produced no local commit'"
        )

    hooks = {
        "codex": values.get("codex_review_hook", ""),
        "claude": values.get("claude_review_hook", ""),
    }
    for engine, hook in hooks.items():
        if not hook:
            raise DoctorError(f"{engine}_review_hook is missing")
        obsolete = (
            "AGENT_LOOP_REVIEW_OUTCOME_FILE",
            "local-review-pass:v1",
            "local-review-complete:v1",
            "local-review-disposition:v1",
            "review-ledger.py",
        )
        if any(token in hook for token in obsolete):
            raise DoctorError(f"{engine}_review_hook contains obsolete review ownership")
        if "AGENT_LOOP_REVIEW_RESULT_FILE" not in hook or "write-result" not in hook:
            raise DoctorError(f"{engine}_review_hook must use helper-owned contract-v3 results")
        if "AGENT_LOOP_REVIEW_PUSH_HELPER" not in hook or re.search(r"\bgit\s+push\b", hook):
            raise DoctorError(f"{engine}_review_hook must use the wrapper-owned review push helper")
        # A missing reviewer CLI used to surface only at that engine's leg,
        # after the issue was claimed and the draft PR opened. Resolve it here,
        # before selection or claim, where the wrapper runs this doctor.
        program = _hook_program(hook)
        if program is not None and shutil.which(program) is None:
            raise DoctorError(
                f"{engine}_review_hook invokes '{program}', which is not installed on PATH; "
                "a run would claim the issue and open its draft PR before failing at this leg"
            )
    if re.search(r"\bcodex\s+exec\b", hooks["codex"]) and not re.search(
        r"<\s*/dev/null", hooks["codex"]
    ):
        # The wrapper closes stdin for every hook itself. The redirect still
        # matters for a hook string that is copied and run by hand: codex exec
        # reads stdin to EOF when it is not a TTY and blocks on an open pipe.
        _warn(
            "codex_review_hook runs codex exec without '</dev/null'; the wrapper closes "
            "stdin, but the same command run by hand with an open stdin blocks until its timeout"
        )
    # The wrapper exports CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 for every hook.
    # A hook that sets it back or unsets it lets a one-shot Claude CLI end its
    # turn with a command still running, which exits 0 without a result.
    for key in ("codex_review_hook", "claude_review_hook", "worker_hook", "setup_hook", "validation_hook"):
        hook = values.get(key, "")
        if re.search(r"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=(?!1(?:\s|;|$))", hook) or re.search(
            r"(?:\bunset\s+|\benv\s+(?:\S+\s+)*-u\s*)CLAUDE_CODE_DISABLE_BACKGROUND_TASKS\b", hook
        ):
            _warn(
                f"{key} overrides CLAUDE_CODE_DISABLE_BACKGROUND_TASKS; the wrapper sets it to 1 so a "
                "Claude CLI cannot end its turn with a background command still running"
            )
    if not re.search(r"(?:^|[ /])deepcritique(?:[ $\"']|$)", hooks["codex"]):
        raise DoctorError("codex_review_hook must invoke deepcritique")
    if not re.search(r"(?:^|[ /])deepcritique(?:[ $\"']|$)", hooks["claude"]):
        raise DoctorError("claude_review_hook must invoke deepcritique")
    _check_hook_literals(values, _SettingsResolver(root, repo, settings_from_env))


def _migrated_line(line: str) -> tuple[str | None, list[str]]:
    """A config line after migration (None when removed) and what changed."""
    match = re.fullmatch(r"(\s*)([a-z_]+)(\s*=\s*)(.*?)(\s*)", line)
    if match is None:
        return line, []
    key, value = match[2], match[4]
    if key in RETIRED_KEYS:
        return None, [f"removed {key}"]
    if key not in REVIEW_HOOK_ENGINES:
        return line, []
    changes = []
    for literal in reversed(_hook_literals(key, value)):
        value = value[: literal.start] + literal.replacement + value[literal.end :]
        changes.insert(0, f"{key}: {literal.flag} {literal.value} -> ${literal.variable}")
    return match[1] + key + match[3] + value + match[5], changes


def migrate(project: Path) -> list[str]:
    """Rewrite review-hook literals to the pinned-settings variables and drop retired keys.

    Every other line, comments included, is kept byte for byte. A second run
    finds nothing to change.
    """
    config_path = project.resolve() / ".claude/skills/agent-loop/agent-loop.config"
    if not config_path.is_file():
        raise DoctorError("required agent-loop file is missing: .claude/skills/agent-loop/agent-loop.config")
    _config(config_path)
    # Decoded without newline translation so CRLF line endings survive.
    original = config_path.read_bytes().decode("utf-8")
    output: list[str] = []
    changes: list[str] = []
    for raw in original.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        ending = raw[len(body) :]
        migrated, line_changes = _migrated_line(body)
        changes += line_changes
        if migrated is not None:
            output.append(migrated + ending)
    if not changes:
        return []
    handle, temporary = tempfile.mkstemp(dir=config_path.parent, prefix=".agent-loop.config.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write("".join(output))
        shutil.copymode(config_path, temporary)
        os.replace(temporary, config_path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument(
        "--repo", help="owner/name for repository overrides; default: the origin remote"
    )
    parser.add_argument(
        "--settings-from-env",
        action="store_true",
        help="check hook literals against the AGENT_LOOP_* settings the wrapper pinned",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="rewrite review-hook model and effort literals to the AGENT_LOOP_* variables "
        "and remove retired keys",
    )
    args = parser.parse_args()
    if args.migrate:
        changes = migrate(Path(args.project_dir))
        for change in changes:
            print(f"agent-loop config doctor: migrated {change}")
        if not changes:
            print("agent-loop config doctor: nothing to migrate")
        return 0
    doctor(Path(args.project_dir), repo=args.repo, settings_from_env=args.settings_from_env)
    print("agent-loop config doctor: compatible")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DoctorError, OSError) as error:
        print(f"agent-loop config doctor: {error}", file=sys.stderr)
        raise SystemExit(1) from error
