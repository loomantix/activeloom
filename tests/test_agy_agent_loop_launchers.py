"""Execution contracts for the agent-loop Agy launchers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / ".agents/skills/agent-loop/scripts/run-agy-worker.sh"
REVIEW = ROOT / ".agents/skills/agent-loop/scripts/run-agy-review.sh"
SHA = "a" * 40


def _subprocess_env() -> dict[str, str]:
    # These launchers intentionally execute standalone Python helpers and fake
    # CLIs. Pytest-cov exports COV_CORE_* for worker collection; inheriting it
    # makes those subprocesses emit statement-only data that cannot be combined
    # with this repository's branch coverage.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COV_CORE_")
    }


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_agy(tmp_path: Path, status: str = "SUCCESS") -> tuple[Path, Path]:
    argv_file = tmp_path / "agy-argv.json"
    agy = _executable(
        tmp_path / "agy",
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['AGY_ARGV_FILE'], 'w', encoding='utf-8') as out:\n"
        "    json.dump(sys.argv[1:], out)\n"
        f"print(json.dumps({{'status': {status!r}, 'response': 'complete'}}))\n",
    )
    return agy, argv_file


def test_worker_uses_selected_model_and_requires_success_status(tmp_path: Path) -> None:
    agy, argv_file = _fake_agy(tmp_path)
    env = {
        **_subprocess_env(),
        "AGY_CLI": str(agy),
        "AGY_ARGV_FILE": str(argv_file),
        "AGENT_LOOP_PROMPT": "Implement the bounded issue.",
    }
    result = subprocess.run(
        [str(WORKER), "--model", "gemini-primary"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "gemini-primary"
    assert "--disable-slash-commands" in argv

    failing_agy, _ = _fake_agy(tmp_path, status="ERROR")
    result = subprocess.run(
        [str(WORKER), "--model", "gemini-primary"],
        cwd=tmp_path,
        env={**env, "AGY_CLI": str(failing_agy)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "status 'ERROR'" in result.stderr


def _trusted_surface(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "trusted"
    surface = repo / ".agents"
    required = [
        "REVIEW_WORKFLOW.md",
        "references/local-review-ledger.md",
        "references/roles/code-reviewer.md",
        "references/roles/silent-failure-hunter.md",
        "references/roles/type-design-analyzer.md",
        "references/roles/comment-analyzer.md",
        "references/roles/pr-test-analyzer.md",
        "references/roles/security-reviewer.md",
        "skills/deepcritique/SKILL.md",
        "skills/critique/SKILL.md",
        "skills/critique/scripts/review-ledger.js",
        "skills/refactorpass/SKILL.md",
    ]
    for relative in required:
        path = surface / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("trusted\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", ".agents"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "trusted surface"], check=True, capture_output=True)
    return repo, surface


def test_review_uses_truthful_engine_trusted_surface_and_fail_closed_json(
    tmp_path: Path,
) -> None:
    _, surface = _trusted_surface(tmp_path)
    issue = tmp_path / "issue"
    issue.mkdir()
    agy, argv_file = _fake_agy(tmp_path)
    env = {
        **_subprocess_env(),
        "AGY_CLI": str(agy),
        "AGY_ARGV_FILE": str(argv_file),
        "AGENT_LOOP_REVIEW_ENGINE": "gemini",
        "AGENT_LOOP_REVIEW_BASE_SHA": SHA,
        "AGENT_LOOP_REVIEW_ROUND": "2",
        "AGENT_LOOP_PR_NUMBER": "17",
        "AGENT_LOOP_PR_HEAD_SHA": SHA,
        "AGENT_LOOP_REVIEW_RESULT_FILE": str(tmp_path / "review-result.json"),
        "AGENT_LOOP_REVIEW_PUSH_HELPER": str(tmp_path / "review-push.sh"),
        "AGENT_LOOP_TRUSTED_AGENTS_ROOT": str(surface),
        "AGENT_LOOP_TRUSTED_BASE_REF": "HEAD",
    }
    result = subprocess.run(
        [str(REVIEW), "--engine", "gemini"],
        cwd=issue,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "gemini-3.7-flash-high"
    assert argv[argv.index("--add-dir") + 1] == str(surface)
    assert "--disable-slash-commands" in argv
    prompt = argv[argv.index("--print") + 1]
    assert str(surface / "skills/deepcritique/SKILL.md") in prompt
    assert "engine gemini" in prompt

    result = subprocess.run(
        [str(REVIEW), "--engine", "claude"],
        cwd=issue,
        env={**env, "AGENT_LOOP_REVIEW_ENGINE": "claude"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    assert argv[argv.index("--effort") + 1] == "low"
    assert "engine claude" in argv[argv.index("--print") + 1]

    failing_agy, _ = _fake_agy(tmp_path, status="CANCELED")
    result = subprocess.run(
        [str(REVIEW), "--engine", "gemini"],
        cwd=issue,
        env={**env, "AGY_CLI": str(failing_agy)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "status 'CANCELED'" in result.stderr


def test_review_rejects_modified_trusted_surface_before_agy(tmp_path: Path) -> None:
    _, surface = _trusted_surface(tmp_path)
    (surface / "skills/deepcritique/SKILL.md").write_text("modified\n", encoding="utf-8")
    issue = tmp_path / "issue"
    issue.mkdir()
    marker = tmp_path / "agy-called"
    agy = _executable(tmp_path / "agy", f"#!/usr/bin/env bash\ntouch {marker}\n")
    env = {
        **_subprocess_env(),
        "AGY_CLI": str(agy),
        "AGENT_LOOP_REVIEW_ENGINE": "gemini",
        "AGENT_LOOP_REVIEW_BASE_SHA": SHA,
        "AGENT_LOOP_REVIEW_ROUND": "1",
        "AGENT_LOOP_PR_NUMBER": "17",
        "AGENT_LOOP_PR_HEAD_SHA": SHA,
        "AGENT_LOOP_REVIEW_RESULT_FILE": str(tmp_path / "result.json"),
        "AGENT_LOOP_REVIEW_PUSH_HELPER": str(tmp_path / "push.sh"),
        "AGENT_LOOP_TRUSTED_AGENTS_ROOT": str(surface),
        "AGENT_LOOP_TRUSTED_BASE_REF": "HEAD",
    }
    result = subprocess.run(
        [str(REVIEW), "--engine", "gemini"],
        cwd=issue,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "differs from the fetched base" in result.stderr
    assert not marker.exists()
