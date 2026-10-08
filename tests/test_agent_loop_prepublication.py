"""Recover completed workers without replay or ambiguous publication across harnesses."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_agent_loop import (
    REPO_ROOT,
    _clean_v3_hook,
    _config_v3,
    _environment,
    _issue,
    _run_git,
    _write_executable,
    consumer as consumer_fixture,
)

pytestmark = pytest.mark.regression

consumer = consumer_fixture


@pytest.fixture(params=(".codex", ".claude", ".agents"))
def harness(
    request: pytest.FixtureRequest, consumer: tuple[Path, Path, Path, Path]
) -> str:
    """Install the selected controller while retaining the shared GitHub fixture."""
    root = str(request.param)
    repo = consumer[0]
    if root != ".claude":
        shutil.copytree(repo / ".claude", repo / root, dirs_exist_ok=True)
        for path in (REPO_ROOT / root / "skills/agent-loop/scripts").iterdir():
            if path.is_file():
                shutil.copy2(
                    path, repo / root / "skills/agent-loop/scripts" / path.name
                )
        if root == ".agents":
            shutil.copytree(
                REPO_ROOT / root / "skills/review-setup/scripts",
                repo / root / "skills/review-setup/scripts",
                dirs_exist_ok=True,
            )
    if root == ".codex":
        shutil.copy2(
            REPO_ROOT / root / "skills/agent-loop/prompt.txt.template",
            repo / root / "skills/agent-loop/prompt.txt",
        )
        shutil.copy2(
            REPO_ROOT / root / "skills/agent-loop/agent-loop-instructions.md.template",
            repo / "agent-loop-instructions.md",
        )
    return root


def run_loop(
    fixture: tuple[Path, Path, Path, Path],
    root: str,
    tmp_path: Path,
    args: list[str],
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    """Run the real controller with deterministic worker and reviewer hooks."""
    config = _config_v3(tmp_path, **overrides)
    if root == ".codex":
        config = (
            config.replace("config_doctor = false", "config_doctor = true")
            .replace("codex_review_hook =", "codex_review_hook = : /deepcritique;")
            .replace("claude_review_hook =", "claude_review_hook = : /deepcritique;")
        )
    if root == ".agents":
        config = config.replace("codex_review_hook", "gemini_review_hook").replace(
            _clean_v3_hook("codex"), _clean_v3_hook("gemini")
        )
    (fixture[0] / root / "skills/agent-loop/agent-loop.config").write_text(config)
    result = subprocess.run(
        [str(fixture[0] / root / "skills/agent-loop/scripts/agent-loop.sh"), *args],
        cwd=fixture[0],
        env=_environment(fixture, [_issue(71), _issue(72)]),
        capture_output=True,
        text=True,
        timeout=90,
    )
    if not list((tmp_path / "logs").glob("*issue-71*/run-state.json")):
        assert result.returncode == 0, result.stderr + result.stdout
    return result


def lose_pr_response_once(fixture: tuple[Path, Path, Path, Path]) -> None:
    """Create the draft PR, then fail the first lookup of its number."""
    gh = fixture[2] / "gh"
    gh.write_text(
        gh.read_text().replace(
            "if '--json number' in joined:\n        print('1')",
            "if '--json number' in joined:\n        if not (state / 'response-lost').exists():\n            (state / 'response-lost').touch()\n            sys.exit(1)\n        print('1')",
        )
    )


def checkpoint(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    """Return the first issue's durable checkpoint."""
    path = next((tmp_path / "logs").glob("*issue-71*/run-state.json"))
    return path, json.loads(path.read_text())


def set_phase(
    fixture: tuple[Path, Path, Path, Path],
    root: str,
    path: Path,
    phase: str,
    base: str,
    head: str,
) -> None:
    """Advance a checkpoint as the controller would before an interruption."""
    helper = fixture[0] / root / "skills/agent-loop/scripts/agent-loop-state.py"
    subprocess.run(
        ["python3", str(helper), "update", "--file", str(path), "--phase", phase,
         "--round", "1", "--base-sha", base, "--head-sha", head],
        check=True, capture_output=True, text=True,
    )  # fmt: skip


def test_completed_worker_validation_failure_resumes_without_replay(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
) -> None:
    failure = consumer[3] / "fail-validation"
    failure.touch()
    gate = 'test ! -f "$AGENT_STATE_DIR/fail-validation"'
    stopped = run_loop(
        consumer, harness, tmp_path, ["--issues", "71,72"], validation_hook=gate
    )
    assert stopped.returncode != 0, stopped.stdout
    state_path, state = checkpoint(tmp_path)
    assert state["phase"] == "worker-complete"
    assert state["prNumber"] is None
    original_budget = state["reviewBudget"]
    assert isinstance(original_budget, dict)
    assert original_budget["attempts"] == []
    batch = next((tmp_path / "logs").glob("*batch*.json"))
    assert json.loads(batch.read_text())["issues"][0]["childRunState"] == str(
        state_path
    )
    failure.unlink()
    resumed = run_loop(
        consumer,
        harness,
        tmp_path,
        ["--resume-batch", str(batch)],
        validation_hook=gate,
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert (
        consumer[3].joinpath("events.log").read_text().splitlines().count("worker") == 2
    )
    final = json.loads(state_path.read_text())
    assert final["phase"] == "finalized"
    assert final["headSha"] == state["headSha"]


@pytest.mark.parametrize(
    ("phase", "refusal"),
    (
        ("worker-complete", "Pre-publication head changed"),
        ("integrating", "Interrupted integration does not match its recorded parents"),
    ),
)
def test_preparation_failure_and_head_drift_never_publish(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
    phase: str,
    refusal: str,
) -> None:
    stopped = run_loop(
        consumer, harness, tmp_path, ["--issues", "71"], preparation_hook="exit 42"
    )
    assert stopped.returncode != 0
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "worker-complete"
    assert "pr create" not in consumer[3].joinpath("gh.log").read_text()
    if phase == "integrating":
        # A commit that is not the recorded merge must not pass as integration.
        target = _run_git("rev-parse", "main", cwd=consumer[1]).stdout.strip()
        set_phase(consumer, harness, path, phase, target, str(state["headSha"]))
    worktree = str(state["worktree"])
    subprocess.run(
        [
            "git",
            "-C",
            worktree,
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-m",
            "unexpected",
        ],
        check=True,
        capture_output=True,
    )
    resumed = run_loop(consumer, harness, tmp_path, ["--resume-run", str(path)])
    assert resumed.returncode != 0
    assert refusal in resumed.stderr
    assert "pr create" not in consumer[3].joinpath("gh.log").read_text()


def test_interrupted_pr_creation_adopts_exact_push_without_worker_replay(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
) -> None:
    # Fail after branch publication, then return to the same private checkpoint.
    gate = 'touch "$AGENT_STATE_DIR/fail-pr-create"'
    gh = consumer[2] / "gh"
    gh.write_text(
        gh.read_text().replace(
            "if os.environ.get('AGENT_PR_CREATE_FAIL'):",
            "if (state / 'fail-pr-create').exists():",
        )
    )
    stopped = run_loop(
        consumer, harness, tmp_path, ["--issues", "71"], validation_hook=gate
    )
    assert stopped.returncode != 0
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "pushed"
    consumer[3].joinpath("fail-pr-create").unlink()
    resumed = run_loop(consumer, harness, tmp_path, ["--resume-run", str(path)])
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert (
        consumer[3].joinpath("events.log").read_text().splitlines().count("worker") == 1
    )
    assert json.loads(path.read_text())["headSha"] == state["headSha"]


@pytest.mark.fast
@pytest.mark.parametrize(
    ("preparation", "expected_status"),
    (("set -e; false; printf unexpected", 1), ("exit 42", 42), ("true", 0)),
)
def test_preparation_preserves_exit_status(
    preparation: str, expected_status: int
) -> None:
    for root in (".codex", ".claude", ".agents"):
        text = (
            REPO_ROOT / root / "skills/agent-loop/scripts/agent-loop.sh"
        ).read_text()
        region = text.split("# agent-loop-publication:begin\n")[1].split(
            "# agent-loop-publication:end"
        )[0]
        command = subprocess.run(
            [
                "bash",
                "-c",
                region
                + '\nPREPARATION_HOOK="$1"\nVALIDATION_HOOK="printf validated"\n'
                + "prepared_validation_hook",
                "preparation-test",
                preparation,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True
        )
        assert result.returncode == expected_status
        assert result.stdout == ("validated" if expected_status == 0 else "")


@pytest.mark.fast
def test_publication_region_is_identical_in_every_controller() -> None:
    expected = (REPO_ROOT / "scripts/agent-loop-publication.sh.inc").read_text()
    for root in (".codex", ".claude", ".agents"):
        text = (
            REPO_ROOT / root / "skills/agent-loop/scripts/agent-loop.sh"
        ).read_text()
        assert (
            text.split("# agent-loop-publication:begin\n")[1].split(
                "# agent-loop-publication:end"
            )[0]
            == expected
        )


@pytest.mark.parametrize("failure", ("lost-pr-response", "wrong-remote-head"))
def test_publication_reconciliation_never_duplicates_or_overwrites(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
    failure: str,
) -> None:
    gh = consumer[2] / "gh"
    if failure == "lost-pr-response":
        lose_pr_response_once(consumer)
        stopped = run_loop(consumer, harness, tmp_path, ["--issues", "71"])
    else:
        gh.write_text(
            gh.read_text().replace(
                "if os.environ.get('AGENT_PR_CREATE_FAIL'):",
                "if (state / 'fail-pr-create').exists():",
            )
        )
        stopped = run_loop(
            consumer,
            harness,
            tmp_path,
            ["--issues", "71"],
            validation_hook='touch "$AGENT_STATE_DIR/fail-pr-create"',
        )
    assert stopped.returncode != 0
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "pushed"
    if failure == "wrong-remote-head":
        _run_git(
            "update-ref",
            "refs/heads/" + str(state["branch"]),
            _run_git("rev-parse", "main", cwd=consumer[1]).stdout.strip(),
            cwd=consumer[1],
        )
        consumer[3].joinpath("fail-pr-create").unlink()
    resumed = run_loop(consumer, harness, tmp_path, ["--resume-run", str(path)])
    if failure == "wrong-remote-head":
        assert resumed.returncode != 0
        assert "Initial publication remote head mismatch" in resumed.stderr
    else:
        assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert consumer[3].joinpath("gh.log").read_text().count("pr create") == 1
    assert (
        consumer[3].joinpath("events.log").read_text().splitlines().count("worker") == 1
    )


@pytest.mark.parametrize("stop_after_merge", (False, True))
def test_preparation_refreshes_outputs_after_initial_base_integration(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
    stop_after_merge: bool,
) -> None:
    # The gate advances the remote once after worker validation. The next gate
    # sees that new source only after the controller integrates it.
    (consumer[0] / ".gitignore").write_text(".generated\n")
    _run_git("add", ".gitignore", cwd=consumer[0])
    _run_git("commit", "-m", "fixture: ignore generated output", cwd=consumer[0])
    _run_git("push", "origin", "main", cwd=consumer[0])
    updater = tmp_path / "advance-base.sh"
    _write_executable(
        updater,
        f"""
#!/usr/bin/env bash
set -eu
if [ ! -f "$AGENT_STATE_DIR/base-advanced" ]; then
  /usr/bin/git clone --branch main '{consumer[1]}' '{tmp_path / "updater"}' >/dev/null 2>&1
  /usr/bin/git -C '{tmp_path / "updater"}' config user.name Test
  /usr/bin/git -C '{tmp_path / "updater"}' config user.email test@example.invalid
  printf 'new-source' > '{tmp_path / "updater"}/artifact-input'
  /usr/bin/git -C '{tmp_path / "updater"}' add artifact-input
  /usr/bin/git -C '{tmp_path / "updater"}' commit -m 'fixture: source advances' >/dev/null
  /usr/bin/git -C '{tmp_path / "updater"}' push origin main >/dev/null
  touch "$AGENT_STATE_DIR/base-advanced"
fi
if [ -f artifact-input ]; then cmp artifact-input .generated; test ! -f "$AGENT_STATE_DIR/fail-initial"; fi
""".lstrip(),
    )
    if stop_after_merge:
        (consumer[3] / "fail-initial").touch()
    completed = run_loop(
        consumer,
        harness,
        tmp_path,
        ["--issues", "71"],
        preparation_hook="if [ -f artifact-input ]; then cp artifact-input .generated; fi",
        validation_hook=str(updater),
    )
    if stop_after_merge:
        assert completed.returncode != 0
        path, saved = checkpoint(tmp_path)
        assert saved["phase"] == "integrated"
        (consumer[3] / "fail-initial").unlink()
        completed = run_loop(
            consumer,
            harness,
            tmp_path,
            ["--resume-run", str(path)],
            preparation_hook="if [ -f artifact-input ]; then cp artifact-input .generated; fi",
            validation_hook=str(updater),
        )
        assert json.loads(path.read_text())["headSha"] == saved["headSha"]
        assert (
            consumer[3].joinpath("events.log").read_text().splitlines().count("worker")
            == 1
        )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    _, state = checkpoint(tmp_path)
    assert state["phase"] == "finalized"


MISMATCHED_PR_ROWS = {
    "wrong-head": ("'sha': remote_head", "'sha': '0' * 40"),
    "wrong-base": ("'base': {'ref': base_name", "'base': {'ref': 'elsewhere'"),
    "wrong-base-repo": (
        "'base': {'ref': base_name, 'repo': {'full_name': 'fixture/consumer'}}",
        "'base': {'ref': base_name, 'repo': {'full_name': 'other/consumer'}}",
    ),
}


@pytest.mark.parametrize("marker", ("pr-closed", "pr-ready", *MISMATCHED_PR_ROWS))
def test_resume_never_adopts_a_closed_ready_or_mismatched_pr(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
    marker: str,
) -> None:
    lose_pr_response_once(consumer)
    stopped = run_loop(consumer, harness, tmp_path, ["--issues", "71"])
    assert stopped.returncode != 0
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "pushed"
    if marker in MISMATCHED_PR_ROWS:
        gh = consumer[2] / "gh"
        original, mismatched = MISMATCHED_PR_ROWS[marker]
        assert original in gh.read_text()
        gh.write_text(gh.read_text().replace(original, mismatched))
    else:
        (consumer[3] / marker).touch()
    resumed = run_loop(consumer, harness, tmp_path, ["--resume-run", str(path)])
    assert resumed.returncode != 0
    assert "Existing initial PR does not match the saved publication" in resumed.stderr
    assert json.loads(path.read_text())["phase"] == "pushed"
    assert consumer[3].joinpath("gh.log").read_text().count("pr create") == 1
    events = consumer[3].joinpath("events.log").read_text().splitlines()
    assert events.count("worker") == 1
    assert "claude" not in events


def test_resume_never_adopts_a_remote_branch_without_publication_intent(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
) -> None:
    failure = consumer[3] / "fail-validation"
    failure.touch()
    gate = 'test ! -f "$AGENT_STATE_DIR/fail-validation"'
    stopped = run_loop(
        consumer, harness, tmp_path, ["--issues", "71"], validation_hook=gate
    )
    assert stopped.returncode != 0
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "worker-complete"
    # Someone else publishes the same commit under the issue branch name.
    _run_git(
        "push",
        str(consumer[1]),
        f"{state['headSha']}:refs/heads/{state['branch']}",
        cwd=Path(str(state["worktree"])),
    )
    failure.unlink()
    resumed = run_loop(
        consumer, harness, tmp_path, ["--resume-run", str(path)], validation_hook=gate
    )
    assert resumed.returncode != 0
    assert "Remote branch existed before publication intent" in resumed.stderr
    assert json.loads(path.read_text())["phase"] in ("worker-complete", "integrated")
    assert "pr create" not in consumer[3].joinpath("gh.log").read_text()


@pytest.mark.parametrize("phase", ("publishing", "pushed"))
def test_resume_honours_recorded_publication_intent(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
    phase: str,
) -> None:
    failure = consumer[3] / "fail-validation"
    failure.touch()
    gate = 'test ! -f "$AGENT_STATE_DIR/fail-validation"'
    stopped = run_loop(
        consumer, harness, tmp_path, ["--issues", "71"], validation_hook=gate
    )
    assert stopped.returncode != 0
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "worker-complete"
    head, base = str(state["headSha"]), str(state["baseSha"])
    set_phase(consumer, harness, path, "publishing", base, head)
    if phase == "publishing":
        # Interrupted after the create-only push, before the pushed checkpoint.
        _run_git(
            "push",
            str(consumer[1]),
            f"{head}:refs/heads/{state['branch']}",
            cwd=Path(str(state["worktree"])),
        )
    else:
        # The recorded push is no longer on the remote.
        set_phase(consumer, harness, path, "pushed", base, head)
    failure.unlink()
    resumed = run_loop(
        consumer, harness, tmp_path, ["--resume-run", str(path)], validation_hook=gate
    )
    gh_log = consumer[3].joinpath("gh.log").read_text()
    if phase == "publishing":
        assert resumed.returncode == 0, resumed.stderr + resumed.stdout
        assert gh_log.count("pr create") == 1
        assert json.loads(path.read_text())["phase"] == "finalized"
    else:
        assert resumed.returncode != 0
        assert "Previously pushed branch disappeared" in resumed.stderr
        assert "pr create" not in gh_log
        assert json.loads(path.read_text())["phase"] == "pushed"
    assert (
        consumer[3].joinpath("events.log").read_text().splitlines().count("worker") == 1
    )


def test_pre_worker_failure_offers_no_resume_and_batch_resume_names_the_bail(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
) -> None:
    stopped = run_loop(
        consumer, harness, tmp_path, ["--issues", "71,72"], setup_hook="exit 7"
    )
    assert stopped.returncode != 0
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "worker-running"
    assert "--resume-run" not in stopped.stderr
    batch = next((tmp_path / "logs").glob("*batch*.json"))
    resumed = run_loop(
        consumer, harness, tmp_path, ["--resume-batch", str(batch)], setup_hook="exit 7"
    )
    assert resumed.returncode != 0
    assert "Explicit bail command:" in resumed.stderr
    assert "--status bailed" in resumed.stderr
    direct = run_loop(
        consumer, harness, tmp_path, ["--resume-run", str(path)], setup_hook="exit 7"
    )
    assert direct.returncode != 0
    assert "Worker completion was not checkpointed" in direct.stderr
    assert "--resume-run" not in direct.stderr
    assert json.loads(path.read_text())["phase"] == "worker-running"
    assert json.loads(batch.read_text())["issues"][0]["status"] == "active"
    events = consumer[3] / "events.log"
    assert not events.exists() or "worker" not in events.read_text().splitlines()


def test_human_glance_stop_never_resumes_into_the_review_chain(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
) -> None:
    worker = (
        "printf 'worker\\n' >> \"$EVENT_LOG\"; printf 'note\\n' > NOTES.md; "
        "git add NOTES.md; git commit -m 'docs: note'"
    )
    stopped = run_loop(
        consumer, harness, tmp_path, ["--issues", "71"], worker_hook=worker
    )
    assert stopped.returncode != 0
    assert "needs a human glance" in stopped.stderr
    assert "--resume-run" not in stopped.stderr
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "draft-open"
    resumed = run_loop(
        consumer, harness, tmp_path, ["--resume-run", str(path)], worker_hook=worker
    )
    assert resumed.returncode != 0
    assert "requires a human glance" in resumed.stderr
    assert "--resume-run" not in resumed.stderr
    assert json.loads(path.read_text())["phase"] == "draft-open"
    assert not (consumer[3] / "pr-ready").exists()
    assert consumer[3].joinpath("events.log").read_text().splitlines().count(
        "worker"
    ) == 1
    assert "claude" not in consumer[3].joinpath("events.log").read_text().splitlines()


def test_batch_human_glance_child_names_the_bail_instead_of_relaunching(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
) -> None:
    worker = (
        "printf 'worker\\n' >> \"$EVENT_LOG\"; printf 'note\\n' > NOTES.md; "
        "git add NOTES.md; git commit -m 'docs: note'"
    )
    stopped = run_loop(
        consumer, harness, tmp_path, ["--issues", "71,72"], worker_hook=worker
    )
    assert stopped.returncode != 0
    assert "needs a human glance" in stopped.stderr
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "draft-open"
    batch = next((tmp_path / "logs").glob("*batch*.json"))
    resumed = run_loop(
        consumer, harness, tmp_path, ["--resume-batch", str(batch)], worker_hook=worker
    )
    assert resumed.returncode != 0
    assert "stopped for a human glance" in resumed.stderr
    assert "Explicit bail command:" in resumed.stderr
    assert "--status bailed" in resumed.stderr
    assert "did not resume to a safely finalized state" not in resumed.stderr
    assert json.loads(batch.read_text())["issues"][0]["status"] == "active"
    events = consumer[3].joinpath("events.log").read_text().splitlines()
    assert events.count("worker") == 1
    assert "claude" not in events


def test_initial_publication_runs_repository_hooks_unless_the_controller_pins_git(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
) -> None:
    marker = consumer[3] / "pre-push-ran"
    _write_executable(
        consumer[0] / ".git/hooks/pre-push",
        f"#!/bin/sh\ntouch '{marker}'\n",
    )
    # Stop right after the create-only push so no later push can run the hook.
    gh = consumer[2] / "gh"
    gh.write_text(
        gh.read_text().replace(
            "if os.environ.get('AGENT_PR_CREATE_FAIL'):",
            "if (state / 'fail-pr-create').exists():",
        )
    )
    stopped = run_loop(
        consumer,
        harness,
        tmp_path,
        ["--issues", "71"],
        validation_hook='touch "$AGENT_STATE_DIR/fail-pr-create"',
    )
    assert stopped.returncode != 0
    assert checkpoint(tmp_path)[1]["phase"] == "pushed", stopped.stderr
    assert marker.exists() == (harness != ".codex")


@pytest.mark.fast
@pytest.mark.parametrize("root", (".codex", ".claude", ".agents"))
def test_state_helper_binds_pr_identity_to_publication(
    root: str, tmp_path: Path
) -> None:
    helper = REPO_ROOT / root / "skills/agent-loop/scripts/agent-loop-state.py"
    head, base = "a" * 40, "b" * 40
    pr = ("--pr", "9", "--pr-url", "https://example.invalid/pr/9")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(helper), *args], capture_output=True, text=True
        )

    def create(name: str, *extra: str) -> tuple[Path, subprocess.CompletedProcess[str]]:
        state = tmp_path / name / "run-state.json"
        return state, run(
            "create", "--file", str(state), "--run-id", "run-1",
            "--repo", "example/repository", "--issue", "7",
            "--issue-title-sha256", "c" * 64, "--issue-body-sha256", "d" * 64,
            "--base-branch", "main", "--branch", "agent-loop/issue-7-run-1",
            "--worktree", str(tmp_path / "worktree"), "--log-dir", str(tmp_path / name),
            "--base-sha", base, "--head-sha", head,
            "--review-budget-seconds", "60", "--review-max-rounds", "4", *extra,
        )  # fmt: skip

    def update(state: Path, phase: str, *extra: str) -> int:
        return run(
            "update", "--file", str(state), "--phase", phase, "--round", "1",
            "--base-sha", base, "--head-sha", head, *extra,
        ).returncode  # fmt: skip

    assert create("with-pr", "--phase", "worker-running", *pr)[1].returncode != 0
    assert create("draft-without-pr", "--phase", "draft-open")[1].returncode != 0
    state, created = create("run", "--phase", "worker-running")
    assert created.returncode == 0, created.stderr
    assert update(state, "worker-complete") == 0
    # PR identity attaches only to a pushed branch, and only as a pair.
    assert run("update", "--file", str(state), "--phase", "draft-open", *pr).returncode != 0
    assert update(state, "draft-open") != 0
    assert update(state, "publishing") == 0
    assert update(state, "pushed") == 0
    assert run("update", "--file", str(state), "--phase", "draft-open", "--pr", "9").returncode != 0
    assert run("update", "--file", str(state), "--phase", "draft-open", *pr).returncode == 0
    assert update(state, "worker-complete") != 0
    assert json.loads(state.read_text())["phase"] == "draft-open"
