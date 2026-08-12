"""Failure fixtures for the non-mutating agent-loop config doctor."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / ".claude/skills/agent-loop/scripts/config-doctor.py"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "consumer"
    skill = project / ".claude/skills/agent-loop"
    scripts = skill / "scripts"
    ledger_dir = project / ".claude/skills/critique/scripts"
    scripts.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)
    shutil.copy2(ROOT / ".claude/skills/agent-loop/scripts/agent-loop-state.py", scripts)
    shutil.copy2(ROOT / ".claude/skills/agent-loop/scripts/review-push.sh", scripts)
    shutil.copy2(ROOT / ".claude/skills/critique/scripts/review-ledger.py", ledger_dir)
    shutil.copy2(
        ROOT / ".claude/skills/agent-loop/prompt.txt.template",
        skill / "prompt.txt",
    )
    shutil.copy2(
        ROOT / ".claude/skills/agent-loop/agent-loop-instructions.md.template",
        project / "agent-loop-instructions.md",
    )
    (skill / "agent-loop.config").write_text(
        "review_contract_version = 3\n"
        "codex_review_hook = deepcritique $AGENT_LOOP_PR_NUMBER; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.py write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE\n"
        "claude_review_hook = claude --effort low /deepcritique $AGENT_LOOP_PR_NUMBER; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.py write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE\n",
        encoding="utf-8",
    )
    return project


def _run(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(DOCTOR), "--project-dir", str(project), "--claude-effort", "low"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_doctor_accepts_current_contract_without_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert "config_doctor = true" in (
        ROOT / ".claude/skills/agent-loop/agent-loop.config.template"
    ).read_text(encoding="utf-8")
    assert "CONFIG_DOCTOR=true" in (
        ROOT / ".claude/skills/agent-loop/scripts/agent-loop.sh"
    ).read_text(encoding="utf-8")
    before = sorted(path.relative_to(project) for path in project.rglob("*"))
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert "compatible" in result.stdout
    assert sorted(path.relative_to(project) for path in project.rglob("*")) == before


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("review_contract_version = 3", "review_contract_version = 2", "must be 3"),
        ("write-result", "AGENT_LOOP_REVIEW_OUTCOME_FILE", "obsolete review ownership"),
        ("/deepcritique", "/deepgrill", "must invoke deepcritique"),
        ("--effort low", "--effort medium", "literal --effort low"),
        ("AGENT_LOOP_REVIEW_PUSH_HELPER", "git push", "review push helper"),
    ],
)
def test_doctor_failure_fixtures(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    config.write_text(config.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    result = _run(project)
    assert result.returncode != 0
    assert message in result.stderr


def test_doctor_rejects_worker_prompt_that_requires_masked_gh(tmp_path: Path) -> None:
    project = _project(tmp_path)
    prompt = project / ".claude/skills/agent-loop/prompt.txt"
    prompt.write_text(
        "Run gh issue view 7, then read AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY. Create a local commit; do not push.\n",
        encoding="utf-8",
    )
    result = _run(project)
    assert result.returncode != 0
    assert "require masked gh" in result.stderr


def test_doctor_rejects_worker_instructions_that_require_masked_gh(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    instructions = project / "agent-loop-instructions.md"
    instructions.write_text(
        "Read AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY, then run gh pr create.\n",
        encoding="utf-8",
    )
    result = _run(project)
    assert result.returncode != 0
    assert "require masked gh" in result.stderr


def test_doctor_rejects_incompatible_review_push_protocol(tmp_path: Path) -> None:
    project = _project(tmp_path)
    review_push = project / ".claude/skills/agent-loop/scripts/review-push.sh"
    review_push.write_text("#!/usr/bin/env bash\nprintf '2\\n'\n", encoding="utf-8")
    review_push.chmod(0o755)
    result = _run(project)
    assert result.returncode != 0
    assert "review-push protocol is incompatible" in result.stderr
