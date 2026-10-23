"""Hermetic safety coverage for the wrapper-owned review push helper."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.regression


ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / ".claude/skills/agent-loop/scripts/review-push.sh"
AGENTS_HELPER = ROOT / ".agents/skills/agent-loop/scripts/review-push.sh"
# The publish-time ledger gate is the same in each harness that runs it on this host.
LEDGER_GATE_HELPERS = pytest.mark.parametrize(
    "helper", [HELPER, AGENTS_HELPER], ids=[".claude", ".agents"]
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def review_repo(tmp_path: Path) -> tuple[Path, dict[str, str], str]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "test: seed")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "switch", "-c", "agent-loop/issue-7")
    _git(repo, "push", "-u", "origin", "HEAD:refs/heads/agent-loop/issue-7")
    start = _git(repo, "rev-parse", "HEAD")
    (repo / "seed.txt").write_text("seed\nreview\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "fix: review")
    env = os.environ.copy()
    env.update(
        {
            "AGENT_LOOP_WORKTREE": str(repo),
            "AGENT_LOOP_BRANCH": "agent-loop/issue-7",
            "AGENT_LOOP_PR_HEAD_SHA": start,
            "AGENT_LOOP_REVIEW_PUSH_STATE_FILE": str(tmp_path / "review-push-state"),
            "AGENT_LOOP_REAL_GIT": shutil.which("git") or "git",
            "AGENT_LOOP_ORIGIN_FETCH_URLS": _git(
                repo, "remote", "get-url", "--all", "origin"
            ),
            "AGENT_LOOP_ORIGIN_PUSH_URLS": _git(
                repo, "remote", "get-url", "--push", "--all", "origin"
            ),
        }
    )
    (tmp_path / "review-push-state").write_text(f"{start}\n", encoding="utf-8")
    _install_fake_ledger(tmp_path, env)
    return repo, env, start


FAKE_LEDGER = """
const { appendFileSync, readFileSync } = require('node:fs');
const args = process.argv.slice(2);
const manifest = readFileSync(args[args.indexOf('--findings-file') + 1], 'utf8');
appendFileSync(process.env.FAKE_LEDGER_LOG, JSON.stringify({ args, manifest }) + '\\n');
if (process.env.FAKE_LEDGER_REFUSE) {
  console.error('review-ledger: ' + process.env.FAKE_LEDGER_REFUSE);
  process.exit(1);
}
"""


def _install_fake_ledger(tmp_path: Path, env: dict[str, str]) -> None:
    """Stand in for the ledger's preflight-publish so no test reaches GitHub."""
    ledger = tmp_path / "review-ledger.cjs"
    ledger.write_text(FAKE_LEDGER, encoding="utf-8")
    log_dir = tmp_path / "logs"
    (log_dir / "ledger-content").mkdir(parents=True)
    (log_dir / "ledger-content" / "publish-fixes.json").write_text(
        '[{"fingerprint": "window", "occurrence": 1}]\n', encoding="utf-8"
    )
    env.update(
        {
            "AGENT_LOOP_LOG_DIR": str(log_dir),
            "AGENT_LOOP_REVIEW_LEDGER": str(ledger),
            "AGENT_LOOP_PR_NUMBER": "12",
            "AGENT_LOOP_REVIEW_ENGINE": "codex",
            "AGENT_LOOP_REVIEW_ROUND": "3",
            "AGENT_LOOP_REVIEW_CONTRACT_VERSION": "3",
            "GH_REPO": "example/repo",
            "FAKE_LEDGER_LOG": str(tmp_path / "fake-ledger.log"),
        }
    )
    env.pop("FAKE_LEDGER_REFUSE", None)


def test_rejects_external_head_incorporated_after_helper_push(
    review_repo: tuple[Path, dict[str, str], str]
) -> None:
    repo, env, _ = review_repo
    first = _run(repo, env)
    assert first.returncode == 0, first.stderr
    helper_head = first.stdout.strip()

    competing = repo.parent / "competing-after-push"
    subprocess.run(
        ["git", "clone", str(repo.parent / "remote.git"), str(competing)],
        check=True,
        capture_output=True,
    )
    _git(competing, "config", "user.name", "Competing")
    _git(competing, "config", "user.email", "competing@example.invalid")
    _git(competing, "switch", "agent-loop/issue-7")
    (competing / "competing.txt").write_text("external\n", encoding="utf-8")
    _git(competing, "add", "competing.txt")
    _git(competing, "commit", "-m", "fix: external review change")
    competing_head = _git(competing, "rev-parse", "HEAD")
    _git(competing, "push", "origin", "HEAD:refs/heads/agent-loop/issue-7")

    _git(repo, "fetch", "origin", "agent-loop/issue-7")
    _git(repo, "merge", "--ff-only", "FETCH_HEAD")
    (repo / "third.txt").write_text("third review fix\n", encoding="utf-8")
    _git(repo, "add", "third.txt")
    _git(repo, "commit", "-m", "fix: third review")

    result = _run(repo, env)

    assert result.returncode != 0
    assert "stale or uncertain remote head" in result.stderr
    assert Path(env["AGENT_LOOP_REVIEW_PUSH_STATE_FILE"]).read_text(
        encoding="utf-8"
    ) == f"{helper_head}\n"
    assert (
        _git(
            repo,
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent-loop/issue-7",
        ).split()[0]
        == competing_head
    )


def test_rejects_divergent_history_after_helper_push(
    review_repo: tuple[Path, dict[str, str], str]
) -> None:
    repo, env, start = review_repo
    first = _run(repo, env)
    assert first.returncode == 0, first.stderr
    helper_head = first.stdout.strip()

    _git(repo, "reset", "--hard", start)
    (repo / "divergent.txt").write_text("divergent review fix\n", encoding="utf-8")
    _git(repo, "add", "divergent.txt")
    _git(repo, "commit", "-m", "fix: divergent review")

    result = _run(repo, env)

    assert result.returncode != 0
    assert "drops a previously published review commit" in result.stderr
    assert Path(env["AGENT_LOOP_REVIEW_PUSH_STATE_FILE"]).read_text(
        encoding="utf-8"
    ) == f"{helper_head}\n"
    assert (
        _git(
            repo,
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent-loop/issue-7",
        ).split()[0]
        == helper_head
    )


def test_rejects_changed_origin_before_push(
    review_repo: tuple[Path, dict[str, str], str],
) -> None:
    repo, env, _ = review_repo
    redirected = repo.parent / "redirected.git"
    subprocess.run(
        ["git", "init", "--bare", str(redirected)],
        check=True,
        capture_output=True,
    )
    _git(repo, "remote", "set-url", "--push", "origin", str(redirected))

    result = _run(repo, env)

    assert result.returncode != 0
    assert "changed origin fetch/push identity" in result.stderr
    assert not _git(repo, "ls-remote", "--heads", str(redirected))


def test_v3_guard_rejects_self_authorized_direct_push(tmp_path: Path) -> None:
    guard = ROOT / ".claude/skills/agent-loop/scripts/hook-git-guard"
    env = os.environ.copy()
    env.update(
        {
            "AGENT_LOOP_REAL_GIT": "/bin/echo",
            "AGENT_LOOP_ALLOW_REVIEW_MUTATIONS": "true",
            "AGENT_LOOP_REVIEW_CONTRACT_VERSION": "3",
            "AGENT_LOOP_SAFE_REVIEW_PUSH": "1",
            "AGENT_LOOP_BRANCH": "agent-loop/issue-7",
        }
    )

    result = subprocess.run(
        [str(guard), "push", "origin", "HEAD:refs/heads/agent-loop/issue-7"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "contract-v3 review hooks must publish" in result.stderr


def test_v2_guard_preserves_explicit_staged_review_push(tmp_path: Path) -> None:
    guard = ROOT / ".claude/skills/agent-loop/scripts/hook-git-guard"
    env = os.environ.copy()
    env.update(
        {
            "AGENT_LOOP_REAL_GIT": "/bin/echo",
            "AGENT_LOOP_ALLOW_REVIEW_MUTATIONS": "true",
            "AGENT_LOOP_REVIEW_CONTRACT_VERSION": "2",
            "AGENT_LOOP_BRANCH": "agent-loop/issue-7",
        }
    )

    result = subprocess.run(
        [str(guard), "push", "origin", "HEAD:refs/heads/agent-loop/issue-7"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "push origin HEAD:refs/heads/agent-loop/issue-7" in result.stdout


def _run(
    repo: Path, env: dict[str, str], *args: str, helper: Path = HELPER
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(helper), *args], cwd=repo, env=env, capture_output=True, text=True
    )


@pytest.mark.fast
def test_reports_review_push_protocol_version() -> None:
    result = subprocess.run(
        [str(HELPER), "--protocol-version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout == "1\n"


def test_exact_fully_qualified_review_push_succeeds(
    review_repo: tuple[Path, dict[str, str], str],
) -> None:
    repo, env, _ = review_repo
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _git(repo, "rev-parse", "HEAD")
    assert (
        _git(
            repo, "ls-remote", "--heads", "origin", "refs/heads/agent-loop/issue-7"
        ).split()[0]
        == result.stdout.strip()
    )


def test_two_sequential_review_pushes_succeed(
    review_repo: tuple[Path, dict[str, str], str],
) -> None:
    repo, env, _ = review_repo
    first = _run(repo, env)
    assert first.returncode == 0, first.stderr

    (repo / "second.txt").write_text("second review fix\n", encoding="utf-8")
    _git(repo, "add", "second.txt")
    _git(repo, "commit", "-m", "fix: second review")
    second = _run(repo, env)

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == _git(repo, "rev-parse", "HEAD")
    assert (
        _git(
            repo,
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent-loop/issue-7",
        ).split()[0]
        == second.stdout.strip()
    )


def test_rejects_failed_worktree_status_inspection(
    review_repo: tuple[Path, dict[str, str], str], tmp_path: Path
) -> None:
    repo, env, start = review_repo
    real_git = env["AGENT_LOOP_REAL_GIT"]
    failing_git = tmp_path / "git-with-failing-status"
    failing_git.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = status ]; then exit 42; fi\n'
        'exec "$REAL_GIT_FOR_TEST" "$@"\n',
        encoding="utf-8",
    )
    failing_git.chmod(0o700)
    env["REAL_GIT_FOR_TEST"] = real_git
    env["AGENT_LOOP_REAL_GIT"] = str(failing_git)

    result = _run(repo, env)

    assert result.returncode != 0
    assert "could not inspect worktree cleanliness" in result.stderr
    assert (
        _git(
            repo,
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent-loop/issue-7",
        ).split()[0]
        == start
    )


@pytest.mark.parametrize("argument", ["origin", "HEAD", "HEAD:other", "--force"])
def test_rejects_bare_ambiguous_wrong_destination_and_force_arguments(
    review_repo: tuple[Path, dict[str, str], str], argument: str
) -> None:
    repo, env, _ = review_repo
    result = _run(repo, env, argument)
    assert result.returncode == 2
    assert "accepts no arguments" in result.stderr


def test_rejects_wrong_checked_out_branch(
    review_repo: tuple[Path, dict[str, str], str],
) -> None:
    repo, env, _ = review_repo
    _git(repo, "switch", "-c", "other")
    result = _run(repo, env)
    assert result.returncode != 0
    assert "different checked-out branch" in result.stderr


def test_rejects_stale_remote_head(
    review_repo: tuple[Path, dict[str, str], str],
) -> None:
    repo, env, start = review_repo
    competing = repo.parent / "competing"
    subprocess.run(
        ["git", "clone", str(repo.parent / "remote.git"), str(competing)],
        check=True,
        capture_output=True,
    )
    _git(competing, "config", "user.name", "Competing")
    _git(competing, "config", "user.email", "competing@example.invalid")
    _git(competing, "switch", "agent-loop/issue-7")
    (competing / "competing.txt").write_text("advanced\n", encoding="utf-8")
    _git(competing, "add", "competing.txt")
    _git(competing, "commit", "-m", "fix: competing review")
    competing_head = _git(competing, "rev-parse", "HEAD")
    _git(competing, "push", "origin", "HEAD:refs/heads/agent-loop/issue-7")

    result = _run(repo, env)
    assert result.returncode != 0
    assert "stale or uncertain remote head" in result.stderr
    assert start != competing_head
    assert (
        _git(
            repo,
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/agent-loop/issue-7",
        ).split()[0]
        == competing_head
    )


def _remote_head(repo: Path) -> str:
    return _git(
        repo, "ls-remote", "--heads", "origin", "refs/heads/agent-loop/issue-7"
    ).split()[0]


@LEDGER_GATE_HELPERS
def test_checks_named_fixes_against_the_ledger_before_push(
    review_repo: tuple[Path, dict[str, str], str], helper: Path
) -> None:
    repo, env, start = review_repo
    result = _run(repo, env, helper=helper)
    assert result.returncode == 0, result.stderr
    [call] = [
        json.loads(line)
        for line in Path(env["FAKE_LEDGER_LOG"]).read_text().splitlines()
    ]
    assert call["args"] == [
        "preflight-publish",
        "--repo",
        "example/repo",
        "--pr",
        "12",
        "--head",
        start,
        "--engine",
        "codex",
        "--round",
        "3",
        "--findings-file",
        str(Path(env["AGENT_LOOP_LOG_DIR"]) / "ledger-content/publish-fixes.json"),
    ]
    assert json.loads(call["manifest"]) == [{"fingerprint": "window", "occurrence": 1}]


@LEDGER_GATE_HELPERS
def test_refuses_publication_without_a_fix_manifest(
    review_repo: tuple[Path, dict[str, str], str], helper: Path
) -> None:
    repo, env, start = review_repo
    (Path(env["AGENT_LOOP_LOG_DIR"]) / "ledger-content/publish-fixes.json").unlink()
    result = _run(repo, env, helper=helper)
    assert result.returncode != 0
    assert "publish-fixes.json" in result.stderr
    assert "posted before publication" in result.stderr
    assert _remote_head(repo) == start


@LEDGER_GATE_HELPERS
def test_refuses_publication_the_ledger_rejects(
    review_repo: tuple[Path, dict[str, str], str], helper: Path
) -> None:
    repo, env, start = review_repo
    env["FAKE_LEDGER_REFUSE"] = (
        "convergence fixes require a posted blocking finding; "
        "label occurrence 1 is major: defer it and leave its change out of the push"
    )
    result = _run(repo, env, helper=helper)
    assert result.returncode != 0
    assert "label occurrence 1 is major" in result.stderr
    assert "refused publication" in result.stderr
    assert _remote_head(repo) == start
    assert Path(env["AGENT_LOOP_REVIEW_PUSH_STATE_FILE"]).read_text() == f"{start}\n"


@LEDGER_GATE_HELPERS
def test_contract_v2_publication_skips_the_v3_ledger_check(
    review_repo: tuple[Path, dict[str, str], str], helper: Path
) -> None:
    repo, env, _ = review_repo
    env["AGENT_LOOP_REVIEW_CONTRACT_VERSION"] = "2"
    (Path(env["AGENT_LOOP_LOG_DIR"]) / "ledger-content/publish-fixes.json").unlink()
    result = _run(repo, env, helper=helper)
    assert result.returncode == 0, result.stderr
    assert not Path(env["FAKE_LEDGER_LOG"]).exists()
