"""Failure fixtures for the non-mutating agent-loop config doctor."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / ".codex/skills/agent-loop/scripts/config-doctor.py"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "consumer"
    skill = project / ".codex/skills/agent-loop"
    scripts = skill / "scripts"
    ledger_dir = project / ".codex/skills/critique/scripts"
    ready_dir = project / ".codex/skills/issues/scripts"
    scripts.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)
    ready_dir.mkdir(parents=True)
    shutil.copy2(ROOT / ".codex/skills/agent-loop/scripts/agent-loop-state.py", scripts)
    shutil.copy2(ROOT / ".codex/skills/agent-loop/scripts/agent-loop.sh", scripts)
    shutil.copy2(ROOT / ".codex/skills/agent-loop/scripts/review-push.sh", scripts)
    shutil.copy2(ROOT / ".codex/skills/agent-loop/scripts/run-codex-review.sh", scripts)
    shutil.copy2(ROOT / ".codex/skills/agent-loop/scripts/hook-git-guard", scripts)
    shutil.copy2(ROOT / ".codex/skills/agent-loop/scripts/hook-gh-guard", scripts)
    shutil.copy2(
        ROOT / ".codex/skills/agent-loop/scripts/process-supervisor.py", scripts
    )
    shutil.copy2(ROOT / ".codex/skills/agent-loop/scripts/config-doctor.py", scripts)
    shutil.copy2(ROOT / ".codex/skills/critique/scripts/review-ledger.js", ledger_dir)
    shutil.copy2(ROOT / ".codex/skills/issues/scripts/ready.py", ready_dir)
    # See tests/test_agent_loop.py: sync ships the sibling ESM manifest, and a
    # CommonJS consumer root is the context that needs it.
    shutil.copy2(ROOT / ".codex/skills/critique/scripts/package.json", ledger_dir)
    claude_ledger_dir = project / ".claude/skills/critique/scripts"
    claude_ledger_dir.mkdir(parents=True)
    shutil.copy2(
        ROOT / ".codex/skills/critique/scripts/review-ledger.js", claude_ledger_dir
    )
    shutil.copy2(
        ROOT / ".codex/skills/critique/scripts/package.json", claude_ledger_dir
    )
    (project / "package.json").write_text(
        '{"name": "fixture-consumer", "private": true, "type": "commonjs"}\n',
        encoding="utf-8",
    )
    (skill / "prompt.txt").write_text(
        "Read AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY. Create a local commit only; do not push.\n",
        encoding="utf-8",
    )
    (project / "agent-loop-instructions.md").write_text(
        "The worker reads AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY.\n",
        encoding="utf-8",
    )
    (skill / "agent-loop.config").write_text(
        "review_contract_version = 4\n"
        'codex_review_hook = "$AGENT_LOOP_CODEX_REVIEW_LAUNCHER" --engine codex\n'
        'claude_review_hook = "$AGENT_LOOP_CODEX_REVIEW_LAUNCHER" --engine claude\n',
        encoding="utf-8",
    )
    for relative in (
        ".codex/REVIEW_WORKFLOW.md",
        ".codex/references/local-review-ledger.md",
        ".codex/skills/deepcritique/SKILL.md",
        ".codex/skills/critique/SKILL.md",
        ".codex/skills/critique/scripts/review-ledger.js",
        ".codex/skills/refactorpass/SKILL.md",
        ".claude/REVIEW_WORKFLOW.md",
        ".claude/references/local-review-ledger.md",
        ".claude/skills/deepcritique/SKILL.md",
        ".claude/skills/critique/SKILL.md",
        ".claude/skills/critique/scripts/package.json",
        ".claude/skills/critique/scripts/review-ledger.js",
        ".claude/skills/refactorpass/SKILL.md",
    ):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("trusted native review file\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=project,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=project, check=True)
    return project


def _run(project: Path) -> subprocess.CompletedProcess[str]:
    real_git = shutil.which("git")
    assert real_git is not None
    env = dict(os.environ)
    env["AGENT_LOOP_REAL_GIT"] = real_git
    return subprocess.run(
        [
            "python3",
            str(DOCTOR),
            "--project-dir",
            str(project),
            "--claude-effort",
            "low",
            "--base-ref",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_doctor_requires_the_controller_resolved_git(tmp_path: Path) -> None:
    project = _project(tmp_path)
    env = dict(os.environ)
    env.pop("AGENT_LOOP_REAL_GIT", None)

    result = subprocess.run(
        [
            "python3",
            str(DOCTOR),
            "--project-dir",
            str(project),
            "--claude-effort",
            "low",
            "--base-ref",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "trusted Git executable is unavailable" in result.stderr


def test_doctor_accepts_current_contract_without_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = sorted(path.relative_to(project) for path in project.rglob("*"))
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert "compatible" in result.stdout
    assert sorted(path.relative_to(project) for path in project.rglob("*")) == before


def test_doctor_retains_contract_v3_hook_compatibility(tmp_path: Path) -> None:
    project = _project(tmp_path)
    config = project / ".codex/skills/agent-loop/agent-loop.config"
    config.write_text(
        "review_contract_version = 3\n"
        'codex_review_hook = deepcritique "$AGENT_LOOP_PR_NUMBER"; '
        '"$AGENT_LOOP_REVIEW_PUSH_HELPER"; node review-ledger.js write-result '
        '"$AGENT_LOOP_REVIEW_RESULT_FILE"\n'
        "claude_review_hook = claude --effort low -p deepcritique; "
        '"$AGENT_LOOP_REVIEW_PUSH_HELPER"; node review-ledger.js write-result '
        '"$AGENT_LOOP_REVIEW_RESULT_FILE"\n',
        encoding="utf-8",
    )
    result = _run(project)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/skills/deepcritique/SKILL.md",
        ".claude/skills/critique/scripts/package.json",
        ".codex/REVIEW_WORKFLOW.md",
    ],
)
def test_doctor_rejects_missing_required_file_in_pinned_base(
    tmp_path: Path, relative: str
) -> None:
    project = _project(tmp_path)
    subprocess.run(
        ["git", "rm", relative],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "remove Claude review skill"],
        cwd=project,
        check=True,
    )

    result = _run(project)

    assert result.returncode != 0
    assert "pinned base review surface is missing" in result.stderr


def test_doctor_rejects_an_incompatible_pinned_claude_ledger(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ledger = project / ".claude/skills/critique/scripts/review-ledger.js"
    ledger.write_text("throw new Error('incompatible fixture');\n", encoding="utf-8")
    subprocess.run(["git", "add", str(ledger)], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "break Claude review ledger"],
        cwd=project,
        check=True,
    )

    result = _run(project)

    assert result.returncode != 0
    assert "Claude review ledger compatibility query failed" in result.stderr


def test_doctor_rejects_preexisting_launcher_drift(tmp_path: Path) -> None:
    project = _project(tmp_path)
    launcher = project / ".codex/skills/agent-loop/scripts/run-codex-review.sh"
    launcher.write_text(
        launcher.read_text(encoding="utf-8") + "\n# pre-existing drift\n",
        encoding="utf-8",
    )

    result = _run(project)

    assert result.returncode != 0
    assert "review launcher differs from the pinned base blob" in result.stderr


def test_doctor_rejects_preexisting_review_tool_drift(tmp_path: Path) -> None:
    project = _project(tmp_path)
    marker = tmp_path / "drifted-helper-ran"
    helper = project / ".codex/skills/agent-loop/scripts/review-push.sh"
    helper.write_text(
        f"#!/usr/bin/env bash\ntouch {marker}\nprintf '1\\n'\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    result = _run(project)

    assert result.returncode != 0
    assert "review tool differs from the pinned base blob" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("echo 4; exit 0", "echo 5; exit 0", "incompatible with contract v4"),
        (
            "CLAUDE_EFFORT_POLICY=low",
            "CLAUDE_EFFORT_POLICY=high",
            "pin Claude effort low",
        ),
    ],
)
def test_doctor_rejects_incompatible_launcher_contract(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    project = _project(tmp_path)
    launcher = project / ".codex/skills/agent-loop/scripts/run-codex-review.sh"
    launcher.write_text(
        launcher.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8"
    )
    subprocess.run(["git", "add", str(launcher)], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "change launcher contract"],
        cwd=project,
        check=True,
    )
    result = _run(project)
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "review_contract_version = 4",
            "review_contract_version = 2",
            "must be 3 or 4",
        ),
        ("--engine codex", "--engine claude", "contract-v4 review launcher"),
        ("--engine claude", "--engine codex", "contract-v4 review launcher"),
        (
            "AGENT_LOOP_CODEX_REVIEW_LAUNCHER",
            "AGENT_LOOP_OTHER_REVIEW_LAUNCHER",
            "contract-v4 review launcher",
        ),
    ],
)
def test_doctor_failure_fixtures(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    project = _project(tmp_path)
    config = project / ".codex/skills/agent-loop/agent-loop.config"
    config.write_text(
        config.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
    )
    result = _run(project)
    assert result.returncode != 0
    assert message in result.stderr


def test_doctor_rejects_worker_prompt_that_requires_masked_gh(tmp_path: Path) -> None:
    project = _project(tmp_path)
    prompt = project / ".codex/skills/agent-loop/prompt.txt"
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
    review_push = project / ".codex/skills/agent-loop/scripts/review-push.sh"
    review_push.write_text("#!/usr/bin/env bash\nprintf '2\\n'\n", encoding="utf-8")
    review_push.chmod(0o755)
    subprocess.run(["git", "add", str(review_push)], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "change review push protocol"],
        cwd=project,
        check=True,
    )
    result = _run(project)
    assert result.returncode != 0
    assert "review-push protocol is incompatible" in result.stderr


def _git_shim(tmp_path: Path, real_git: str) -> Path:
    """A PATH `git` that lies about `hash-object` and delegates everything else.

    The pinned-blob checks compare a base-tree OID read with the trusted binary
    against the working file's OID. If that second read goes through PATH, a
    shim like this one picks the value the comparison is made against.
    """
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '    if [ "$arg" = hash-object ]; then\n'
        "        printf '%s\\n' "
        "'0000000000000000000000000000000000000000'\n"
        "        exit 0\n"
        "    fi\n"
        "done\n"
        f'exec {real_git} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim_dir


def test_doctor_pins_review_surface_with_the_trusted_git_not_path(
    tmp_path: Path,
) -> None:
    """A shimmed PATH `git` must not be able to steer the pinned-blob checks.

    `agent-loop.sh` exports AGENT_LOOP_REAL_GIT precisely so the doctor cannot
    be steered by PATH. A hand-built `git` argv would take the shim's OID here
    and either clear a tampered surface or block an untouched one.
    """
    real_git = shutil.which("git")
    assert real_git is not None
    project = _project(tmp_path)
    shim_dir = _git_shim(tmp_path, real_git)

    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    env["AGENT_LOOP_REAL_GIT"] = real_git

    result = subprocess.run(
        [
            "python3",
            str(DOCTOR),
            "--project-dir",
            str(project),
            "--claude-effort",
            "low",
            "--base-ref",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "differs from the pinned base blob" not in result.stderr
