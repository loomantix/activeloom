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


def checkpoint(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    """Return the first issue's durable checkpoint."""
    path = next((tmp_path / "logs").glob("*issue-71*/run-state.json"))
    return path, json.loads(path.read_text())


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


def test_preparation_failure_and_head_drift_never_publish(
    consumer: tuple[Path, Path, Path, Path],
    harness: str,
    tmp_path: Path,
) -> None:
    stopped = run_loop(
        consumer, harness, tmp_path, ["--issues", "71"], preparation_hook="exit 42"
    )
    assert stopped.returncode != 0
    path, state = checkpoint(tmp_path)
    assert state["phase"] == "worker-complete"
    assert "pr create" not in consumer[3].joinpath("gh.log").read_text()
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
    assert "Pre-publication head changed" in resumed.stderr
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
        gh.write_text(
            gh.read_text().replace(
                "if '--json number' in joined:\n        print('1')",
                "if '--json number' in joined:\n        if not (state / 'response-lost').exists():\n            (state / 'response-lost').touch()\n            sys.exit(1)\n        print('1')",
            )
        )
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
  /usr/bin/git clone '{consumer[1]}' '{tmp_path / "updater"}' >/dev/null 2>&1
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
