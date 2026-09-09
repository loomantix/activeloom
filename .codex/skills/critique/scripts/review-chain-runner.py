#!/usr/bin/env python3
"""Drive a bounded PR review plan. Workers return results; this process advances it.

POSIX, same-user automation, not a sandbox against a malicious reviewer. The
control snapshot is independent of worker commits; resumption checks its hashes.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
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
    *LAUNCHERS.values(),
]


class Blocked(RuntimeError):
    pass


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
                    raise Blocked(
                        f"{Path(argv[0]).name} exited {code}; inspect {log.name}"
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

    def persist(self) -> None:
        save(self.checkpoint, self.state)

    def helper(self, name: str, *args: str) -> dict[str, Any]:
        for filename, expected in self.state.get("control_hashes", {}).items():
            if digest(self.control / filename) != expected:
                raise Blocked("pinned controller or launcher changed")
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
            if not self.args.resume or self.state["config"] != config:
                raise Blocked(
                    "checkpoint exists; use --resume with the same plan, tier, and gates"
                )
            if (
                digest(Path(__file__))
                != self.state["control_hashes"]["review-chain-runner.py"]
            ):
                raise Blocked(
                    "runner version changed; deliberate checkpoint migration required"
                )
            for name, expected in self.state["control_hashes"].items():
                if digest(self.control / name) != expected:
                    raise Blocked(
                        "pinned controller or launcher changed; reconcile, do not relaunch"
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
            "version": 1,
            "config": config,
            "base": base,
            "head": head,
            "start_head": head,
            "actor": command(["gh", "api", "user", "--jq", ".login"]),
            "control_hashes": {n: digest(self.control / n) for n in CONTROL_FILES},
            "run_id": None,
            "pending": None,
            "completed": [],
            "status": "prepared",
        }
        self.persist()

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
        if pending["phase"] == "launching":
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
                self.complete_pass(self.state["pending"])
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
            env = {
                k: v for k, v in os.environ.items() if not k.startswith("AGENT_LOOP_")
            }
            for key in ("CLAUDE_REVIEW_CLI", "AGY_REVIEW_CLI", "CODEX_REVIEW_CLI"):
                env.pop(key, None)
            env.update(
                AGENT_LOOP_REVIEW_RESULT_FILE=str(folder / "result.json"),
                AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE=str(
                    folder / "historical.json"
                ),
                AGENT_LOOP_PR_NUMBER=str(self.args.pr),
                AGENT_LOOP_PR_HEAD_SHA=head,
                AGENT_LOOP_REVIEW_BASE_SHA=self.state["base"],
                AGENT_LOOP_REVIEW_ROUND=str(number),
                AGENT_LOOP_REVIEW_ENGINE=engine,
                AGENT_LOOP_LOG_DIR=str(folder),
                GH_REPO=self.args.repo,
            )
            print(f"Starting {engine} pass {number} at {head}", flush=True)
            launcher = self.control / LAUNCHERS[engine]
            managed(
                [
                    sys.executable if engine == "codex" else "bash",
                    str(launcher),
                    *self.scope(head),
                    "--base",
                    self.state["base"],
                    "--round",
                    str(number),
                ],
                folder / "worker.log",
                env,
                3660,
            )
            pending["phase"] = "returned"
            self.persist()


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
    args = parser.parse_args(argv)
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
