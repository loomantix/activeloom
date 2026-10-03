"""Real-Git coverage for isolated agent-loop repositories and dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_codex_agent_loop import Consumer, _executable, _git


ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / ".codex/skills/agent-loop/scripts/isolate-repository.py"


def _publish(consumer: Consumer) -> None:
    _git("add", ".", cwd=consumer.repo)
    _git("commit", "-m", "test: configure isolated run", cwd=consumer.repo)
    _git("push", "origin", "main", cwd=consumer.repo)


def _helper(
    consumer: Consumer, destination: Path, *child_args: str, exit_code: int = 0
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ISOLATION_TEST_RECORD"] = str(consumer.tmp / "child.json")
    env["ISOLATION_TEST_EXIT"] = str(exit_code)
    return subprocess.run(
        [
            sys.executable, str(HELPER),
            "--project-dir", str(consumer.repo),
            "--base-ref", "origin/main",
            "--destination", str(destination),
            "--harness", ".codex", "--", *child_args,
        ],
        cwd=consumer.repo, env=env, text=True, capture_output=True, timeout=60,
    )


def _stub_consumer(tmp_path: Path) -> Consumer:
    consumer = Consumer(tmp_path, 3)
    _executable(
        consumer.repo / ".codex/skills/agent-loop/scripts/agent-loop.sh",
        """#!/usr/bin/env python3
import json, os, pathlib, sys
pathlib.Path(os.environ['ISOLATION_TEST_RECORD']).write_text(json.dumps({
    'cwd': os.getcwd(), 'argv': sys.argv[1:], 'runner': __file__,
}))
sys.exit(int(os.environ['ISOLATION_TEST_EXIT']))
""",
    )
    _publish(consumer)
    return consumer


def test_helper_dispatches_pinned_controller_with_independent_git_state(tmp_path: Path) -> None:
    consumer = _stub_consumer(tmp_path)
    source_head = _git("rev-parse", "HEAD", cwd=consumer.repo)
    _git("config", "branch.unrelated.remote", "origin", cwd=consumer.repo)
    note = consumer.repo / "unfinished.txt"
    note.write_text("preserve this unrelated work\n", encoding="utf-8")
    destination = tmp_path / "batch with spaces"
    result = _helper(consumer, destination, "--issues", "5,7", "--iterations", "2", exit_code=17)
    assert result.returncode == 17, result.stdout + result.stderr
    record = json.loads((tmp_path / "child.json").read_text(encoding="utf-8"))
    assert Path(record["cwd"]) == destination / "controller"
    assert record["argv"] == ["--issues", "5,7", "--iterations", "2"]
    repository = destination / "repository"
    controller = destination / "controller"
    common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=controller))
    assert common == repository / ".git"
    assert common != consumer.repo / ".git"
    assert not (common / "objects/info/alternates").exists()
    source_inodes = {
        (path.stat().st_dev, path.stat().st_ino)
        for path in (consumer.repo / ".git/objects").rglob("*") if path.is_file()
    }
    assert not any(
        (path.stat().st_dev, path.stat().st_ino) in source_inodes
        for path in (common / "objects").rglob("*") if path.is_file()
    )
    assert _git("rev-parse", "HEAD", cwd=controller) == source_head
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=controller) == "HEAD"
    assert _git("remote", "get-url", "origin", cwd=controller) == str(tmp_path / "remote.git")
    assert _git("config", "user.name", cwd=controller) == "Test"
    assert _git("config", "user.email", cwd=controller) == "test@example.invalid"
    assert "branch.unrelated.remote" not in _git("config", "--local", "--list", cwd=controller)
    assert note.read_text(encoding="utf-8") == "preserve this unrelated work\n"
    assert _git("rev-parse", "HEAD", cwd=consumer.repo) == source_head


def test_helper_refuses_existing_destination_without_changing_it(tmp_path: Path) -> None:
    consumer = _stub_consumer(tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep"
    marker.write_text("retained\n", encoding="utf-8")
    result = _helper(consumer, destination)
    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "retained\n"
    assert not (tmp_path / "child.json").exists()
    assert sorted(path.name for path in destination.iterdir()) == ["keep"]


def test_helper_uses_selected_base_and_keeps_two_batches_independent(tmp_path: Path) -> None:
    consumer = _stub_consumer(tmp_path)
    base = _git("rev-parse", "origin/main", cwd=consumer.repo)
    (consumer.repo / "local-only.txt").write_text("unpublished commit\n", encoding="utf-8")
    _git("add", "local-only.txt", cwd=consumer.repo)
    _git("commit", "-m", "test: unrelated local work", cwd=consumer.repo)
    source_head = _git("rev-parse", "HEAD", cwd=consumer.repo)
    first = tmp_path / "first"
    second = tmp_path / "second"
    for destination in (first, second):
        result = _helper(consumer, destination)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _git("rev-parse", "HEAD", cwd=destination / "controller") == base
        assert not (destination / "controller/local-only.txt").exists()
    _git("config", "branch.parallel.remote", "origin", cwd=first / "controller")
    _git("update-ref", "refs/heads/parallel", base, cwd=first / "controller")
    for repository in (consumer.repo, second / "controller"):
        assert "branch.parallel.remote" not in _git("config", "--local", "--list", cwd=repository)
        assert "refs/heads/parallel" not in _git("show-ref", cwd=repository)
    assert _git("rev-parse", "HEAD", cwd=consumer.repo) == source_head


def test_helper_retains_worktree_author_settings(tmp_path: Path) -> None:
    consumer = _stub_consumer(tmp_path)
    _git("config", "extensions.worktreeConfig", "true", cwd=consumer.repo)
    _git("config", "--worktree", "user.name", "Worktree Author", cwd=consumer.repo)
    destination = tmp_path / "isolated"
    result = _helper(consumer, destination)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git("config", "user.name", cwd=destination / "controller") == "Worktree Author"


def test_helper_preserves_raw_origin_before_url_rewriting(tmp_path: Path) -> None:
    consumer = _stub_consumer(tmp_path)
    remote = str(tmp_path / "remote.git")
    _git("remote", "set-url", "origin", "fixture:remote", cwd=consumer.repo)
    _git("config", f"url.{remote}.insteadOf", "fixture:remote", cwd=consumer.repo)
    # Git rewrites once. Storing the expanded URL would trigger the other rule
    # in the clone and change the remote's identity.
    _git("config", "url.unexpected:remote.insteadOf", remote, cwd=consumer.repo)
    assert _git("remote", "get-url", "origin", cwd=consumer.repo) == remote
    destination = tmp_path / "isolated"
    result = _helper(consumer, destination)
    assert result.returncode == 0, result.stdout + result.stderr
    controller = destination / "controller"
    assert _git("config", "remote.origin.url", cwd=controller) == "fixture:remote"
    assert _git("remote", "get-url", "origin", cwd=controller) == remote
    assert _git("remote", "get-url", "--push", "origin", cwd=controller) == remote


@pytest.mark.parametrize("untracked", [False, True])
@pytest.mark.parametrize("relative", [
    ".codex/skills/agent-loop/agent-loop.config",
    ".codex/skills/agent-loop/prompt.txt",
    "agent-loop-instructions.md",
    ".codex/skills/agent-loop/scripts/agent-loop.sh",
])
def test_helper_refuses_uncommitted_consumer_bootstrap(
    tmp_path: Path, relative: str, untracked: bool
) -> None:
    consumer = _stub_consumer(tmp_path)
    if untracked:
        _git("rm", "--cached", relative, cwd=consumer.repo)
        _git("commit", "-m", "test: remove bootstrap", cwd=consumer.repo)
        _git("push", "origin", "main", cwd=consumer.repo)
    target = consumer.repo / relative
    target.write_text("uncommitted bootstrap\n", encoding="utf-8")
    destination = tmp_path / "isolated"
    result = _helper(consumer, destination)
    assert result.returncode != 0
    assert not (tmp_path / "child.json").exists()
    assert target.read_text(encoding="utf-8") == "uncommitted bootstrap\n"


def test_isolated_dry_run_selects_without_creating_clone_or_claiming(tmp_path: Path) -> None:
    consumer = Consumer(tmp_path, 3)
    destination = tmp_path / "isolated"
    result = consumer.run(
        "--isolate", str(destination), "--dry-run", "--issues", "5",
        extra_env={"AGENT_READY_JSON": '[{"number": 5}]'},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(destination) in result.stdout
    assert not destination.exists()
    assert not (tmp_path / "worktrees").exists()
    assert "issue edit" not in consumer.log("gh.log")
    assert consumer.log("ready.log")


@pytest.mark.parametrize("resume", ["--resume-run", "--resume-batch"])
def test_isolate_rejects_resume_before_creating_destination(tmp_path: Path, resume: str) -> None:
    consumer = Consumer(tmp_path, 3)
    destination = tmp_path / "isolated"
    result = consumer.run("--isolate", str(destination), resume, str(tmp_path / "checkpoint.json"))
    assert result.returncode != 0
    assert "--isolate" in result.stderr and "resume" in result.stderr.lower()
    assert not destination.exists()
    assert "issue edit" not in consumer.log("gh.log")


@pytest.mark.parametrize("mutate_isolated", [False, True])
def test_worker_config_changes_are_scoped_to_isolated_repository(
    tmp_path: Path, mutate_isolated: bool
) -> None:
    worker = tmp_path / "worker.py"
    _executable(worker, """#!/usr/bin/env python3
import json, os, pathlib, subprocess
cwd = pathlib.Path.cwd()
target = cwd if os.environ['MUTATE_ISOLATED'] == '1' else pathlib.Path(os.environ['SOURCE_CHECKOUT'])
subprocess.run(['/usr/bin/git', '-C', str(target), 'config', 'branch.concurrent.remote', 'origin'], check=True)
pathlib.Path(os.environ['WORKER_RECORD']).write_text(json.dumps({'cwd': str(cwd)}))
(cwd / 'result.py').write_text('value = 1\\n')
subprocess.run(['/usr/bin/git', 'add', 'result.py'], check=True)
subprocess.run(['/usr/bin/git', 'commit', '-m', 'fix: worker result'], check=True)
""")
    consumer = Consumer(
        tmp_path, 3,
        f'worker_hook = python3 "{worker}"\n'
        'worker_retries = 1\n',
    )
    config = consumer.repo / ".codex/skills/agent-loop/agent-loop.config"
    config.write_text(config.read_text(encoding="utf-8").replace(
        "validation_hook = true", 'validation_hook = printf validated > "$VALIDATION_RECORD"; false'
    ), encoding="utf-8")
    _publish(consumer)
    destination = tmp_path / "isolated"
    validation = tmp_path / "validated"
    result = consumer.run(
        "--isolate", str(destination), "--issues", "5", "--iterations", "1",
        extra_env={
            "AGENT_READY_JSON": '[{"number": 5}]',
            "SOURCE_CHECKOUT": str(consumer.repo),
            "MUTATE_ISOLATED": "1" if mutate_isolated else "0",
            "WORKER_RECORD": str(tmp_path / "worker.json"),
            "VALIDATION_RECORD": str(validation),
        },
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    record_path = tmp_path / "worker.json"
    assert record_path.exists(), output
    worktree = Path(json.loads(record_path.read_text(encoding="utf-8"))["cwd"])
    common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree))
    assert common == destination / "repository/.git"
    if mutate_isolated:
        assert not validation.exists(), output
        assert "Git configuration changed" in output
    else:
        assert validation.read_text(encoding="utf-8") == "validated", output
        assert "Git configuration changed" not in output
        assert _git("config", "branch.concurrent.remote", cwd=consumer.repo) == "origin"
    # The validation hook intentionally stops before any publication.
    assert "pr create" not in consumer.log("gh.log")
