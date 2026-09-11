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
import uuid
from pathlib import Path
from typing import Any

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
    *LAUNCHERS.values(),
]
# This v1 pair has exactly one dirty-surface diagnostic, followed by exit 1
# before the mutating Agy invocation. No other legacy log is recovery evidence.
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


def managed(
    argv: list[str], log: Path, env: dict[str, str], timeout: int = 3600
) -> None:
    """Keep the PID through cancellation, forward TERM, then kill the group."""

    def interrupted(signum: int, frame: Any) -> None:
        raise Blocked(f"interrupted by signal {signum}")

    handlers = {
        s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        with log.open("ab") as output:
            os.chmod(log, 0o600)
            child = subprocess.Popen(
                argv, stdout=output, stderr=output, env=env, start_new_session=True
            )
            try:
                code = child.wait(timeout=timeout)
                if code:
                    raise ProcessFailure(
                        f"{Path(argv[0]).name} exited {code}; inspect {log}", code
                    )
            finally:
                # Also clean up descendants left behind after the leader exits.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
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

    def initialize(self) -> None:
        head = self.boundary()
        config = {
            "repo": self.args.repo,
            "pr": self.args.pr,
            "worktree": str(Path.cwd()),
            "tier": self.args.tier,
            "author": self.args.author,
            "trigger": self.args.trigger,
            "mode": "chain" if self.args.chain else "cycle",
            "plan": self.args.chain or self.args.cycle,
            "checks": self.args.check,
            "base_argument": self.args.base,
            "require_dco": self.args.require_dco
            or Path(".github/workflows/dco.yml").is_file(),
        }
        if self.state:
            if self.state.get("version") not in (1, 2):
                raise Blocked("unsupported checkpoint version")
            if not self.args.resume or self.state["config"] != config:
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
                    "runner version changed; deliberate checkpoint migration required"
                )
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
        base = command(["git", "rev-parse", "--verify", self.args.base + "^{commit}"])
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
            raise Blocked(f"partial review installation requires inspection: {native}")
        native.mkdir(mode=0o700)
        revision = self.state["installation_revision"]
        roots = [
            ".codex" if e == "codex" else ".claude"
            for e in self.engines()
            if e != "gemini"
        ]
        files: dict[str, str] = {}
        if roots:
            archive = subprocess.check_output(
                ["git", "archive", revision, *roots], timeout=120
            )
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
        folder = self.directory / pending["folder"]
        attempt = {
            "attempt_id": uuid.uuid4().hex,
            "engine": pending["engine"],
            "round": pending["round"],
            "folder": pending["folder"],
            "review_started": None,
            "exit_status": None,
            "failure_reason": None,
            "phase": "launching",
        }
        self.state.setdefault("attempts", []).append(attempt)
        pending.update(phase="launching", attempt_id=attempt["attempt_id"])
        self.persist()
        env = self.environment(pending["engine"])
        env.update(
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
            )
            attempt.update(exit_status=0, review_started=True, phase="returned")
            pending["phase"] = "returned"
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
                    if (
                        evidence.get("phase") == "preflight"
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
        finally:
            self.persist()
        if error:
            raise error

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
            or digest(folder / "launch.json") != attempt.get("launch_sha256")
        ):
            raise Blocked("worker exit is unknown; no proven preflight-only failure")
        retry = folder / ("retry-" + str(len(self.state["attempts"])))
        if retry.exists() or retry.is_symlink():
            raise Blocked("retry directory already contains evidence; reconcile it")
        retry.mkdir(mode=0o700)
        shutil.copyfile(folder / "historical.json", retry / "historical.json")
        pending["folder"] = str(retry.relative_to(self.directory))
        pending["phase"] = "prepared"
        self.persist()
        self.launch(pending)

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
        if (
            digest(log) != expected_log
            or log.read_bytes() != b"agy relay surface checkout must be clean\n"
            or {p.name for p in folder.iterdir()}
            != {"worker.log", "historical.json", "before-threads.json"}
        ):
            raise Blocked("legacy log does not prove a preflight-only failure")
        if self.boundary() != pending["before"]:
            raise Blocked("legacy review head changed; reconcile it")
        attempt = {
            "attempt_id": uuid.uuid4().hex,
            "engine": pending["engine"],
            "round": pending["round"],
            "folder": pending["folder"],
            "review_started": False,
            "exit_status": 1,
            "phase": "preflight_failed",
            "failure_reason": "dirty_surface",
            "legacy_log_sha256": expected_log,
        }
        save(
            folder / "launch.json",
            {
                "version": 1,
                "attempt_id": attempt["attempt_id"],
                "phase": "preflight",
                "review_started": False,
                "failure_reason": "dirty_surface",
                "proof": "explicit legacy reconciliation against pinned pre-execution exit",
                "legacy_log_sha256": expected_log,
            },
        )
        attempt["launch_sha256"] = digest(folder / "launch.json")
        self.state["attempts"].append(attempt)
        pending.update(phase="preflight_failed", attempt_id=attempt["attempt_id"])
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
        if (self.state.get("pending") or {}).get("phase") == "preflight_failed":
            argv.append("--recover-preflight")
        return shlex.join(argv)

    def decision(self, head: str) -> dict[str, Any]:
        result = self.helper("controller", "next-pass", *self.scope(head))
        if result["run_id"] != self.state["run_id"]:
            raise Blocked("active run changed; refuse to adopt a different budget")
        return result

    def complete_pass(self, pending: dict[str, Any]) -> None:
        head = self.boundary()
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
        if result["status"] == "blocked":
            raise Blocked(
                "reviewer reported blocked; inspect saved result and recover the owed pass"
            )
        # A late failure after result creation must not be silently accepted.
        if pending["phase"] != "returned":
            raise Blocked(
                "worker exit is unknown; reconcile before accepting its saved result"
            )
        self.dco(head)
        if not (folder / "validated.json").exists():
            for index, check in enumerate(self.state["config"]["checks"]):
                managed(
                    shlex.split(check), folder / f"check-{index}.log", dict(os.environ)
                )
            if self.boundary() != head:
                raise Blocked("validation changed the reviewed head")
            save(
                folder / "validated.json",
                {"head": head, "result": result["resultSha256"]},
            )
        if read(folder / "validated.json") != {
            "head": head,
            "result": result["resultSha256"],
        }:
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
        summary.write_text(
            f"Runner-verified {pending['engine']} pass {pending['round']} at {head}.\n"
            f"Base: {self.state['base']}. Result: {result['status']}.\n"
            "Required unfiltered validation commands passed at this exact head:\n"
            + "\n".join(self.state["config"]["checks"])
            + "\n"
        )
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
                p.name not in ("before-threads.json", "historical.json")
                or p.is_symlink()
                or not p.is_file()
                for p in folder.iterdir()
            ):
                raise Blocked(
                    "unexpected uncheckpointed pass evidence; reconcile before launch"
                )
            ids = self.threads(folder / "before-threads.json")
            save(folder / "historical.json", ids)
            pending = {
                "engine": engine,
                "round": number,
                "before": head,
                "folder": folder.name,
                "phase": "launching",
                "historical_sha256": digest(folder / "historical.json"),
            }
            self.state["pending"] = pending
            self.persist()
            self.launch(pending)


def main(argv: list[str] | None = None) -> int:
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
        required=True,
        help="required full-suite command (argv syntax; no shell)",
    )
    parser.add_argument(
        "--authorization-file",
        required=True,
        help="public-safe scope and tier rationale",
    )
    parser.add_argument("--require-dco", action="store_true")
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
        "--repair-installation",
        action="store_true",
        help="preserve and replace the managed installation at its existing pins",
    )
    args = parser.parse_args(argv)
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
    if not re.fullmatch(r"[0-9a-f]{40}", args.base):
        parser.error("--base must be a pinned full commit SHA")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo) or args.pr < 1:
        parser.error("invalid repository or PR")
    if bool(args.cycle) != args.until_converged:
        parser.error("--cycle requires --until-converged; --chain does not use it")
    if (args.tier == "deep") != (args.trigger is not None) or any(
        not shlex.split(c) for c in args.check
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
