#!/usr/bin/env python3
"""Drive a bounded PR review plan. Workers return results; this process advances it.

POSIX, same-user automation, not a sandbox against a malicious reviewer. The
control snapshot is independent of worker commits; resumption checks its hashes.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import tarfile
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

VALIDATION_CONTRACT = ".activeloom-review.json"
VALIDATION_BASE_ENVIRONMENT = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)
VALIDATION_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
VALIDATION_GATE_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?:^|_)(?:AUTH|CREDENTIALS?|KEY|PASSWORD|PASS|SECRETS?|TOKENS?)(?:_|$)",
    re.IGNORECASE,
)
RESERVED_VALIDATION_ENVIRONMENT = re.compile(
    r"(?:ACTIVELOOM|AGENT_LOOP|GITHUB|GIT|SSH)_", re.IGNORECASE
)

LAUNCHERS = {
    "codex": "run-codex-review.py",
    "claude": "run-claude-review.sh",
    "gemini": "run-agy-review.sh",
}
CONTROL_FILES = [
    "review-chain-runner.py",
    "local-review-handoff.py",
    "review-ledger.js",
    "package.json",
    "review-ledger.version",
    "review-ledger.integrity",
    "review-launch-state.py",
    "review-profile.py",
    "review-profile.defaults.json",
    "review-settings.py",
    *LAUNCHERS.values(),
]
# Only this inspected v1 pair supports legacy reconciliation. Git diagnostics
# also need the controller's terminal log to distinguish exit 128 from review
# stderr followed by an interrupted or failed invocation.
LEGACY_PREFLIGHT_HASHES = {
    "review-chain-runner.py": "708b421366df3d04e59dabccbf1e6a9e8358b8f5b7c281f72e39b1d834d370cf",
    "run-agy-review.sh": "114657daa10c196915d351c7ebc4e8df767bf4fa92b3edbe0a577b095667cd5a",
}


class Blocked(RuntimeError):
    pass


class ProcessFailure(Blocked):
    def __init__(self, message: str, exit_status: int):
        super().__init__(message)
        self.exit_status = exit_status


class StartupStalled(Blocked):
    """The worker never emitted its first event within the startup bound."""


class CleanupBlocked(Blocked):
    def __init__(
        self, message: str, group: int, exit_status: int | None, completed: bool
    ):
        super().__init__(message)
        self.group = group
        self.exit_status = exit_status
        self.completed = completed


def capacity_rejected(log: Path) -> bool:
    """Read terminal Codex JSON events, never command output or plain diagnostics."""
    if log.is_symlink() or not log.is_file():
        return False
    rejected = False
    with log.open(errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind in ("error", "turn.failed"):
                error = event.get("error") if kind == "turn.failed" else event
                rejected = isinstance(error, dict) and error.get("message") == (
                    "Selected model is at capacity. Please try a different model."
                )
            else:
                rejected = False
    return rejected


def claude_provider_500(log: Path) -> bool:
    """Recognize Claude's sole provider diagnostic, never reviewer output."""
    if log.is_symlink() or not log.is_file():
        return False
    if log.stat().st_size > 4096:
        return False
    lines = log.read_text(errors="replace").splitlines()
    # bash emits one setlocale warning per LC_* variable it cannot honour.
    while lines and lines[0].startswith("bash: warning: setlocale:"):
        lines = lines[1:]
    return len(lines) == 1 and lines[0].startswith(
        "API Error: 500 Internal server error."
    )


# Codex emits thread.started within about a second of launch; a worker still
# silent after this bound stalled before contacting the model.
CODEX_STARTUP_SECONDS = 180
WAIT_POLL_SECONDS = 5.0


def json_event_seen(log: Path, kind: str) -> bool:
    """Whether the log holds a JSON event line of this type, ignoring plain text."""
    if log.is_symlink() or not log.is_file():
        return False
    with log.open(errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict) and event.get("type") == kind:
                return True
    return False


def codex_startup_stalled(log: Path) -> bool:
    """A Codex worker log that never reached thread.started."""
    return log.is_file() and not log.is_symlink() and not json_event_seen(
        log, "thread.started"
    )


AGY_IDLE = re.compile(
    r"root agent idle; waiting up to \d+s for [1-9]\d* background task\(s\)\s*$"
)
AGY_TERMINATE = re.compile(r"terminating [1-9]\d* background task\(s\) on exit\s*$")


def agy_idle_exit(log: Path) -> bool:
    """Recognize Agy ending its print turn and discarding its own background tasks."""
    if log.is_symlink() or not log.is_file():
        return False
    idle = False
    with log.open(errors="replace") as stream:
        for line in stream:
            if AGY_IDLE.search(line):
                idle = True
            elif idle and AGY_TERMINATE.search(line):
                return True
    return False


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise Blocked(f"expected a regular file: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: Any) -> None:
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(path: Path) -> Any:
    digest(path)
    return json.loads(path.read_text())


def command(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if result.returncode:
        # Raw external errors can contain repository content or credentials.
        raise Blocked(
            f"{Path(argv[0]).name} operation failed (exit {result.returncode})"
        )
    return result.stdout.strip()


def json_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting ambiguous duplicate keys."""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise Blocked(f"duplicate validation contract key: {key}")
        value[key] = item
    return value


def strict_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise Blocked(f"unknown {label} field: {sorted(unknown)[0]}")


def validation_environment_values(value: Any, gate: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise Blocked(f"validation gate {gate} environment must be an object")
    result: dict[str, str] = {}
    for name, item in value.items():
        if (
            not isinstance(name, str)
            or not VALIDATION_ENVIRONMENT_NAME.fullmatch(name)
            or name in VALIDATION_BASE_ENVIRONMENT
            or SENSITIVE_ENVIRONMENT_NAME.search(name)
            or RESERVED_VALIDATION_ENVIRONMENT.match(name)
        ):
            raise Blocked(f"validation gate {gate} has forbidden environment name")
        if not isinstance(item, str) or "\0" in item or "\n" in item or "\r" in item:
            raise Blocked(f"validation gate {gate} environment values must be one line")
        result[name] = item
    return result


def validation_path(value: Any, gate: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(("/", "!"))
        or "\\" in value
        or "\0" in value
        or "\n" in value
        or "\r" in value
        or ".." in Path(value).parts
    ):
        raise Blocked(f"validation gate {gate} has an unsafe path pattern")
    return value


def validation_path_matches(path: str, pattern: str) -> bool:
    """Match repository paths with slash-aware ``*`` and recursive ``**``."""
    expression = ""
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    expression += "(?:.*/)?"
                    index += 1
                else:
                    expression += ".*"
                continue
            expression += "[^/]*"
        elif character == "?":
            expression += "[^/]"
        else:
            expression += re.escape(character)
        index += 1
    return re.fullmatch(expression, path) is not None


def parse_validation_contract(raw: bytes) -> dict[str, Any]:
    if len(raw) > 256 * 1024:
        raise Blocked("validation contract exceeds 256 KiB")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Blocked("validation contract must be valid UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise Blocked("validation contract must be a JSON object")
    strict_keys(document, {"schema_version", "fallback_gate", "gates"}, "contract")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise Blocked("validation contract schema_version must be 1")
    fallback = document.get("fallback_gate")
    gates = document.get("gates")
    if not isinstance(fallback, str) or not VALIDATION_GATE_NAME.fullmatch(fallback):
        raise Blocked("validation contract fallback_gate is invalid")
    if not isinstance(gates, dict) or not gates:
        raise Blocked("validation contract gates must be a nonempty object")
    parsed: dict[str, Any] = {}
    for name, gate in gates.items():
        if not isinstance(name, str) or not VALIDATION_GATE_NAME.fullmatch(name):
            raise Blocked("validation contract gate name is invalid")
        if not isinstance(gate, dict):
            raise Blocked(f"validation gate {name} must be an object")
        strict_keys(
            gate, {"paths", "always", "commands", "environment"}, f"gate {name}"
        )
        paths = gate.get("paths", [])
        always = gate.get("always", False)
        commands = gate.get("commands")
        if not isinstance(paths, list) or any(
            not isinstance(path, str) for path in paths
        ):
            raise Blocked(f"validation gate {name} paths must be a list")
        if type(always) is not bool:
            raise Blocked(f"validation gate {name} always must be boolean")
        if name != fallback and (bool(paths) == always):
            raise Blocked(
                f"validation gate {name} must declare paths or always true, not both"
            )
        if name == fallback and (paths or always):
            raise Blocked("the fallback validation gate must not declare paths or always")
        if not isinstance(commands, list) or not commands:
            raise Blocked(f"validation gate {name} commands must be nonempty")
        parsed_commands: list[list[str]] = []
        for item in commands:
            if (
                not isinstance(item, dict)
                or set(item) != {"argv"}
                or not isinstance(item["argv"], list)
                or not item["argv"]
                or any(
                    not isinstance(argument, str)
                    or not argument
                    or "\0" in argument
                    or "\n" in argument
                    or "\r" in argument
                    for argument in item["argv"]
                )
            ):
                raise Blocked(
                    f"validation gate {name} commands require nonempty argv arrays"
                )
            parsed_commands.append(item["argv"])
        parsed[name] = {
            "paths": [validation_path(path, name) for path in paths],
            "always": always,
            "commands": parsed_commands,
            "environment": validation_environment_values(
                gate.get("environment", {}), name
            ),
        }
    if fallback not in parsed:
        raise Blocked("validation contract fallback_gate does not exist")
    return {
        "schema_version": 1,
        "fallback_gate": fallback,
        "gates": parsed,
    }


def validate_contract_command() -> int:
    root = Path(command(["git", "rev-parse", "--show-toplevel"])).resolve()
    if Path.cwd().resolve() != root:
        raise Blocked("validate the contract from the repository worktree root")
    path = Path(VALIDATION_CONTRACT)
    checksum = digest(path)
    contract = parse_validation_contract(path.read_bytes())
    print(
        json.dumps(
            {
                "status": "valid",
                "path": VALIDATION_CONTRACT,
                "schema_version": contract["schema_version"],
                "gates": list(contract["gates"]),
                "sha256": checksum,
            }
        )
    )
    return 0


def managed(
    argv: list[str],
    log: Path,
    env: dict[str, str],
    timeout: float = 3600,
    startup_event: str | None = None,
    startup_seconds: float = CODEX_STARTUP_SECONDS,
) -> None:
    """Keep the PID through cancellation, forward TERM, then kill the group.

    With ``startup_event``, a worker whose log has no such JSON event after
    ``startup_seconds`` is stopped as stalled instead of waiting out the timeout.
    """

    def interrupted(signum: int, frame: Any) -> None:
        raise Blocked(f"interrupted by signal {signum}")

    handlers = {
        s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        with log.open("ab") as output:
            os.chmod(log, 0o600)
            # Never inherit the runner's stdin: CLIs such as `codex exec`
            # read a non-TTY stdin to EOF before starting, so an open pipe
            # from the caller hangs the worker indefinitely.
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                env=env,
                start_new_session=True,
            )
            pending: BaseException | None = None

            def signal_group(sig: int) -> None:
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
                except PermissionError as error:
                    # A denied signal is not evidence of surviving descendants.
                    # Only ESRCH from a fresh, non-signalling probe proves the
                    # group is gone; EPERM on that probe still fails closed.
                    try:
                        os.killpg(child.pid, 0)
                    except ProcessLookupError:
                        return
                    except PermissionError:
                        pass
                    exit_state = (
                        f"exited {child.returncode}"
                        if child.returncode is not None
                        else "exit not confirmed"
                    )
                    # This raise replaces any in-flight failure, so carry its
                    # cause. TimeoutExpired's text includes argv; keep it out.
                    if isinstance(pending, subprocess.TimeoutExpired):
                        cause = f"; worker timed out after {pending.timeout:g}s"
                    elif isinstance(pending, Blocked):
                        cause = f"; worker failure: {pending}"
                    elif pending is not None:
                        cause = f"; worker failure: {type(pending).__name__}"
                    else:
                        cause = ""
                    raise CleanupBlocked(
                        f"{Path(argv[0]).name} {exit_state}; process-group cleanup "
                        f"denied for {child.pid}; reconcile surviving processes "
                        f"before resuming{cause}",
                        child.pid,
                        child.returncode,
                        pending is None and child.returncode == 0,
                    ) from error

            try:
                started = time.monotonic()
                watch = startup_event
                while True:
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    wait = min(WAIT_POLL_SECONDS, timeout - elapsed)
                    if watch:
                        wait = min(wait, max(startup_seconds - elapsed, 0.01))
                    try:
                        code = child.wait(timeout=wait)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if watch and time.monotonic() - started >= startup_seconds:
                        if not json_event_seen(log, watch):
                            raise StartupStalled(
                                f"{Path(argv[0]).name} emitted no {watch} event "
                                f"within {startup_seconds:g}s; stopped as stalled"
                            )
                        watch = None
                if code:
                    raise ProcessFailure(
                        f"{Path(argv[0]).name} exited {code}; inspect {log}", code
                    )
            except BaseException as failure:
                pending = failure
                raise
            finally:
                # Also clean up descendants left behind after the leader exits.
                signal_group(signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                signal_group(signal.SIGKILL)
                child.wait()
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


class Runner:
    def __init__(self, args: argparse.Namespace, directory: Path):
        self.args, self.directory = args, directory
        self.checkpoint = directory / "state.json"
        self.control = directory / "control"
        self.state: dict[str, Any] = (
            read(self.checkpoint) if self.checkpoint.exists() else {}
        )
        self.control = directory / self.state.get("control_directory", "control")
        self.installation_repaired = False
        self._settings: ModuleType | None = None

    def persist(self) -> None:
        save(self.checkpoint, self.state)

    def verify_control(self) -> None:
        for name, expected in self.state.get("control_hashes", {}).items():
            if digest(self.control / name) != expected:
                raise Blocked(
                    "pinned controller or launcher changed; reconcile, do not relaunch"
                )

    def helper(self, name: str, *args: str) -> dict[str, Any]:
        self.verify_control()
        executable = (
            ["node", str(self.control / "review-ledger.js")]
            if name == "ledger"
            else [sys.executable, "-I", str(self.control / "local-review-handoff.py")]
        )
        value = json.loads(command([*executable, *args]))
        if not isinstance(value, dict):
            raise Blocked("helper did not return an object")
        return value

    def scope(self, head: str) -> list[str]:
        return ["--repo", self.args.repo, "--pr", str(self.args.pr), "--head", head]

    def boundary(self) -> str:
        if command(["git", "status", "--porcelain"]):
            raise Blocked("review worktree is dirty; preserve and reconcile it")
        head = command(["git", "rev-parse", "HEAD"])
        pr = json.loads(
            command(
                [
                    "gh",
                    "pr",
                    "view",
                    str(self.args.pr),
                    "--repo",
                    self.args.repo,
                    "--json",
                    "headRefOid,headRefName,headRepository,author,state,isDraft",
                ]
            )
        )
        actor = command(["gh", "api", "user", "--jq", ".login"])
        if (
            pr["state"] != "OPEN"
            or not pr["isDraft"]
            or pr["headRepository"]["nameWithOwner"] != self.args.repo
            or pr["author"]["login"] != actor
            or command(
                [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner",
                    "--jq",
                    ".nameWithOwner",
                ]
            )
            != self.args.repo
        ):
            raise Blocked("requires a self-authored, same-repository open draft PR")
        remote = command(
            [
                "git",
                "ls-remote",
                "--exit-code",
                "origin",
                "refs/heads/" + pr["headRefName"],
            ]
        ).split()[0]
        if not head == remote == pr["headRefOid"]:
            raise Blocked("local, remote, and PR heads disagree")
        if self.state and actor != self.state["actor"]:
            raise Blocked("authenticated actor changed")
        return head

    def repository_root(self) -> Path:
        return Path(command(["git", "rev-parse", "--show-toplevel"])).resolve()

    def target_revision(self) -> str:
        return command(
            [
                "gh",
                "pr",
                "view",
                str(self.args.pr),
                "--repo",
                self.args.repo,
                "--json",
                "baseRefOid",
                "--jq",
                ".baseRefOid",
            ]
        )

    def merge_base(self, target: str, head: str) -> str:
        return command(["git", "merge-base", target, head])

    def threads(self, path: Path) -> list[int]:
        owner, name = self.args.repo.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$endCursor:String){
          repository(owner:$owner,name:$name){pullRequest(number:$number){
            reviewThreads(first:100,after:$endCursor){
              nodes{id isResolved repository{nameWithOwner} pullRequest{number}
                comments(first:100){nodes{databaseId body author{login}} pageInfo{hasNextPage}}}
              pageInfo{hasNextPage endCursor}}}}}"""
        pages = json.loads(
            command(
                [
                    "gh",
                    "api",
                    "graphql",
                    "--paginate",
                    "--slurp",
                    "-f",
                    "query=" + query,
                    "-f",
                    "owner=" + owner,
                    "-f",
                    "name=" + name,
                    "-F",
                    f"number={self.args.pr}",
                ]
            )
        )
        ids: list[int] = []
        for page in pages:
            for thread in page["data"]["repository"]["pullRequest"]["reviewThreads"][
                "nodes"
            ]:
                if thread["comments"]["pageInfo"]["hasNextPage"]:
                    raise Blocked(
                        "thread exceeds export limit; reconcile with a complete export"
                    )
                ids.extend(c["databaseId"] for c in thread["comments"]["nodes"])
        save(path, pages)
        return ids

    def comments(self, path: Path) -> None:
        pages = json.loads(
            command(
                [
                    "gh",
                    "api",
                    f"repos/{self.args.repo}/issues/{self.args.pr}/comments",
                    "--paginate",
                    "--slurp",
                ]
            )
        )
        save(
            path,
            [
                {"id": c["id"], "body": c["body"], "author": c["user"]["login"]}
                for page in pages
                for c in page
                if not c["body"].lstrip().startswith("<!-- local-review-telemetry:")
            ],
        )

    def dco(self, head: str) -> None:
        if not self.state["config"]["require_dco"]:
            return
        for sha in command(
            ["git", "rev-list", "--no-merges", f"{self.state['base']}..{head}"]
        ).splitlines():
            if not re.search(
                r"^Signed-off-by: .+ <.+@.+>$",
                command(["git", "show", "-s", "--format=%B", sha]),
                re.M,
            ):
                raise Blocked(
                    f"DCO sign-off missing on {sha}; no history rewrite is authorized"
                )

    def validation_contract(self, revision: str) -> dict[str, Any] | None:
        """Load the consumer contract from the trusted target revision."""
        entry = subprocess.run(
            [
                "git",
                "ls-tree",
                "-z",
                "--full-tree",
                revision,
                "--",
                VALIDATION_CONTRACT,
            ],
            capture_output=True,
            timeout=120,
        )
        if entry.returncode:
            raise Blocked("git operation failed while locating validation contract")
        if not entry.stdout:
            return None
        try:
            metadata, path = entry.stdout.removesuffix(b"\0").split(b"\t", 1)
            mode, kind, object_id = metadata.split(b" ", 2)
        except ValueError as error:
            raise Blocked("git returned malformed validation contract metadata") from error
        if (
            path != VALIDATION_CONTRACT.encode()
            or kind != b"blob"
            or not mode.startswith(b"100")
            or not re.fullmatch(rb"[0-9a-f]{40,64}", object_id)
        ):
            raise Blocked("validation contract must be a regular file in the base")
        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id.decode()],
            capture_output=True,
            timeout=120,
        )
        if blob.returncode:
            raise Blocked("git operation failed while reading validation contract")
        parsed = parse_validation_contract(blob.stdout)
        return {
            "mode": "contract-v1",
            "path": VALIDATION_CONTRACT,
            "policy_revision": revision,
            "manifest_sha256": hashlib.sha256(blob.stdout).hexdigest(),
            "contract": parsed,
            "base_environment": {
                name: os.environ[name]
                for name in VALIDATION_BASE_ENVIRONMENT
                if name in os.environ
            },
        }

    def changed_paths(self, base: str, head: str) -> list[str]:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                f"{base}...{head}",
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise Blocked("git operation failed while resolving validation gates")
        try:
            return [
                item.decode("utf-8")
                for item in result.stdout.split(b"\0")
                if item
            ]
        except UnicodeDecodeError as error:
            raise Blocked("validation gate paths must be UTF-8") from error

    def resolved_validation(self, head: str) -> dict[str, Any]:
        validation = self.state["config"].get("validation")
        if not validation:
            return {
                "mode": "legacy",
                "gates": [],
                "commands": [
                    {
                        "argv": shlex.split(check),
                        "environment": dict(os.environ),
                    }
                    for check in self.state["config"]["checks"]
                ],
            }
        contract = validation["contract"]
        fallback = contract["fallback_gate"]
        paths = self.changed_paths(self.state["base"], head)
        selected: list[str] = []
        unmatched = not paths
        for name, gate in contract["gates"].items():
            if name == fallback:
                continue
            if gate["always"]:
                selected.append(name)
                continue
            if any(
                validation_path_matches(path, pattern)
                for path in paths
                for pattern in gate["paths"]
            ):
                selected.append(name)
        for path in paths:
            if not any(
                validation_path_matches(path, pattern)
                for name, gate in contract["gates"].items()
                if name != fallback and not gate["always"]
                for pattern in gate["paths"]
            ):
                unmatched = True
                break
        if unmatched:
            selected.append(fallback)
        commands: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for name in selected:
            gate = contract["gates"][name]
            environment = {
                **validation["base_environment"],
                **gate["environment"],
            }
            for argv in gate["commands"]:
                identity = json_digest([argv, environment])
                if identity in seen:
                    commands[seen[identity]]["gates"].append(name)
                    continue
                seen[identity] = len(commands)
                commands.append(
                    {
                        "argv": argv,
                        "environment": environment,
                        "gates": [name],
                    }
                )
        return {
            "mode": validation["mode"],
            "gates": selected,
            "commands": commands,
            "manifest_sha256": validation["manifest_sha256"],
            "changed_paths_sha256": json_digest(paths),
            "environment_sha256": json_digest(
                [command["environment"] for command in commands]
            ),
        }

    def initialize(self) -> None:
        if Path.cwd().resolve() != self.repository_root():
            raise Blocked("run the review chain from the repository worktree root")
        head = self.boundary()
        base = command(["git", "rev-parse", "--verify", self.args.base + "^{commit}"])
        legacy_checkpoint = bool(
            self.state
            and "validation_policy_revision" not in self.state["config"]
        )
        if legacy_checkpoint:
            policy_revision = None
            validation = None
        elif self.state:
            policy_revision = self.state["config"]["validation_policy_revision"]
            validation = self.validation_contract(policy_revision)
        else:
            policy_revision = self.target_revision()
            if self.merge_base(policy_revision, head) != base:
                raise Blocked("--base must equal the pull request merge base")
            validation = self.validation_contract(policy_revision)
        checks = self.args.check or []
        if not legacy_checkpoint and validation and checks:
            raise Blocked(
                f"{VALIDATION_CONTRACT} exists in the pinned target policy; remove --check"
            )
        if not legacy_checkpoint and not validation and not checks:
            raise Blocked(
                f"no {VALIDATION_CONTRACT} exists in the pinned target policy; pass --check"
            )
        config: dict[str, Any] = {
            "repo": self.args.repo,
            "pr": self.args.pr,
            "worktree": str(Path.cwd()),
            "tier": self.args.tier,
            "author": self.args.author,
            "trigger": self.args.trigger,
            "mode": "chain" if self.args.chain else "cycle",
            "plan": self.args.chain or self.args.cycle,
            "checks": checks,
            "base_argument": self.args.base,
            "require_dco": self.args.require_dco
            or Path(".github/workflows/dco.yml").is_file(),
        }
        if validation:
            config["validation"] = validation
        if policy_revision is not None:
            config["validation_policy_revision"] = policy_revision
        # Added only when set, so a checkpoint written before the flag existed
        # still resumes with the same arguments.
        scope_decision = getattr(self.args, "scope_decision", None)
        if scope_decision:
            config["scope_decision"] = scope_decision
        if self.state:
            if self.state.get("version") not in (1, 2):
                raise Blocked("unsupported checkpoint version")
            adopt_scope = bool(
                self.args.resume
                and self.state.get("run_id") is None
                and scope_decision
                and "scope_decision" not in self.state["config"]
            )
            # Compare in memory: a resume this method goes on to reject must
            # not leave an adopted decision behind in the checkpoint.
            recorded = dict(self.state["config"])
            if adopt_scope:
                recorded["scope_decision"] = scope_decision
            if not self.args.resume or recorded != config:
                raise Blocked(
                    "checkpoint exists; use --resume with the same plan, tier, and gates"
                )
            self.verify_control()
            migration = getattr(self.args, "migrate_controller", None)
            if migration:
                self.migrate(migration)
            if (
                digest(Path(__file__))
                != self.state["control_hashes"]["review-chain-runner.py"]
            ):
                raise Blocked(
                    "runner version changed; deliberate checkpoint migration required: "
                    "from the original review worktree, run a clean checkout's runner "
                    "with the original arguments plus --resume --migrate-controller "
                    "<that checkout's HEAD sha>"
                )
            if adopt_scope:
                self.state["config"]["scope_decision"] = scope_decision
                self.persist()
            return
        if self.args.resume:
            raise Blocked("no checkpoint to resume")
        # No checkpoint means no worker was launched. Rebuild a partial
        # snapshot left by failed initialization, without following symlinks.
        if self.control.is_symlink():
            raise Blocked("control directory cannot be a symlink")
        self.control.mkdir(mode=0o700, exist_ok=True)
        source = Path(__file__).resolve().parent
        for name in CONTROL_FILES:
            digest(source / name)
            if (self.control / name).is_symlink():
                raise Blocked("control file cannot be a symlink")
            shutil.copyfile(source / name, self.control / name)
        auth = Path(self.args.authorization_file).read_text().strip()
        if not auth or "<!-- local-review-" in auth:
            raise Blocked(
                "authorization must be nonempty public-safe text, without markers"
            )
        (self.directory / "authorization.txt").write_text(auth)
        os.chmod(self.directory / "authorization.txt", 0o600)
        self.state = {
            "version": 2,
            "config": config,
            "base": base,
            "head": head,
            "start_head": head,
            "actor": command(["gh", "api", "user", "--jq", ".login"]),
            "control_hashes": {n: digest(self.control / n) for n in CONTROL_FILES},
            "run_id": None,
            "pending": None,
            "completed": [],
            "attempts": [],
            "installation_revision": head,
            "status": "prepared",
        }
        self.persist()

    def migrate(self, revision: str) -> None:
        """Explicitly adopt committed controller bytes, retaining every old file."""
        if self.state["version"] != 1:
            if self.state.get("migrations", [{}])[-1].get("revision") == revision:
                return
            raise Blocked("only version 1 checkpoints support this migration")
        source = Path(__file__).resolve().parent
        root = command(["git", "-C", str(source), "rev-parse", "--show-toplevel"])
        if (
            not re.fullmatch(r"[0-9a-f]{40}", revision)
            or command(["git", "-C", root, "rev-parse", "HEAD"]) != revision
            or command(["git", "-C", root, "status", "--porcelain"])
        ):
            raise Blocked(
                "migration requires a clean checkout at the explicitly verified controller commit"
            )
        if self.boundary() != self.state["head"]:
            raise Blocked("reconcile the pending review head before migration")
        backup = self.directory / "state-v1.json"
        if not backup.exists():
            save(backup, self.state)
        elif read(backup) != self.state:
            raise Blocked("existing migration snapshot differs; reconcile it")
        replacement = self.directory / ("control-" + revision)
        if replacement.is_symlink():
            raise Blocked("migration control directory cannot be a symlink")
        replacement.mkdir(mode=0o700, exist_ok=True)
        for name in CONTROL_FILES:
            expected = digest(source / name)
            target = replacement / name
            if target.exists() or target.is_symlink():
                if digest(target) != expected:
                    raise Blocked("partial migration snapshot changed")
            else:
                shutil.copyfile(source / name, target)
        migration = {
            "revision": revision,
            "prior_state": backup.name,
            "prior_state_sha256": digest(backup),
            "prior_control": self.control.name,
            "prior_control_hashes": self.state["control_hashes"],
        }
        self.control = replacement
        self.state.update(
            version=2,
            control_directory=replacement.name,
            control_hashes={name: digest(replacement / name) for name in CONTROL_FILES},
            migrations=[migration],
            attempts=[],
            installation_revision=self.state["head"],
        )
        self.persist()

    def prepare_installation(self) -> None:
        """Own prompt bytes independently of review edits and global skill links."""
        directory = self.directory / "installation"
        if (
            getattr(self.args, "repair_installation", False)
            and not self.installation_repaired
        ):
            if directory.is_symlink():
                raise Blocked("review installation cannot be a symlink")
            if directory.exists():
                preserved = self.directory / (
                    "installation-preserved-" + uuid.uuid4().hex
                )
                directory.rename(preserved)
                self.state.setdefault("installation_history", []).append(
                    {
                        "directory": preserved.name,
                        "installation": self.state.get("installation"),
                    }
                )
            self.state.pop("installation", None)
            self.persist()
            self.installation_repaired = True
        if "installation" in self.state:
            return
        if directory.is_symlink():
            raise Blocked("review installation cannot be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        native = directory / "native"
        if native.exists():
            raise Blocked(
                f"partial review installation requires inspection: {native}; "
                "--resume --repair-installation preserves it and rebuilds at the original pin"
            )
        native.mkdir(mode=0o700)
        revision = self.state["installation_revision"]
        roots = [
            ".codex" if e == "codex" else ".claude"
            for e in self.engines()
            if e != "gemini"
        ]
        files: dict[str, str] = {}
        if roots:
            try:
                archive = subprocess.check_output(
                    ["git", "archive", revision, *roots],
                    timeout=120,
                    stderr=subprocess.PIPE,
                )
            except subprocess.SubprocessError as error:
                raise Blocked(
                    f"cannot archive selected review harnesses {', '.join(roots)} at {revision}; "
                    "verify the installed harnesses, then use --resume --repair-installation"
                ) from error
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                for member in tar:
                    relative = Path(member.name)
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or relative.parts[0] not in roots
                    ):
                        raise Blocked("unsafe path in review installation")
                    target = native / relative
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        data = tar.extractfile(member)
                        assert data
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data.read())
                        target.chmod(member.mode & 0o755)
                        files[member.name] = digest(target)
                    else:
                        raise Blocked(
                            f"review installation requires regular files: {member.name}"
                        )
        manifest = directory / "manifest.json"
        for root in roots:
            required = [
                "REVIEW_WORKFLOW.md",
                "references/local-review-ledger.md",
                "skills/critique/scripts/review-ledger.js",
                "skills/critique/scripts/review-ledger.version",
                "skills/critique/scripts/review-ledger.integrity",
                "skills/critique/scripts/package.json",
                *[
                    f"skills/{skill}/SKILL.md"
                    for skill in ("deepcritique", "critique", "refactorpass")
                ],
            ]
            for name in required:
                if f"{root}/{name}" not in files:
                    raise Blocked(f"review installation is incomplete: {root}/{name}")
        save(manifest, {"revision": revision, "files": files})
        self.state["installation"] = {
            "revision": revision,
            "manifest_sha256": digest(manifest),
        }
        self.persist()

    def engines(self) -> list[str]:
        return list(
            dict.fromkeys(
                "gemini" if e.strip() == "antigravity" else e.strip()
                for e in self.state["config"]["plan"].split(",")
            )
        )

    def environment(self, engine: str) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("AGENT_LOOP_", "ACTIVELOOM_"))
        }
        for key in ("CLAUDE_REVIEW_CLI", "AGY_REVIEW_CLI", "CODEX_REVIEW_CLI"):
            env.pop(key, None)
        installation = self.directory / "installation"
        env.update(
            ACTIVELOOM_INSTALLATION_MANIFEST=str(installation / "manifest.json"),
            ACTIVELOOM_INSTALLATION_SHA256=self.state["installation"][
                "manifest_sha256"
            ],
            GH_REPO=self.args.repo,
        )
        settings = self.selected_settings(engine)
        env.update(
            ACTIVELOOM_REVIEW_MODEL=settings["model"],
            ACTIVELOOM_REVIEW_EFFORT=settings["effort"],
        )
        if engine == "gemini":
            checkout = installation / "agy"
            match = re.search(
                r'^agy_surface_sha="([0-9a-f]{40})"$',
                (self.control / LAUNCHERS[engine]).read_text(),
                re.M,
            )
            if not match:
                raise Blocked("Agy launcher lacks a trusted surface pin")
            if not checkout.exists():
                command(["git", "init", str(checkout)])
                command(
                    [
                        "git",
                        "-C",
                        str(checkout),
                        "remote",
                        "add",
                        "origin",
                        "https://github.com/loomantix/activeloom.git",
                    ]
                )
                command(
                    [
                        "git",
                        "-C",
                        str(checkout),
                        "fetch",
                        "--depth=1",
                        "origin",
                        match[1],
                    ]
                )
                command(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-C",
                        str(checkout),
                        "checkout",
                        "--detach",
                        match[1],
                    ]
                )
            if checkout.is_symlink():
                raise Blocked("Agy installation cannot be a symlink")
            env["ACTIVELOOM_REVIEW_SURFACE"] = str(checkout / ".agents")
        else:
            env["ACTIVELOOM_REVIEW_SURFACE"] = str(
                installation / "native" / (".codex" if engine == "codex" else ".claude")
            )
        return env

    def settings_helper(self) -> ModuleType:
        """Load the settings helper, from the verified control snapshot once pinned."""
        if self._settings is None:
            name = "review-settings.py"
            expected = self.state.get("control_hashes", {}).get(name)
            path = (self.control if expected else Path(__file__).resolve().parent) / name
            digest(path)
            source = path.read_bytes()
            if expected and hashlib.sha256(source).hexdigest() != expected:
                raise Blocked(
                    "pinned controller or launcher changed; reconcile, do not relaunch"
                )
            module = ModuleType("review_settings")
            module.__file__ = str(path)
            exec(compile(source, str(path), "exec"), module.__dict__)
            self._settings = module
        return self._settings

    def settings_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        helper = self.settings_helper()
        try:
            return getattr(helper, name)(*args, **kwargs)
        except helper.SettingsError as error:
            raise Blocked(str(error)) from error

    def review_settings(self, engine: str) -> dict[str, Any]:
        """Pin an engine's profile settings once; profile edits apply to the next run."""
        helper = self.settings_helper()
        settings, created = self.settings_call(
            "pin",
            self.state,
            engine,
            "reviewer",
            lambda: helper.resolve(
                engine, "reviewer", self.args.repo, self.control / "review-profile.py"
            ),
        )
        if created:
            self.persist()
        return dict(settings)

    def selected_settings(self, engine: str) -> dict[str, Any]:
        self.review_settings(engine)
        return dict(self.settings_call("selected", self.state, engine, "reviewer"))

    def launcher_command(self, engine: str, head: str, number: int) -> list[str]:
        return [
            sys.executable if engine == "codex" else "bash",
            str(self.control / LAUNCHERS[engine]),
            *self.scope(head),
            "--base",
            self.state["base"],
            "--round",
            str(number),
        ]

    def preflight(self) -> None:
        self.verify_control()
        self.prepare_installation()
        head = self.boundary()
        failures = []
        for engine in self.engines():
            try:
                managed(
                    [*self.launcher_command(engine, head, 1), "--preflight-only"],
                    self.directory / f"preflight-{engine}.log",
                    self.environment(engine),
                    180,
                )
            except (Blocked, OSError, subprocess.SubprocessError) as error:
                failures.append(f"{engine}: {error}")
        if failures:
            raise Blocked(
                "selected-engine preflight failed; "
                + "; ".join(failures)
                + "; --resume --repair-installation preserves and replaces damaged installations"
            )

    def launch(self, pending: dict[str, Any]) -> None:
        self.verify_control()
        if self.boundary() != pending["before"]:
            raise Blocked("head changed before reviewer launch")
        decision = self.decision(pending["before"])
        if (
            decision.get("passes") != self.state["completed"]
            or decision.get("status") != "next"
            or (decision.get("engine"), decision.get("round"))
            != (pending["engine"], pending["round"])
        ):
            raise Blocked(
                "ledger changed before launch; reconcile the owed pass and budget"
            )
        folder = self.directory / pending["folder"]
        if digest(folder / "historical.json") != pending["historical_sha256"]:
            raise Blocked("pre-pass comment snapshot changed before launch")
        # Build the environment first: a failure here launched nothing and must
        # leave the owed pass resumable, not an unknown "launching" attempt.
        env = self.environment(pending["engine"])
        if origin := pending.get("capacity_origin"):
            attempts = [
                item for item in self.state["attempts"]
                if item["attempt_id"] == origin
            ]
            if len(attempts) != 1:
                raise Blocked("capacity fallback origin changed")
            self.verify_retry_evidence(pending, attempts[0], "capacity")
        if origin := pending.get("idle_exit_origin"):
            attempts = [
                item for item in self.state["attempts"]
                if item["attempt_id"] == origin
            ]
            if len(attempts) != 1:
                raise Blocked("idle-exit retry origin changed")
            self.verify_retry_evidence(pending, attempts[0], "idle_exit")
        if origin := pending.get("startup_stall_origin"):
            attempts = [
                item for item in self.state["attempts"]
                if item["attempt_id"] == origin
            ]
            if len(attempts) != 1:
                raise Blocked("startup-stall retry origin changed")
            self.verify_retry_evidence(pending, attempts[0], "startup_stall")
        if origin := pending.get("provider_500_origin"):
            attempts = [
                item for item in self.state["attempts"]
                if item["attempt_id"] == origin
            ]
            if len(attempts) != 1:
                raise Blocked("provider-error retry origin changed")
            self.verify_retry_evidence(pending, attempts[0], "provider_500")
        attempt = {
            "attempt_id": uuid.uuid4().hex,
            "engine": pending["engine"],
            "round": pending["round"],
            "folder": pending["folder"],
            "review_started": None,
            "exit_status": None,
            "failure_reason": None,
            "phase": "launching",
            "settings": {
                "model": env.get("ACTIVELOOM_REVIEW_MODEL"),
                "effort": env.get("ACTIVELOOM_REVIEW_EFFORT"),
            },
        }
        self.state.setdefault("attempts", []).append(attempt)
        pending.update(phase="launching", attempt_id=attempt["attempt_id"])
        self.persist()
        env.update(
            ACTIVELOOM_RUN_ID=self.state["run_id"],
            ACTIVELOOM_LAUNCH_STATE=str(folder / "launch.json"),
            ACTIVELOOM_ATTEMPT_ID=attempt["attempt_id"],
            AGENT_LOOP_REVIEW_RESULT_FILE=str(folder / "result.json"),
            AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE=str(
                folder / "historical.json"
            ),
            AGENT_LOOP_PR_NUMBER=str(self.args.pr),
            AGENT_LOOP_PR_HEAD_SHA=pending["before"],
            AGENT_LOOP_REVIEW_BASE_SHA=self.state["base"],
            AGENT_LOOP_REVIEW_ROUND=str(pending["round"]),
            AGENT_LOOP_REVIEW_ENGINE=pending["engine"],
            AGENT_LOOP_LOG_DIR=str(folder),
        )
        error: BaseException | None = None
        try:
            print(
                f"Starting {pending['engine']} pass {pending['round']} at {pending['before']}",
                flush=True,
            )
            managed(
                self.launcher_command(
                    pending["engine"], pending["before"], pending["round"]
                ),
                folder / "worker.log",
                env,
                3660,
                startup_event=(
                    "thread.started" if pending["engine"] == "codex" else None
                ),
            )
            attempt.update(exit_status=0, review_started=True, phase="returned")
            pending["phase"] = "returned"
            # Bind completed-result recovery to the observed worker return.
            # A sidecar introduced later, or an unknown exit, is not proof of a
            # completed pass and must not authorize automatic finalization.
            recovery = folder / "result.json.recovery.json"
            pending["result_recovery_sha256"] = (
                digest(recovery) if recovery.exists() else None
            )
            if pending["engine"] == "gemini":
                self.classify_idle_exit(pending, attempt, folder)
        except (
            Blocked,
            OSError,
            subprocess.SubprocessError,
            KeyboardInterrupt,
        ) as caught:
            error = caught
            attempt["exit_status"] = getattr(caught, "exit_status", None)
            attempt["failure_reason"] = "execution_failed_or_unknown"
            marker = folder / "launch.json"
            if marker.is_file():
                evidence = read(marker)
                if (
                    evidence.get("attempt_id") == attempt["attempt_id"]
                    and evidence.get("version") == 1
                ):
                    attempt["launch_sha256"] = digest(marker)
                    # Only ProcessFailure proves the launcher exited and its
                    # process-group cleanup completed. A preflight marker can
                    # belong to a stalled launcher whose cleanup was denied.
                    if (
                        isinstance(caught, ProcessFailure)
                        and caught.exit_status > 0
                        and evidence.get("phase") == "preflight"
                        and evidence.get("review_started") is False
                    ):
                        attempt.update(
                            review_started=False,
                            phase="preflight_failed",
                            failure_reason=evidence.get("failure_reason"),
                        )
                        pending["phase"] = "preflight_failed"
                    elif evidence.get("phase") == "execution":
                        attempt["phase"] = pending["phase"] = "execution_failed"
                        if (
                            pending["engine"] == "codex"
                            and isinstance(caught, ProcessFailure)
                            and caught.exit_status == 1
                            and capacity_rejected(folder / "worker.log")
                        ):
                            attempt.update(
                                review_started=True,
                                phase="capacity_failed",
                                failure_reason="model_capacity",
                                log_sha256=digest(folder / "worker.log"),
                            )
                            pending["phase"] = "capacity_failed"
                        elif (
                            pending["engine"] == "codex"
                            and isinstance(caught, StartupStalled)
                            and codex_startup_stalled(folder / "worker.log")
                        ):
                            # Cleanup completed (a denial raises CleanupBlocked
                            # instead), and nothing precedes thread.started that
                            # could post, commit or push.
                            attempt.update(
                                review_started=True,
                                phase="startup_stall_failed",
                                failure_reason="codex_startup_stall",
                                log_sha256=digest(folder / "worker.log"),
                            )
                            pending["phase"] = "startup_stall_failed"
                        elif (
                            pending["engine"] == "claude"
                            and isinstance(caught, ProcessFailure)
                            and caught.exit_status == 1
                            and claude_provider_500(folder / "worker.log")
                        ):
                            attempt.update(
                                review_started=True,
                                phase="provider_500_failed",
                                failure_reason="claude_provider_500",
                                log_sha256=digest(folder / "worker.log"),
                            )
                            pending["phase"] = "provider_500_failed"
            if isinstance(caught, CleanupBlocked):
                attempt.update(
                    failure_reason="cleanup_denied",
                    process_group=caught.group,
                )
                # Seal the completed output before allowing a later resume
                # to separate a successful exit from unfinished cleanup.
                # Digest everything first: a half-sealed attempt would disagree
                # with its pending phase, and raising here would replace the
                # cleanup denial the operator has to act on.
                seal = self.seal_completed(folder) if caught.completed else None
                if seal is not None:
                    attempt.update(review_started=True, phase="cleanup_blocked")
                    pending.update(phase="cleanup_blocked", **seal)
        finally:
            self.persist()
        if error and pending["phase"] not in (
            "capacity_failed", "startup_stall_failed", "provider_500_failed"
        ):
            raise error

    def seal_completed(self, folder: Path) -> dict[str, Any] | None:
        """Digest a completed worker's output, or None when it cannot be sealed."""
        recovery = folder / "result.json.recovery.json"
        try:
            return {
                "cleanup_result_sha256": digest(folder / "result.json"),
                "cleanup_log_sha256": digest(folder / "worker.log"),
                "result_recovery_sha256": (
                    digest(recovery) if recovery.exists() else None
                ),
            }
        except (Blocked, OSError):
            return None

    def recover_cleanup(self, pending: dict[str, Any]) -> None:
        attempts = [
            item for item in self.state["attempts"]
            if item["attempt_id"] == pending["attempt_id"]
        ]
        if len(attempts) != 1:
            raise Blocked("cleanup recovery attempt changed")
        attempt = attempts[0]
        if (
            attempt.get("phase") != "cleanup_blocked"
            or type(attempt.get("exit_status")) is not int
            or attempt["exit_status"] != 0
            or attempt.get("review_started") is not True
            or attempt.get("failure_reason") != "cleanup_denied"
            or (attempt["engine"], attempt["round"], attempt["folder"])
            != (pending["engine"], pending["round"], pending["folder"])
        ):
            raise Blocked("cleanup recovery requires a recorded successful worker exit")
        group = attempt.get("process_group")
        if type(group) is not int or group <= 0:
            raise Blocked("cleanup recovery process group is unavailable")
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise Blocked("cleanup recovery cannot inspect the process group") from error
        else:
            raise Blocked("cleanup recovery process group still exists")
        folder = self.directory / pending["folder"]
        recovery = folder / "result.json.recovery.json"
        if (
            digest(folder / "result.json") != pending.get("cleanup_result_sha256")
            or digest(folder / "worker.log") != pending.get("cleanup_log_sha256")
            or (digest(recovery) if recovery.exists() else None)
            != pending.get("result_recovery_sha256")
        ):
            raise Blocked("completed worker evidence changed after cleanup failure")
        # Ordinary completion still verifies head, result, ledger and gates.
        # No worker is relaunched and no pass or budget is added here.
        attempt["phase"] = pending["phase"] = "returned"
        attempt["cleanup_reconciled"] = True
        self.persist()

    def classify_idle_exit(
        self, pending: dict[str, Any], attempt: dict[str, Any], folder: Path
    ) -> None:
        """Mark a returned Agy pass that wrote nothing because its turn ended early."""
        marker = folder / "launch.json"
        if (
            any(
                (folder / name).exists() or (folder / name).is_symlink()
                for name in ("result.json", "result.json.recovery.json")
            )
            or marker.is_symlink()
            or not marker.is_file()
            or not agy_idle_exit(folder / "worker.log")
        ):
            return
        try:
            evidence = read(marker)
        except (Blocked, OSError, ValueError):
            return  # Unreadable evidence keeps the ordinary missing-result block.
        if (
            not isinstance(evidence, dict)
            or evidence.get("attempt_id") != attempt["attempt_id"]
            or evidence.get("version") != 1
            or evidence.get("phase") != "execution"
        ):
            return
        attempt.update(
            phase="idle_exit_failed",
            failure_reason="agy_idle_exit",
            launch_sha256=digest(marker),
            log_sha256=digest(folder / "worker.log"),
        )
        pending["phase"] = "idle_exit_failed"

    def verify_retry_evidence(
        self, pending: dict[str, Any], attempt: dict[str, Any], kind: str
    ) -> None:
        """Verify the original failure both during recovery and at the retry launch."""
        exit_status: int | None
        outputs: tuple[str, ...]
        if kind == "capacity":
            label, failure, exit_status = "capacity fallback", "capacity failure", 1
            proven, outputs = capacity_rejected, ("result.json",)
        elif kind == "startup_stall":
            label, failure, exit_status = (
                "startup-stall retry", "Codex startup stall", None
            )
            proven = codex_startup_stalled
            outputs = ("result.json", "result.json.recovery.json")
        elif kind == "provider_500":
            label, failure, exit_status = (
                "provider-error retry", "Claude provider 500", 1
            )
            proven = claude_provider_500
            outputs = ("result.json", "result.json.recovery.json")
        else:
            label, failure, exit_status = "idle-exit retry", "Agy idle exit", 0
            proven = agy_idle_exit
            outputs = ("result.json", "result.json.recovery.json")
        if (
            self.boundary() != pending["before"]
            or self.state["head"] != pending["before"]
        ):
            raise Blocked(f"{label} requires the unchanged review head")
        decision = self.decision(pending["before"])
        if (
            decision.get("passes") != self.state["completed"]
            or decision.get("status") != "next"
            or (decision.get("engine"), decision.get("round"))
            != (pending["engine"], pending["round"])
        ):
            raise Blocked(f"{label} cannot change the owed pass or budget")
        folder = self.directory / attempt["folder"]
        if (
            attempt.get("engine") != pending["engine"]
            or attempt.get("round") != pending["round"]
            or attempt.get("phase") != f"{kind}_failed"
            or type(attempt.get("exit_status")) is not type(exit_status)
            or attempt["exit_status"] != exit_status
            or attempt.get("review_started") is not True
            or digest(folder / "launch.json") != attempt.get("launch_sha256")
            or digest(folder / "worker.log") != attempt.get("log_sha256")
            or not proven(folder / "worker.log")
            or any(
                (folder / name).exists() or (folder / name).is_symlink()
                for name in outputs
            )
            or digest(folder / "historical.json") != pending["historical_sha256"]
        ):
            raise Blocked(f"{failure} evidence changed or a reviewer result exists")
        prefix = kind.replace("_", "-")
        for name, capture in (("threads", self.threads), ("comments", self.comments)):
            before = folder / f"before-{name}.json"
            if digest(before) != pending.get(f"before_{name}_sha256"):
                raise Blocked("pre-pass review evidence changed")
            current = folder / f"{prefix}-{name}.json"
            capture(current)
            if digest(current) != digest(before):
                raise Blocked(
                    f"review evidence changed; {label} requires reconciliation"
                )

    def copy_recovery_snapshot(
        self, source: Path, target: Path, attempt_id: str
    ) -> None:
        # A killed save can leave a temporary file. Keep it outside the evidence
        # directory so that the recorded recovery transaction remains resumable.
        staged = self.directory / f"recovery-{attempt_id}-{target.name}"
        save(staged, read(source))
        os.replace(staged, target)

    def recover_capacity(self, pending: dict[str, Any]) -> None:
        """Switch once to a pinned fallback without changing run, round or history."""
        self.verify_control()
        engine = pending["engine"]
        self.review_settings(engine)
        if engine != "codex":
            raise Blocked(
                "model is at capacity; no Codex fallback was pinned for this run"
            )
        recovery = pending.get("capacity_recovery")
        self.settings_call(
            "check_fallback", self.state, engine, "reviewer", in_progress=bool(recovery)
        )
        attempt = self.state["attempts"][-1]
        if attempt.get("attempt_id") != pending.get("attempt_id"):
            raise Blocked("capacity fallback transaction changed")
        self.verify_retry_evidence(pending, attempt, "capacity")
        retry = self.stage_retry(
            pending, attempt, "capacity_recovery", "fallback", "capacity fallback"
        )
        fallback = self.settings_call("switch_to_fallback", self.state, engine, "reviewer")
        pending.update(
            folder=str(retry.relative_to(self.directory)), phase="prepared",
            capacity_origin=attempt["attempt_id"],
        )
        pending.pop("capacity_recovery")
        self.persist()
        print(
            f"Capacity fallback: {engine} model {fallback['model']}, effort {fallback['effort']}",
            flush=True,
        )

    def stage_retry(
        self,
        pending: dict[str, Any],
        attempt: dict[str, Any],
        key: str,
        directory: str,
        label: str,
    ) -> Path:
        """Copy the pre-pass snapshots into a fresh retry folder, resumably."""
        folder = self.directory / str(pending["folder"])
        retry = folder / directory
        recovery = pending.get(key)
        if recovery is None:
            if retry.exists() or retry.is_symlink():
                raise Blocked(f"unexpected {label} directory")
            pending[key] = {"attempt_id": attempt["attempt_id"]}
            self.persist()
        elif recovery.get("attempt_id") != attempt["attempt_id"]:
            raise Blocked(f"{label} transaction changed")
        if retry.is_symlink():
            raise Blocked(f"{label} directory cannot be a symlink")
        retry.mkdir(mode=0o700, exist_ok=True)
        names = {"historical.json", "before-threads.json", "before-comments.json"}
        if any(
            p.name not in names or p.is_symlink() or not p.is_file()
            for p in retry.iterdir()
        ):
            raise Blocked(f"unexpected {label} evidence")
        for name in names:
            target = retry / name
            if target.exists():
                if digest(target) != digest(folder / name):
                    raise Blocked(f"{label} snapshot changed")
            else:
                self.copy_recovery_snapshot(
                    folder / name, target, attempt["attempt_id"]
                )
        return retry

    def recover_startup_stall(self, pending: dict[str, Any]) -> None:
        """Relaunch a Codex pass that stalled before thread.started, once."""
        self.verify_control()
        if pending["engine"] != "codex" or pending.get("startup_stall_origin"):
            raise Blocked(
                "Codex stalled before starting again; no further retry"
            )
        attempt = self.state["attempts"][-1]
        if attempt.get("attempt_id") != pending.get("attempt_id"):
            raise Blocked("startup-stall retry transaction changed")
        self.verify_retry_evidence(pending, attempt, "startup_stall")
        retry = self.stage_retry(
            pending,
            attempt,
            "startup_stall_recovery",
            "stall-retry",
            "startup-stall retry",
        )
        pending.update(
            folder=str(retry.relative_to(self.directory)), phase="prepared",
            startup_stall_origin=attempt["attempt_id"],
        )
        pending.pop("startup_stall_recovery")
        self.persist()
        print(
            f"Codex startup stall: retrying {pending['engine']} pass "
            f"{pending['round']} once at {pending['before']}",
            flush=True,
        )

    def recover_idle_exit(self, pending: dict[str, Any]) -> None:
        """Relaunch the same pinned Agy pass once without changing round or budget."""
        self.verify_control()
        if pending["engine"] != "gemini" or pending.get("idle_exit_origin"):
            raise Blocked(
                "Agy ended its turn again before writing a result; no further retry"
            )
        attempt = self.state["attempts"][-1]
        if attempt.get("attempt_id") != pending.get("attempt_id"):
            raise Blocked("idle-exit retry transaction changed")
        self.verify_retry_evidence(pending, attempt, "idle_exit")
        retry = self.stage_retry(
            pending, attempt, "idle_exit_recovery", "idle-retry", "idle-exit retry"
        )
        pending.update(
            folder=str(retry.relative_to(self.directory)), phase="prepared",
            idle_exit_origin=attempt["attempt_id"],
        )
        pending.pop("idle_exit_recovery")
        self.persist()
        print(
            f"Agy idle exit: retrying {pending['engine']} pass {pending['round']} "
            f"once at {pending['before']}",
            flush=True,
        )

    def recover_provider_500(self, pending: dict[str, Any]) -> None:
        """Retry a clean Claude provider failure once at the same head and round."""
        self.verify_control()
        if pending["engine"] != "claude" or pending.get("provider_500_origin"):
            raise Blocked("Claude provider 500 recurred; no further retry")
        attempt = self.state["attempts"][-1]
        if attempt.get("attempt_id") != pending.get("attempt_id"):
            raise Blocked("provider-error retry transaction changed")
        self.verify_retry_evidence(pending, attempt, "provider_500")
        retry = self.stage_retry(
            pending, attempt, "provider_500_recovery", "provider-retry",
            "provider-error retry",
        )
        pending.update(
            folder=str(retry.relative_to(self.directory)), phase="prepared",
            provider_500_origin=attempt["attempt_id"],
        )
        pending.pop("provider_500_recovery")
        self.persist()
        print(
            f"Claude provider 500: retrying pass {pending['round']} once "
            f"at {pending['before']}",
            flush=True,
        )

    def recover_preflight(self, pending: dict[str, Any]) -> None:
        if not getattr(self.args, "recover_preflight", False):
            raise Blocked(
                "preflight-only failure; repair the installation and use --resume --recover-preflight"
            )
        self.verify_control()
        head = self.boundary()
        if head != pending["before"] or head != self.state["head"]:
            raise Blocked("head changed; preflight recovery requires reconciliation")
        decision = self.decision(head)
        if (
            decision.get("passes") != self.state["completed"]
            or decision.get("status") != "next"
            or (decision.get("engine"), decision.get("round"))
            != (pending["engine"], pending["round"])
        ):
            raise Blocked(
                "ledger changed; preflight recovery cannot change the owed pass or budget"
            )
        folder = self.directory / pending["folder"]
        if (folder / "result.json").exists() or digest(
            folder / "historical.json"
        ) != pending["historical_sha256"]:
            raise Blocked("review evidence changed; reconcile before recovery")
        attempt = self.state["attempts"][-1]
        if (
            attempt.get("attempt_id") != pending.get("attempt_id")
            or attempt.get("review_started") is not False
            or attempt.get("phase") != "preflight_failed"
            or type(attempt.get("exit_status")) is not int
            or attempt["exit_status"] <= 0
            or digest(folder / "launch.json") != attempt.get("launch_sha256")
        ):
            raise Blocked("worker exit is unknown; no proven preflight-only failure")
        if "recovery" not in pending:
            retry = folder / ("retry-" + str(len(self.state["attempts"])))
            if retry.exists() or retry.is_symlink():
                raise Blocked("retry directory already contains evidence; reconcile it")
            pending["recovery"] = {
                "folder": str(retry.relative_to(self.directory)),
                "attempt_id": attempt["attempt_id"],
            }
            self.persist()
        recovery = pending["recovery"]
        retry = self.directory / recovery["folder"]
        if recovery["attempt_id"] != attempt["attempt_id"] or retry.is_symlink():
            raise Blocked("recovery transaction identity changed")
        retry.mkdir(mode=0o700, exist_ok=True)
        if any(
            p.name not in ("historical.json", "history.pending", "before-threads.json", "before-comments.json")
            or p.is_symlink()
            or not p.is_file()
            for p in retry.iterdir()
        ):
            raise Blocked("unexpected retry evidence; reconcile before launch")
        history = retry / "historical.json"
        if history.exists():
            if digest(history) != pending["historical_sha256"]:
                raise Blocked("retry snapshot changed")
        else:
            # Atomic creation prevents a failed copy from leaving a partial snapshot.
            temporary = retry / "history.pending"
            with temporary.open("wb") as stream:
                stream.write((folder / "historical.json").read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, history)
        for name in ("threads", "comments"):
            source = folder / f"before-{name}.json"
            if f"before_{name}_sha256" in pending:
                if digest(source) != pending[f"before_{name}_sha256"]:
                    raise Blocked("pre-pass review snapshot changed")
                target = retry / source.name
                if target.exists():
                    if digest(target) != digest(source):
                        raise Blocked("retry review snapshot changed")
                else:
                    self.copy_recovery_snapshot(source, target, attempt["attempt_id"])
        pending["folder"] = str(retry.relative_to(self.directory))
        pending["phase"] = "prepared"
        pending.pop("recovery")
        self.persist()
        self.launch(pending)

    def legacy_failure(self, pending: dict[str, Any], log: Path) -> dict[str, Any]:
        """Recognize only pre-execution failures proved by the pinned v1 pair."""
        diagnostic = log.read_bytes()
        if diagnostic == b"agy relay surface checkout must be clean\n":
            return {"exit_status": 1, "failure_reason": "dirty_surface"}
        if not re.fullmatch(
            rb"fatal: not a git repository: (/[^\x00-\x1f\x7f]+/\.git/worktrees/[^/\x00-\x1f\x7f]+|\(null\))\n",
            diagnostic,
        ):
            raise Blocked("legacy log does not prove a preflight-only failure")
        proof = getattr(self.args, "legacy_controller_log", None)
        if not proof:
            raise Blocked(
                "legacy Git failure requires --legacy-controller-log PATH SHA256"
            )
        controller_log = Path(proof[0]).absolute()
        if digest(controller_log) != proof[1]:
            raise Blocked("legacy controller log changed")
        terminal = (
            f"Starting gemini pass {pending['round']} at {pending['before']}\n"
            "review-chain blocked: bash exited 128; inspect worker.log; "
            f"checkpoint: {self.directory}\n"
        ).encode()
        if not controller_log.read_bytes().endswith(terminal):
            raise Blocked(
                "legacy controller log does not prove the matching exit and cleanup"
            )
        # In the recognized launcher the mutating invocation is followed by a
        # Python parser, which exits 1 and adds its own diagnostic on failure.
        # This sole Git diagnostic plus bash exit 128 therefore precedes review.
        # The recognized runner emits its terminal message only after cleanup;
        # denied cleanup or interruption produces a different terminal message.
        return {
            "exit_status": 128,
            "failure_reason": "surface_provenance",
            "legacy_controller_log": str(controller_log),
            "legacy_controller_log_sha256": proof[1],
        }

    def reconcile_legacy_preflight(
        self, pending: dict[str, Any], expected_log: str
    ) -> None:
        """Narrow, operator-requested proof for the supported pre-instrumentation pair."""
        if (
            self.state.get("attempts")
            or pending.get("phase") != "launching"
            or pending.get("engine") != "gemini"
            or not self.state.get("migrations")
        ):
            raise Blocked(
                "legacy preflight reconciliation does not apply to this attempt"
            )
        migration = self.state["migrations"][0]
        if (
            digest(self.directory / migration["prior_state"])
            != migration["prior_state_sha256"]
        ):
            raise Blocked("legacy migration evidence changed")
        for name, sha in migration["prior_control_hashes"].items():
            if digest(self.directory / migration["prior_control"] / name) != sha:
                raise Blocked("legacy controller evidence changed")
        if any(
            migration["prior_control_hashes"].get(name) != sha
            for name, sha in LEGACY_PREFLIGHT_HASHES.items()
        ):
            raise Blocked(
                "unsupported legacy launcher; unknown exits require reconciliation"
            )
        folder = self.directory / pending["folder"]
        log = folder / "worker.log"
        intent = pending.get("legacy_reconciliation")
        allowed = {"worker.log", "historical.json", "before-threads.json"}
        present = {p.name for p in folder.iterdir()}
        if (
            digest(log) != expected_log
            or not allowed <= present
            or present - allowed - ({"launch.json"} if intent else set())
        ):
            raise Blocked("legacy log does not prove a preflight-only failure")
        failure = self.legacy_failure(pending, log)
        if self.boundary() != pending["before"]:
            raise Blocked("legacy review head changed; reconcile it")
        if not intent:
            intent = {
                "attempt_id": uuid.uuid4().hex,
                "log_sha256": expected_log,
                "failure": failure,
            }
            pending["legacy_reconciliation"] = intent
            self.persist()
        if intent["log_sha256"] != expected_log:
            raise Blocked("legacy reconciliation proof changed")
        if (
            intent.get("failure", {"exit_status": 1, "failure_reason": "dirty_surface"})
            != failure
        ):
            raise Blocked("legacy reconciliation failure proof changed")
        attempt = {
            "attempt_id": intent["attempt_id"],
            "engine": pending["engine"],
            "round": pending["round"],
            "folder": pending["folder"],
            "review_started": False,
            "phase": "preflight_failed",
            "legacy_log_sha256": expected_log,
            **failure,
        }
        evidence = {
            "version": 1,
            "attempt_id": attempt["attempt_id"],
            "phase": "preflight",
            "review_started": False,
            "failure_reason": failure["failure_reason"],
            "proof": "explicit legacy reconciliation against pinned pre-execution exit",
            "legacy_log_sha256": expected_log,
        }
        if "legacy_controller_log" in failure:
            evidence.update(failure)
        marker = folder / "launch.json"
        if marker.exists() or marker.is_symlink():
            if read(marker) != evidence:
                raise Blocked("legacy reconciliation evidence changed")
        else:
            # Stage outside the evidence allowlist. A killed atomic save may
            # leave its temporary file; that must not invalidate the same proof.
            staged = self.directory / (
                "legacy-launch-" + attempt["attempt_id"] + ".json"
            )
            save(staged, evidence)
            os.replace(staged, marker)
        attempt["launch_sha256"] = digest(folder / "launch.json")
        self.state["attempts"].append(attempt)
        pending.update(phase="preflight_failed", attempt_id=attempt["attempt_id"])
        pending.pop("legacy_reconciliation")
        self.persist()

    def recovery_command(self) -> str:
        config = self.state["config"]
        argv = [
            sys.executable,
            str(self.control / "review-chain-runner.py"),
            "--repo",
            config["repo"],
            "--pr",
            str(config["pr"]),
            "--base",
            config["base_argument"],
            "--tier",
            config["tier"],
            "--author",
            config["author"],
            "--" + config["mode"],
            config["plan"],
            "--authorization-file",
            str(self.directory / "authorization.txt"),
            "--resume",
        ]
        if config["mode"] == "cycle":
            argv.append("--until-converged")
        if config["trigger"]:
            argv.extend(["--trigger", str(config["trigger"])])
        if config["require_dco"]:
            argv.append("--require-dco")
        for check in config["checks"]:
            argv.extend(["--check", check])
        if config.get("scope_decision"):
            argv.extend(["--scope-decision", config["scope_decision"]])
        if (self.state.get("pending") or {}).get("phase") == "preflight_failed":
            argv.append("--recover-preflight")
        intent = (self.state.get("pending") or {}).get("legacy_reconciliation")
        if intent:
            argv.extend(
                [
                    "--recover-preflight",
                    "--reconcile-legacy-preflight",
                    intent["log_sha256"],
                ]
            )
            failure = intent.get("failure", {})
            if "legacy_controller_log" in failure:
                argv.extend(
                    [
                        "--legacy-controller-log",
                        failure["legacy_controller_log"],
                        failure["legacy_controller_log_sha256"],
                    ]
                )
        return shlex.join(argv)

    def decision(self, head: str) -> dict[str, Any]:
        result = self.helper("controller", "next-pass", *self.scope(head))
        if result["run_id"] != self.state["run_id"]:
            raise Blocked("active run changed; refuse to adopt a different budget")
        return result

    def complete_pass(self, pending: dict[str, Any]) -> None:
        head = self.boundary()
        self.decision(head)
        folder = self.directory / pending["folder"]
        if digest(folder / "historical.json") != pending["historical_sha256"]:
            raise Blocked("pre-pass comment snapshot changed")
        result_path = folder / "result.json"
        if not result_path.exists():
            raise Blocked(
                "reviewer returned no result; pass not counted, explicit recovery required"
            )
        fields = [
            "--head",
            head,
            "--engine",
            pending["engine"],
            "--round",
            str(pending["round"]),
            "--base",
            self.state["base"],
            "--before",
            pending["before"],
            "--result-file",
            str(result_path),
        ]
        result = self.helper("ledger", "validate-result", *fields)
        # A late failure after result creation must not be silently accepted.
        if pending["phase"] != "returned":
            raise Blocked(
                "worker exit is unknown; reconcile before accepting its saved result"
            )
        recovery_sha256 = pending.get("result_recovery_sha256")
        if recovery_sha256:
            recovery = folder / "result.json.recovery.json"
            if digest(recovery) != recovery_sha256:
                raise Blocked("completed result recovery evidence changed")
            self.helper(
                "ledger",
                "recover-result",
                "--repo",
                self.args.repo,
                "--pr",
                str(self.args.pr),
                *fields,
                "--expected-recovery-sha256",
                recovery_sha256,
                "--historical-comment-ids-file",
                str(folder / "historical.json"),
            )
            result = self.helper("ledger", "validate-result", *fields)
        if result["status"] == "blocked":
            raise Blocked(
                "reviewer reported blocked without recoverable completed evidence; "
                "inspect saved result and recover the owed pass"
            )
        self.dco(head)
        validation = self.resolved_validation(head)
        expected_validation = {
            "head": head,
            "result": result["resultSha256"],
        }
        if validation["mode"] != "legacy":
            expected_validation.update(
                {
                    "mode": validation["mode"],
                    "gates": validation["gates"],
                    "manifest_sha256": validation["manifest_sha256"],
                    "changed_paths_sha256": validation["changed_paths_sha256"],
                    "environment_sha256": validation["environment_sha256"],
                }
            )
        if not (folder / "validated.json").exists():
            for index, check in enumerate(validation["commands"]):
                managed(
                    check["argv"],
                    folder / f"check-{index}.log",
                    check["environment"],
                )
            if self.boundary() != head:
                raise Blocked("validation changed the reviewed head")
            save(folder / "validated.json", expected_validation)
        if read(folder / "validated.json") != expected_validation:
            raise Blocked("saved validation does not name this exact head and result")
        self.threads(folder / "threads.json")
        intermediate = (
            command(
                [
                    "git",
                    "rev-list",
                    "--reverse",
                    "--ancestry-path",
                    f"{pending['before']}..{head}",
                ]
            ).splitlines()
            if head != pending["before"]
            else []
        )
        save(folder / "heads.json", [pending["before"], *intermediate])
        summary = folder / "summary.txt"
        validation_label = (
            "Legacy caller-supplied validation commands"
            if validation["mode"] == "legacy"
            else "Repository-declared validation gates "
            + ", ".join(validation["gates"])
            + f" (manifest {validation['manifest_sha256']})"
        )
        summary.write_text(
            f"Runner-verified {pending['engine']} pass {pending['round']} at {head}.\n"
            f"Base: {self.state['base']}. Result: {result['status']}.\n"
            + self.settings_line(pending["engine"])
            + validation_label
            + " passed at this exact head:\n"
            + "\n".join(shlex.join(check["argv"]) for check in validation["commands"])
            + "\n"
        )
        self.decision(head)
        attestation = self.helper(
            "ledger",
            "attest",
            "--repo",
            self.args.repo,
            "--pr",
            str(self.args.pr),
            *fields,
            "--expected-result-sha256",
            result["resultSha256"],
            "--threads-file",
            str(folder / "threads.json"),
            "--expected-threads-sha256",
            digest(folder / "threads.json"),
            "--allowed-heads-file",
            str(folder / "heads.json"),
            "--historical-comment-ids-file",
            str(folder / "historical.json"),
            "--content-file",
            str(summary),
        )
        if attestation.get("verified") is not True:
            raise Blocked("helper did not verify the pass attestation")
        next_step = self.decision(head)
        passes = next_step["passes"]
        if len(passes) != len(self.state["completed"]) + 1 or (
            passes[-1]["engine"],
            passes[-1]["round"],
            passes[-1]["head"],
        ) != (pending["engine"], pending["round"], head):
            raise Blocked("ledger did not advance by exactly the authorized pass")
        self.state.update(head=head, completed=passes, pending=None, status="running")
        self.persist()

    def settings_line(self, engine: str) -> str:
        settings = self.settings_call("selected", self.state, engine, "reviewer")
        if not settings:
            return "Reviewer settings: not recorded by this run.\n"
        return f"Reviewer settings: {self.settings_call('describe', settings)}.\n"

    def run(self) -> str:
        self.initialize()
        if self.state["status"] in ("converged", "plan-complete", "exhausted"):
            if self.boundary() != self.state["head"]:
                raise Blocked("terminal result belongs to a different head")
            return str(self.state["status"])
        self.preflight()
        self.dco(self.boundary())
        if self.state.get("finishing"):
            terminal = self.state["finishing"]
            if self.boundary() != self.state["head"]:
                raise Blocked("head changed during finalization")
            self.helper(
                "controller",
                "finish-run",
                *self.scope(self.state["head"]),
                "--run-id",
                str(self.state["run_id"]),
                "--outcome",
                "converged" if terminal == "converged" else "exhausted",
            )
            self.state.update(status=terminal, finishing=None)
            self.persist()
            return str(terminal)
        if self.state["run_id"] is None:
            config = self.state["config"]
            started = self.helper(
                "controller",
                "start-run",
                *self.scope(self.state["start_head"]),
                "--base",
                self.state["base"],
                "--tier",
                config["tier"],
                "--" + config["mode"],
                config["plan"],
                "--authorization-file",
                str(self.directory / "authorization.txt"),
                *(
                    ["--scope-decision", config["scope_decision"]]
                    if config.get("scope_decision")
                    else []
                ),
            )
            self.state["run_id"] = started["run_id"]
            self.persist()
        if not self.state.get("metadata_posted"):
            engines = [
                "gemini" if e.strip() == "antigravity" else e.strip()
                for e in self.state["config"]["plan"].split(",")
            ]
            reviewers = list(dict.fromkeys(e for e in engines if e != self.args.author))
            roster_state = self.helper(
                "ledger",
                "read-roster",
                "--repo",
                self.args.repo,
                "--pr",
                str(self.args.pr),
            )
            if roster_state.get("present") and (
                roster_state["author"] != self.args.author
                or set(roster_state["reviewers"]) != set(reviewers)
            ):
                raise Blocked(
                    "existing roster differs from this plan; explicitly reconcile it first"
                )
            # A single-engine finite plan may run, but cannot claim independent coverage.
            if reviewers:
                roster = self.directory / "roster.txt"
                roster.write_text(
                    "Reviewer roster from the explicitly authorized runner plan.\n"
                )
                self.helper(
                    "ledger",
                    "post-roster",
                    *self.scope(self.state["head"]),
                    "--author",
                    self.args.author,
                    "--reviewers",
                    ",".join(reviewers),
                    "--content-file",
                    str(roster),
                )
            tier = self.directory / "tier.txt"
            trigger = (
                f" trigger={self.args.trigger}"
                if self.args.tier == "deep"
                else " trigger=none"
            )
            tier.write_text(
                f"<!-- local-review-tier:v1 tier={self.args.tier}{trigger} head={self.state['head']} -->\n"
                + (self.directory / "authorization.txt").read_text()
                + "\n"
            )
            self.helper(
                "ledger",
                "post-pr-comment",
                *self.scope(self.state["head"]),
                "--body-file",
                str(tier),
            )
            self.state["metadata_posted"] = True
            self.persist()
        while True:
            if self.state["pending"]:
                pending = self.state["pending"]
                legacy_proof = getattr(self.args, "reconcile_legacy_preflight", None)
                if legacy_proof and pending["phase"] == "launching":
                    self.reconcile_legacy_preflight(pending, legacy_proof)
                if pending["phase"] == "preflight_failed":
                    self.recover_preflight(pending)
                elif pending["phase"] == "capacity_failed":
                    self.recover_capacity(pending)
                elif pending["phase"] == "idle_exit_failed":
                    self.recover_idle_exit(pending)
                elif pending["phase"] == "startup_stall_failed":
                    self.recover_startup_stall(pending)
                elif pending["phase"] == "provider_500_failed":
                    self.recover_provider_500(pending)
                elif pending["phase"] == "cleanup_blocked":
                    self.recover_cleanup(pending)
                elif pending["phase"] == "prepared":
                    self.launch(pending)
                else:
                    self.complete_pass(pending)
                continue
            head = self.boundary()
            if head != self.state["head"]:
                raise Blocked("head changed outside an owned review pass")
            decision = self.decision(head)
            if decision["passes"] != self.state["completed"]:
                raise Blocked("unexpected pass evidence; reconcile before resuming")
            status = decision["status"]
            if status != "next":
                if status not in ("converged", "plan-complete", "exhausted"):
                    raise Blocked("unknown terminal decision")
                self.state["finishing"] = status
                self.persist()
                self.helper(
                    "controller",
                    "finish-run",
                    *self.scope(head),
                    "--run-id",
                    str(self.state["run_id"]),
                    "--outcome",
                    "converged" if status == "converged" else "exhausted",
                )
                self.state["status"] = status
                self.state["finishing"] = None
                self.persist()
                return str(status)
            engine, number = decision["engine"], decision["round"]
            authorization = self.helper(
                "controller",
                "authorize-pass",
                *self.scope(head),
                "--base",
                self.state["base"],
                "--engine",
                engine,
                "--round",
                str(number),
            )
            if authorization.get("run_id") != self.state["run_id"]:
                raise Blocked("active run changed before launch")
            folder = self.directory / f"pass-{len(self.state['completed']) + 1}"
            # pending is persisted before launch. With no pending pass, only
            # pre-launch snapshot files may be left by an interrupted export.
            if folder.is_symlink():
                raise Blocked("pass directory cannot be a symlink")
            folder.mkdir(mode=0o700, exist_ok=True)
            if any(
                p.name
                not in (
                    "before-threads.json",
                    "before-comments.json",
                    "historical.json",
                )
                or p.is_symlink()
                or not p.is_file()
                for p in folder.iterdir()
            ):
                raise Blocked(
                    "unexpected uncheckpointed pass evidence; reconcile before launch"
                )
            ids = self.threads(folder / "before-threads.json")
            self.comments(folder / "before-comments.json")
            save(folder / "historical.json", ids)
            pending = {
                "engine": engine,
                "round": number,
                "before": head,
                "folder": folder.name,
                # launch() alone moves a pass to "launching" once its attempt
                # is recorded; until then nothing has started and resume may
                # relaunch it.
                "phase": "prepared",
                "historical_sha256": digest(folder / "historical.json"),
                "before_threads_sha256": digest(folder / "before-threads.json"),
                "before_comments_sha256": digest(folder / "before-comments.json"),
            }
            self.state["pending"] = pending
            self.persist()
            self.launch(pending)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--validate-contract"]:
        try:
            return validate_contract_command()
        except (Blocked, OSError, ValueError, KeyError) as error:
            print(f"validation contract invalid: {error}", file=sys.stderr)
            return 2
    if os.environ.get("AGENT_LOOP_REVIEW_RESULT_FILE"):
        raise Blocked("a one-pass reviewer cannot start another chain runner")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--base", required=True)
    parser.add_argument("--tier", required=True, choices=("lean", "deep"))
    parser.add_argument("--author", required=True, choices=tuple(LAUNCHERS))
    parser.add_argument(
        "--trigger", help="all resolved Deep trigger ids, comma-separated"
    )
    plan = parser.add_mutually_exclusive_group(required=True)
    plan.add_argument("--chain")
    plan.add_argument("--cycle")
    parser.add_argument("--until-converged", action="store_true")
    parser.add_argument(
        "--check",
        action="append",
        help=(
            "legacy required full-suite command (argv syntax; no shell); omit when "
            f"the pinned target policy contains {VALIDATION_CONTRACT}"
        ),
    )
    parser.add_argument(
        "--authorization-file",
        required=True,
        help="public-safe scope and tier rationale",
    )
    parser.add_argument("--require-dco", action="store_true")
    parser.add_argument(
        "--scope-decision",
        choices=("keep", "split"),
        help="scope decision forwarded to start-run when its scope checkpoint fires",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--recover-preflight",
        action="store_true",
        help="retry a proven preflight-only attempt within the same run",
    )
    parser.add_argument(
        "--migrate-controller",
        metavar="COMMIT",
        help="explicitly adopt this clean controller commit for a v1 checkpoint",
    )
    parser.add_argument(
        "--reconcile-legacy-preflight",
        metavar="LOG_SHA256",
        help="explicitly verify a supported v1 pre-execution rejection log",
    )
    parser.add_argument(
        "--legacy-controller-log",
        nargs=2,
        metavar=("PATH", "SHA256"),
        help="original v1 controller output and reviewed hash proving Git exit 128",
    )
    parser.add_argument(
        "--repair-installation",
        action="store_true",
        help="preserve and replace the managed installation at its existing pins",
    )
    args = parser.parse_args(arguments)
    if (
        args.recover_preflight or args.migrate_controller or args.repair_installation
    ) and not args.resume:
        parser.error("recovery and controller migration require --resume")
    if args.reconcile_legacy_preflight and (
        not args.recover_preflight
        or not re.fullmatch(r"[0-9a-f]{64}", args.reconcile_legacy_preflight)
    ):
        parser.error(
            "legacy proof requires --recover-preflight and the reviewed log SHA256"
        )
    if args.legacy_controller_log and (
        not args.reconcile_legacy_preflight
        or not re.fullmatch(r"[0-9a-f]{64}", args.legacy_controller_log[1])
    ):
        parser.error(
            "legacy controller log requires legacy reconciliation and its SHA256"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", args.base):
        parser.error("--base must be a pinned full commit SHA")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo) or args.pr < 1:
        parser.error("invalid repository or PR")
    if bool(args.cycle) != args.until_converged:
        parser.error("--cycle requires --until-converged; --chain does not use it")
    if (args.tier == "deep") != (args.trigger is not None) or any(
        not shlex.split(c) for c in (args.check or [])
    ):
        parser.error("Deep requires a trigger; Lean has none; checks must not be empty")
    if args.trigger is not None and not re.fullmatch(r"[1-6](?:,[1-6])*", args.trigger):
        parser.error("triggers must be comma-separated ids from 1 through 6")
    common = Path(command(["git", "rev-parse", "--git-common-dir"])).resolve()
    directory = (
        common / "activeloom-review" / f"{args.repo.replace('/', '-')}-{args.pr}"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise Blocked("checkpoint directory cannot be a symlink")
    with (directory / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Blocked("another runner owns this PR") from error
        runner = Runner(args, directory)
        try:
            status = runner.run()
        except (
            Blocked,
            OSError,
            ValueError,
            KeyError,
            subprocess.TimeoutExpired,
            KeyboardInterrupt,
        ) as error:
            print(
                f"review-chain blocked: {error}; checkpoint: {directory}",
                file=sys.stderr,
            )
            if runner.state:
                print(
                    f"After reconciliation, in {runner.state['config']['worktree']}:\n{runner.recovery_command()}",
                    file=sys.stderr,
                )
            return 2
        print(
            json.dumps(
                {
                    "status": status,
                    "passes": runner.state["completed"],
                    "head": runner.state["head"],
                }
            )
        )
        return 0 if status == "converged" else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Blocked, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"review-chain blocked: {error}", file=sys.stderr)
        raise SystemExit(2) from error
