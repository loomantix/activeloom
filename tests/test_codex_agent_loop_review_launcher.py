"""Execution contracts for the agent-loop trusted Codex review launcher."""

from __future__ import annotations

import json
import os
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
        "pathlib.Path(os.environ['REVIEW_INVOCATION']).write_text(json.dumps({\n"
        "  'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "  'additional_instructions': os.environ.get('CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD'),\n"
        "  'auto_memory': os.environ.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY')\n"
        "}), encoding='utf-8')\n",
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
        "AGENT_LOOP_TRUSTED_CODEX_ROOT": str(trusted),
        "AGENT_LOOP_TRUSTED_BASE_REF": "main",
        "REVIEW_INVOCATION": str(tmp_path / "invocation.json"),
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
        "TMPDIR": str(tmp_path / "issue"),
        f"{engine.upper()}_REVIEW_CLI": str(cli),
    }


def test_launcher_runs_both_engines_from_an_empty_root_with_trusted_guidance(
    tmp_path: Path,
) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)

    codex = _fake_cli(tmp_path / "codex")
    result = subprocess.run(
        [str(LAUNCHER), "--engine", "codex"],
        cwd=issue,
        env=_environment(
            tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=codex
        ),
        capture_output=True,
        text=True,
        check=False,
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
    assert str(trusted / "skills/deepcritique/SKILL.md") in prompt
    assert str(issue) in prompt
    assert "worker-authored" not in prompt

    claude = _fake_cli(tmp_path / "claude")
    result = subprocess.run(
        [str(LAUNCHER), "--engine", "claude"],
        cwd=issue,
        env=_environment(
            tmp_path, engine="claude", trusted=trusted, base=base, head=head, cli=claude
        ),
        capture_output=True,
        text=True,
        check=False,
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
    assert str(trusted / "skills/deepcritique/SKILL.md") in argv[-1]
    assert invocation["additional_instructions"] is None
    assert invocation["auto_memory"] == "1"


def test_launcher_rejects_a_modified_trusted_surface_before_review(
    tmp_path: Path,
) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    (trusted / "skills/deepcritique/SKILL.md").write_text(
        "modified\n", encoding="utf-8"
    )
    marker = tmp_path / "called"
    cli = tmp_path / "codex"
    cli.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
    cli.chmod(0o755)

    result = subprocess.run(
        [str(LAUNCHER), "--engine", "codex"],
        cwd=issue,
        env=_environment(
            tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "differs from the fetched base" in result.stderr
    assert not marker.exists()


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

    result = subprocess.run(
        [str(LAUNCHER), "--engine", "codex"],
        cwd=issue,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no longer resolves to AGENT_LOOP_REVIEW_BASE_SHA" in result.stderr
    assert not (tmp_path / "invocation.json").exists()


def test_launcher_rejects_a_symlinked_required_instruction(tmp_path: Path) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    skill = trusted / "skills/deepcritique/SKILL.md"
    skill.unlink()
    skill.symlink_to(trusted / "REVIEW_WORKFLOW.md")
    cli = _fake_cli(tmp_path / "codex")

    result = subprocess.run(
        [str(LAUNCHER), "--engine", "codex"],
        cwd=issue,
        env=_environment(
            tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "contains a symlink" in result.stderr
    assert not (tmp_path / "invocation.json").exists()


def test_launcher_rejects_a_reviewer_mutation_of_the_trusted_surface(
    tmp_path: Path,
) -> None:
    trusted_repo, trusted = _trusted_surface(tmp_path)
    issue, base, head = _issue_worktree(tmp_path, trusted_repo)
    cli = tmp_path / "codex"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib\n"
        "pathlib.Path(os.environ['MUTATE_TRUSTED_PATH']).write_text('mutated\\n')\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    environment = _environment(
        tmp_path, engine="codex", trusted=trusted, base=base, head=head, cli=cli
    )
    environment["MUTATE_TRUSTED_PATH"] = str(
        trusted / "skills/deepcritique/SKILL.md"
    )

    result = subprocess.run(
        [str(LAUNCHER), "--engine", "codex"],
        cwd=issue,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "reviewer modified the trusted Codex review surface" in result.stderr
