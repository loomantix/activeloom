"""Execution contracts for the agent-loop trusted Codex review launcher."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / ".codex/skills/agent-loop/scripts/run-codex-review.sh"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_repo(path: Path, files: dict[str, str]) -> str:
    _git("init", "-b", "main", str(path))
    _git("config", "user.name", "Test", cwd=path)
    _git("config", "user.email", "test@example.invalid", cwd=path)
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-m", "fixture", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def _trusted_surface(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "trusted"
    required = {
        ".codex/REVIEW_WORKFLOW.md": "trusted workflow\n",
        ".codex/references/local-review-ledger.md": "trusted ledger\n",
        ".codex/references/roles/code-reviewer.md": "trusted role\n",
        ".codex/references/roles/silent-failure-hunter.md": "trusted role\n",
        ".codex/references/roles/type-design-analyzer.md": "trusted role\n",
        ".codex/references/roles/comment-analyzer.md": "trusted role\n",
        ".codex/references/roles/pr-test-analyzer.md": "trusted role\n",
        ".codex/references/roles/security-reviewer.md": "trusted role\n",
        ".codex/skills/deepcritique/SKILL.md": "trusted deep review\n",
        ".codex/skills/critique/SKILL.md": "trusted critique\n",
        ".codex/skills/critique/scripts/review-ledger.js": "trusted helper\n",
        ".codex/skills/refactorpass/SKILL.md": "trusted refactor\n",
        ".claude/REVIEW_WORKFLOW.md": "trusted Claude workflow\n",
        ".claude/references/local-review-ledger.md": "trusted Claude ledger\n",
        ".claude/skills/deepcritique/SKILL.md": "trusted Claude deep review\n",
        ".claude/skills/critique/SKILL.md": "trusted Claude critique\n",
        ".claude/skills/critique/scripts/package.json": '{"type":"module"}\n',
        ".claude/skills/critique/scripts/review-ledger.js": "trusted Claude helper\n",
        ".claude/skills/refactorpass/SKILL.md": "trusted Claude refactor\n",
        "nested/AGENTS.md": "trusted nested instructions\n",
        "AGENTS.md": "trusted Codex instructions\n",
        "CLAUDE.md": "trusted Claude instructions\n",
    }
    _commit_repo(repo, required)
    return repo, repo / ".codex"


def _issue_worktree(tmp_path: Path, trusted_repo: Path) -> tuple[Path, str, str]:
    issue = tmp_path / "issue"
    base = _git("rev-parse", "main", cwd=trusted_repo)
    _git("worktree", "add", "-b", "issue", str(issue), cwd=trusted_repo)
    for relative, content in {
        "result.txt": "review target\n",
        "AGENTS.md": "worker-authored instructions\n",
        "CLAUDE.md": "worker-authored instructions\n",
        ".codex/skills/deepcritique/SKILL.md": "worker-authored skill\n",
    }.items():
        target = issue / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git("add", ".", cwd=issue)
    _git("commit", "-m", "worker changes", cwd=issue)
    return issue, base, _git("rev-parse", "HEAD", cwd=issue)


def _fake_cli(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "prompt = sys.argv[-1]\n"
        "skill_path = pathlib.Path(prompt.split('Read ', 1)[1].split(' completely', 1)[0])\n"
        "snapshot_root = skill_path.parents[3]\n"
        "pathlib.Path(os.environ['REVIEW_INVOCATION']).write_text(json.dumps({\n"
        "  'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "  'additional_instructions': os.environ.get('CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD'),\n"
        "  'auto_memory': os.environ.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY'),\n"
        "  'coverage_env': {key: os.environ.get(key) for key in (\n"
        "    'COVERAGE_PROCESS_START', 'COV_CORE_SOURCE', 'COV_CORE_CONFIG',\n"
        "    'COV_CORE_DATAFILE', 'COV_CORE_BRANCH'\n"
        "  )},\n"
        "  'skill_content': skill_path.read_text(),\n"
        "  'nested_instructions': (snapshot_root / 'nested/AGENTS.md').read_text()\n"
        "}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _delegating_git(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "for argument in \"$@\"; do\n"
        "  if [ \"$argument\" = \"${AGENT_TEST_GIT_FAILURE}\" ]; then\n"
        "    if [ \"$argument\" = hash-object ]; then printf '%040d\\n' 0; exit 0; fi\n"
        "    exit 86\n"
        "  fi\n"
        "done\n"
        "exec \"$AGENT_TEST_REAL_GIT\" \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _environment(
    tmp_path: Path,
    *,
    engine: str,
    trusted: Path,
    base: str,
    head: str,
    cli: Path,
) -> dict[str, str]:
    return {
        **os.environ,
        "AGENT_LOOP_REVIEW_ENGINE": engine,
        "AGENT_LOOP_REVIEW_BASE_SHA": base,
        "AGENT_LOOP_REVIEW_ROUND": "2",
        "AGENT_LOOP_PR_NUMBER": "17",
        "AGENT_LOOP_PR_HEAD_SHA": head,
        "AGENT_LOOP_REVIEW_RESULT_FILE": str(tmp_path / "result.json"),
        "AGENT_LOOP_REVIEW_PUSH_HELPER": str(tmp_path / "review-push.sh"),
        "AGENT_LOOP_TRUSTED_REPO_ROOT": str(trusted.parent),
        "AGENT_LOOP_TRUSTED_BASE_REF": "main",
        "AGENT_LOOP_REVIEW_BIN_SHA256": hashlib.sha256(cli.read_bytes()).hexdigest(),
        "REVIEW_INVOCATION": str(tmp_path / "invocation.json"),
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
        "COVERAGE_PROCESS_START": "fixture",
        "COV_CORE_SOURCE": "fixture",
        "COV_CORE_CONFIG": "fixture",
        "COV_CORE_DATAFILE": "fixture",
        "COV_CORE_BRANCH": "fixture",
        "TMPDIR": str(tmp_path / "issue"),
        f"{engine.upper()}_REVIEW_CLI": str(cli),
    }


def _run_launcher(
    issue: Path, engine: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LAUNCHER), "--engine", engine],
        cwd=issue,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_launcher_runs_both_engines_from_an_empty_root_with_trusted_guidance(
    tmp_path: Path,
) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)

    codex = _fake_cli(tmp_path / "codex")
    result = _run_launcher(
        issue,
        "codex",
        _environment(
            tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=codex
        ),
    )
    assert result.returncode == 0, result.stderr
    invocation = json.loads((tmp_path / "invocation.json").read_text(encoding="utf-8"))
    argv = invocation["argv"]
    assert argv[:2] == ["exec", "--dangerously-bypass-approvals-and-sandbox"]
    assert "--skip-git-repo-check" in argv
    launch_root = argv[argv.index("-C") + 1]
    assert launch_root not in {str(issue), str(trusted_repo)}
    assert not Path(launch_root).exists()
    assert argv[argv.index("--add-dir") + 1] == str(issue)
    prompt = argv[-1]
    assert "/trusted/.codex/skills/deepcritique/SKILL.md" in prompt
    assert "round 2 (adversarial)" in prompt
    assert str(issue) in prompt
    assert "worker-authored" not in prompt
    assert set(invocation["coverage_env"].values()) == {None}
    assert invocation["skill_content"] == "trusted deep review\n"
    assert invocation["nested_instructions"] == "trusted nested instructions\n"

    claude = _fake_cli(tmp_path / "claude")
    result = _run_launcher(
        issue,
        "claude",
        _environment(
            tmp_path, engine="claude", trusted=trusted, base=base, head=head, cli=claude
        ),
    )
    assert result.returncode == 0, result.stderr
    invocation = json.loads((tmp_path / "invocation.json").read_text(encoding="utf-8"))
    assert invocation["cwd"] not in {str(issue), str(trusted_repo)}
    assert not Path(invocation["cwd"]).exists()
    argv = invocation["argv"]
    assert argv[:2] == ["--effort", "low"]
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--add-dir") + 1] == str(issue)
    assert "/trusted/.claude/skills/deepcritique/SKILL.md" in argv[-1]
    assert invocation["additional_instructions"] is None
    assert invocation["auto_memory"] == "1"
    assert set(invocation["coverage_env"].values()) == {None}
    assert invocation["skill_content"] == "trusted Claude deep review\n"
    assert invocation["nested_instructions"] == "trusted nested instructions\n"


def test_launcher_uses_base_blobs_when_the_source_checkout_is_modified(
    tmp_path: Path,
) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    (trusted / "skills/deepcritique/SKILL.md").write_text(
        "modified\n", encoding="utf-8"
    )
    cli = _fake_cli(tmp_path / "codex")

    result = _run_launcher(
        issue,
        "codex",
        _environment(
            tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
        ),
    )
    assert result.returncode == 0, result.stderr
    invocation = json.loads((tmp_path / "invocation.json").read_text(encoding="utf-8"))
    assert str(trusted) not in invocation["argv"][-1]
    assert "/trusted/.codex/skills/deepcritique/SKILL.md" in invocation["argv"][-1]
    assert invocation["skill_content"] == "trusted deep review\n"
    assert invocation["skill_content"] != "modified\n"
    assert invocation["nested_instructions"] == "trusted nested instructions\n"


def test_launcher_rejects_a_trusted_ref_that_moved_from_the_pinned_base(
    tmp_path: Path,
) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    cli = _fake_cli(tmp_path / "codex")
    environment = _environment(
        tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
    )
    environment["AGENT_LOOP_TRUSTED_BASE_REF"] = "issue"

    result = _run_launcher(issue, "codex", environment)
    assert result.returncode != 0
    assert "no longer resolves to AGENT_LOOP_REVIEW_BASE_SHA" in result.stderr
    assert not (tmp_path / "invocation.json").exists()


def test_launcher_rejects_an_empty_repository_instruction_surface(
    tmp_path: Path,
) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    _git("rm", "AGENTS.md", "CLAUDE.md", "nested/AGENTS.md", cwd=trusted_repo)
    _git("commit", "-m", "remove repository instructions", cwd=trusted_repo)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    cli = _fake_cli(tmp_path / "codex")

    result = _run_launcher(
        issue,
        "codex",
        _environment(
            tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
        ),
    )

    assert result.returncode != 0
    assert "pinned repository instruction surface is empty" in result.stderr
    assert not (tmp_path / "invocation.json").exists()


def test_launcher_ignores_a_symlinked_working_tree_instruction(tmp_path: Path) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    skill = trusted / "skills/deepcritique/SKILL.md"
    skill.unlink()
    skill.symlink_to(trusted / "REVIEW_WORKFLOW.md")
    cli = _fake_cli(tmp_path / "codex")

    result = _run_launcher(
        issue,
        "codex",
        _environment(
            tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
        ),
    )
    assert result.returncode == 0, result.stderr
    invocation = json.loads((tmp_path / "invocation.json").read_text(encoding="utf-8"))
    assert invocation["skill_content"] == "trusted deep review\n"
    assert invocation["skill_content"] != "trusted workflow\n"


def test_launcher_rejects_a_reviewer_binary_that_changed_after_pinning(
    tmp_path: Path,
) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    cli = _fake_cli(tmp_path / "codex")
    environment = _environment(
        tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
    )
    environment["AGENT_LOOP_REVIEW_BIN"] = str(cli)
    environment["AGENT_LOOP_REVIEW_BIN_SHA256"] = "0" * 64

    result = _run_launcher(issue, "codex", environment)
    assert result.returncode != 0
    assert "reviewer executable changed after startup" in result.stderr


def test_launcher_rejects_failed_object_graph_verification(tmp_path: Path) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    cli = _fake_cli(tmp_path / "codex")
    environment = _environment(
        tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
    )
    real_git = shutil.which("git")
    assert real_git is not None
    environment.update(
        {
            "AGENT_LOOP_REAL_GIT": str(_delegating_git(tmp_path / "git")),
            "AGENT_TEST_REAL_GIT": real_git,
            "AGENT_TEST_GIT_FAILURE": "fsck",
        }
    )

    result = _run_launcher(issue, "codex", environment)

    assert result.returncode != 0
    assert "pinned base object graph failed integrity verification" in result.stderr
    assert not (tmp_path / "invocation.json").exists()


def test_launcher_rejects_materialized_blob_oid_mismatch(tmp_path: Path) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    cli = _fake_cli(tmp_path / "codex")
    environment = _environment(
        tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
    )
    real_git = shutil.which("git")
    assert real_git is not None
    environment.update(
        {
            "AGENT_LOOP_REAL_GIT": str(_delegating_git(tmp_path / "git")),
            "AGENT_TEST_REAL_GIT": real_git,
            "AGENT_TEST_GIT_FAILURE": "hash-object",
        }
    )

    result = _run_launcher(issue, "codex", environment)

    assert result.returncode != 0
    assert "trusted guidance blob failed integrity verification" in result.stderr
    assert not (tmp_path / "invocation.json").exists()
