"""Publish-time ledger gate of the Codex root's review push helper.

The helper locks its pass journal with flock, checks modes with GNU stat and
runs pre-publication validation under the Linux process supervisor, so these
cases run only on Linux.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


pytestmark = [
    pytest.mark.regression,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux flock, stat and supervisor"),
]


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".codex/skills/agent-loop/scripts"
HELPER = SCRIPTS / "review-push.sh"
SUPERVISOR = SCRIPTS / "process-supervisor.py"
BRANCH = "agent-loop/issue-7"
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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _config_sha256(repo: Path) -> str:
    """The fingerprint the helper recomputes after dropping every GIT_* variable."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    listing = subprocess.run(
        ["git", "--no-replace-objects", "config", "--null", "--list"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(listing).hexdigest()


@pytest.fixture
def review_repo(tmp_path: Path) -> tuple[Path, dict[str, str], str]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "test: seed")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "switch", "-c", BRANCH)
    _git(repo, "push", "-u", "origin", f"HEAD:refs/heads/{BRANCH}")
    start = _git(repo, "rev-parse", "HEAD")
    (repo / "seed.txt").write_text("seed\nreview\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "fix: review")

    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    state_file = journal / "review-push-state"
    _private(
        state_file,
        json.dumps(
            {
                "version": 2,
                "startSha": start,
                "validatedSha": None,
                "publishedSha": None,
                "validationReceipt": None,
            }
        ),
    )
    _private(journal / "review-push-state.lock", "")
    guard_bin = tmp_path / "guard-bin"
    guard_bin.mkdir()
    ledger = tmp_path / "review-ledger.cjs"
    ledger.write_text(FAKE_LEDGER, encoding="utf-8")
    log_dir = tmp_path / "logs"
    (log_dir / "ledger-content").mkdir(parents=True)
    (log_dir / "ledger-content/publish-fixes.json").write_text(
        '[{"fingerprint": "window", "occurrence": 1}]\n', encoding="utf-8"
    )

    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("AGENT_LOOP_", "FAKE_LEDGER_"))
    }
    env.update(
        {
            "AGENT_LOOP_WORKTREE": str(repo),
            "AGENT_LOOP_BRANCH": BRANCH,
            "AGENT_LOOP_PR_HEAD_SHA": start,
            "AGENT_LOOP_REAL_GIT": shutil.which("git") or "git",
            "AGENT_LOOP_ORIGIN_FETCH_URLS": _git(repo, "remote", "get-url", "--all", "origin"),
            "AGENT_LOOP_ORIGIN_PUSH_URLS": _git(
                repo, "remote", "get-url", "--push", "--all", "origin"
            ),
            "AGENT_LOOP_REVIEW_PUSH_STATE_FILE": str(state_file),
            "AGENT_LOOP_REVIEW_VALIDATION_HOOK": 'printf "validated\\n"',
            "AGENT_LOOP_PROCESS_SUPERVISOR": str(SUPERVISOR),
            "AGENT_LOOP_PROCESS_SUPERVISOR_SHA256": hashlib.sha256(
                SUPERVISOR.read_bytes()
            ).hexdigest(),
            "AGENT_LOOP_HOOK_TIMEOUT_SECONDS": "60",
            "AGENT_LOOP_REVIEW_DEADLINE_EPOCH": str(int(time.time()) + 600),
            "AGENT_LOOP_REVIEW_VALIDATION_LOG": str(journal / "prepublish-validation.log"),
            "AGENT_LOOP_HOOK_GUARD_BIN": str(guard_bin),
            "AGENT_LOOP_GIT_CONFIG_SHA256": _config_sha256(repo),
            "AGENT_LOOP_LOG_DIR": str(log_dir),
            "AGENT_LOOP_REVIEW_LEDGER": str(ledger),
            "AGENT_LOOP_PR_NUMBER": "12",
            "AGENT_LOOP_REVIEW_ENGINE": "codex",
            "AGENT_LOOP_REVIEW_ROUND": "3",
            "AGENT_LOOP_REVIEW_CONTRACT_VERSION": "4",
            "GH_REPO": "example/repo",
            "FAKE_LEDGER_LOG": str(tmp_path / "fake-ledger.log"),
        }
    )
    return repo, env, start


def _run(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HELPER)], cwd=repo, env=env, capture_output=True, text=True, timeout=120
    )


def _remote_head(repo: Path) -> str:
    return _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}").split()[0]


def _journal(env: dict[str, str]) -> dict[str, object]:
    value = json.loads(Path(env["AGENT_LOOP_REVIEW_PUSH_STATE_FILE"]).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _ledger_calls(env: dict[str, str]) -> list[dict[str, object]]:
    log = Path(env["FAKE_LEDGER_LOG"])
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_checks_named_fixes_against_the_ledger_then_validates_and_publishes(
    review_repo: tuple[Path, dict[str, str], str],
) -> None:
    repo, env, start = review_repo
    local_head = _git(repo, "rev-parse", "HEAD")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    [call] = _ledger_calls(env)
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
    assert json.loads(str(call["manifest"])) == [{"fingerprint": "window", "occurrence": 1}]
    assert "validated" in Path(env["AGENT_LOOP_REVIEW_VALIDATION_LOG"]).read_text()
    assert _remote_head(repo) == local_head
    assert _journal(env)["publishedSha"] == local_head


def test_a_refused_publication_leaves_the_pass_able_to_publish(
    review_repo: tuple[Path, dict[str, str], str],
) -> None:
    repo, env, start = review_repo
    env["FAKE_LEDGER_REFUSE"] = (
        "convergence fixes require a posted blocking finding; "
        "label occurrence 1 is major: defer it and leave its change out of the push"
    )
    refused = _run(repo, env)
    assert refused.returncode != 0
    assert "label occurrence 1 is major" in refused.stderr
    assert "refused publication" in refused.stderr
    assert _remote_head(repo) == start
    assert _journal(env) == {
        "version": 2,
        "startSha": start,
        "validatedSha": None,
        "publishedSha": None,
        "validationReceipt": None,
    }
    # The refusal comes before validation, so no validation ran or was recorded.
    assert not Path(env["AGENT_LOOP_REVIEW_VALIDATION_LOG"]).exists()

    del env["FAKE_LEDGER_REFUSE"]
    corrected = _run(repo, env)
    assert corrected.returncode == 0, corrected.stderr
    assert _remote_head(repo) == _git(repo, "rev-parse", "HEAD")
    assert len(_ledger_calls(env)) == 2


def test_refuses_publication_without_a_fix_manifest(
    review_repo: tuple[Path, dict[str, str], str],
) -> None:
    repo, env, start = review_repo
    (Path(env["AGENT_LOOP_LOG_DIR"]) / "ledger-content/publish-fixes.json").unlink()
    result = _run(repo, env)
    assert result.returncode != 0
    assert "publish-fixes.json" in result.stderr
    assert "posted before publication" in result.stderr
    assert _remote_head(repo) == start
    assert _ledger_calls(env) == []


def test_contract_v2_publication_skips_the_v3_ledger_check(
    review_repo: tuple[Path, dict[str, str], str],
) -> None:
    repo, env, _ = review_repo
    env["AGENT_LOOP_REVIEW_CONTRACT_VERSION"] = "2"
    (Path(env["AGENT_LOOP_LOG_DIR"]) / "ledger-content/publish-fixes.json").unlink()
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    assert _ledger_calls(env) == []
    assert _remote_head(repo) == _git(repo, "rev-parse", "HEAD")
