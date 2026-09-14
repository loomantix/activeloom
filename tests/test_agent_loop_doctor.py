"""Failure fixtures for the non-mutating agent-loop config doctor."""

from __future__ import annotations

import os
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
    shutil.copy2(ROOT / ".claude/skills/critique/scripts/review-ledger.js", ledger_dir)
    # See tests/test_agent_loop.py: sync ships the sibling ESM manifest, and a
    # CommonJS consumer root is the context that needs it.
    shutil.copy2(ROOT / ".claude/skills/critique/scripts/package.json", ledger_dir)
    (project / "package.json").write_text(
        '{"name": "fixture-consumer", "private": true, "type": "commonjs"}\n',
        encoding="utf-8",
    )
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
        "codex_review_hook = deepcritique $AGENT_LOOP_PR_NUMBER; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE\n"
        "claude_review_hook = claude --effort low /deepcritique $AGENT_LOOP_PR_NUMBER; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE\n",
        encoding="utf-8",
    )
    return project


def _run(
    project: Path, *, path_stubs: tuple[str, ...] = ("deepcritique", "claude")
) -> subprocess.CompletedProcess[str]:
    # The doctor resolves each review hook's program on PATH. Stub the fixture
    # hooks' programs outside the project tree so the no-mutation assertion and
    # the resolution check both hold on a machine without those CLIs.
    # PATH is exactly that directory: a developer box has the real CLIs
    # installed, and inheriting them would make the missing-CLI cases pass
    # for the wrong reason.
    bin_dir = project.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in bin_dir.iterdir():
        name.unlink()
    for command in ("bash", "env", "node", "python3"):
        executable = shutil.which(command)
        assert executable is not None
        (bin_dir / command).symlink_to(executable)
    for name in path_stubs:
        stub = bin_dir / name
        stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    return subprocess.run(
        ["python3", str(DOCTOR), "--project-dir", str(project), "--claude-effort", "low"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
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


def test_doctor_fails_before_claim_when_a_reviewer_cli_is_missing(tmp_path: Path) -> None:
    # Without this check a run claimed the issue, ran the worker, pushed, and
    # opened the draft PR before discovering the missing CLI at its review leg.
    project = _project(tmp_path)
    result = _run(project, path_stubs=("deepcritique",))
    assert result.returncode != 0
    assert "claude_review_hook invokes 'claude', which is not installed on PATH" in result.stderr


@pytest.mark.parametrize(
    ("hook_prefix", "stubs", "expect_failure"),
    [
        # A leading environment assignment is skipped; the program after it is resolved.
        ("FOO=1 claude", ("deepcritique", "claude"), False),
        ("FOO=1 claude", ("deepcritique",), True),
        # Shell syntax cannot be resolved statically and is left alone.
        ("if true; then claude", ("deepcritique",), False),
        (": && claude", ("deepcritique",), False),
    ],
)
def test_doctor_resolves_the_first_program_word_only(
    tmp_path: Path, hook_prefix: str, stubs: tuple[str, ...], expect_failure: bool
) -> None:
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    text = config.read_text(encoding="utf-8").replace(
        "claude_review_hook = claude", f"claude_review_hook = {hook_prefix}"
    )
    config.write_text(text, encoding="utf-8")
    result = _run(project, path_stubs=stubs)
    assert (result.returncode != 0) == expect_failure, result.stderr


def test_doctor_warns_when_a_codex_exec_hook_leaves_stdin_open(tmp_path: Path) -> None:
    # The wrapper redirects stdin itself; the warning protects the same hook
    # string when it is pasted and run outside the wrapper.
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    open_stdin = config.read_text(encoding="utf-8").replace(
        "codex_review_hook = deepcritique", "codex_review_hook = codex exec deepcritique"
    )
    config.write_text(open_stdin, encoding="utf-8")
    result = _run(project, path_stubs=("codex", "claude"))
    assert result.returncode == 0, result.stderr
    assert "warning: codex_review_hook runs codex exec without '</dev/null'" in result.stderr

    closed = open_stdin.replace(
        "--result-file $AGENT_LOOP_REVIEW_RESULT_FILE\nclaude_review_hook",
        "--result-file $AGENT_LOOP_REVIEW_RESULT_FILE </dev/null\nclaude_review_hook",
    )
    assert closed != open_stdin
    config.write_text(closed, encoding="utf-8")
    result = _run(project, path_stubs=("codex", "claude"))
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
