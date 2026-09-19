"""The Codex-root contract-v4 review launcher passes the pinned model and effort to both CLIs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / ".codex/skills/agent-loop/scripts/run-codex-review.sh"
PINNED = {
    "AGENT_LOOP_CODEX_MODEL": "codex-review",
    "AGENT_LOOP_CODEX_EFFORT": "high",
    "AGENT_LOOP_CLAUDE_MODEL": "claude-review",
    "AGENT_LOOP_CLAUDE_EFFORT": "medium",
}
CODEX_FLAGS = [
    "exec",
    "--dangerously-bypass-approvals-and-sandbox",
    "--ephemeral",
    "--ignore-rules",
    "--ignore-user-config",
    "--skip-git-repo-check",
    "-C",
]
Launch = Callable[..., tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]]
CLAUDE_FLAGS = [
    "--permission-mode",
    "bypassPermissions",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--safe-mode",
    "--add-dir",
]


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _required_paths(engine: str) -> list[str]:
    return subprocess.run(
        [str(LAUNCHER), "--required-paths", engine],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()


@pytest.fixture
def launch(tmp_path: Path) -> Launch:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    _git("init", "-b", "main", cwd=trusted)
    _git("config", "user.name", "Test", cwd=trusted)
    _git("config", "user.email", "test@example.invalid", cwd=trusted)
    _git("config", "commit.gpgsign", "false", cwd=trusted)
    (trusted / "AGENTS.md").write_text("# instructions\n", encoding="utf-8")
    for relative in _required_paths("codex") + _required_paths("claude"):
        path = trusted / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}\n", encoding="utf-8")
    _git("add", ".", cwd=trusted)
    _git("commit", "-m", "base", cwd=trusted)
    base = _git("rev-parse", "HEAD", cwd=trusted)
    worktree = tmp_path / "review"
    _git("worktree", "add", "--detach", str(worktree), base, cwd=trusted)

    record = tmp_path / "argv.jsonl"
    stub = tmp_path / "reviewer-cli"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['REVIEW_ARGV_LOG'], 'a') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:],\n"
        "        'effort_env': os.environ.get('CLAUDE_CODE_EFFORT_LEVEL')}) + '\\n')\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    digest = hashlib.sha256(stub.read_bytes()).hexdigest()

    def run(
        engine: str, *, settings: dict[str, str] | None = None, extra: dict[str, str] | None = None
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("AGENT_LOOP_", "CLAUDE_CODE_"))
        }
        env.update(PINNED if settings is None else settings)
        env.update(
            AGENT_LOOP_REVIEW_ENGINE=engine,
            AGENT_LOOP_REVIEW_BASE_SHA=base,
            AGENT_LOOP_REVIEW_ROUND="1",
            AGENT_LOOP_PR_NUMBER="7",
            AGENT_LOOP_PR_HEAD_SHA=base,
            AGENT_LOOP_REVIEW_RESULT_FILE=str(tmp_path / "result.json"),
            AGENT_LOOP_REVIEW_PUSH_HELPER=str(tmp_path / "push-helper"),
            AGENT_LOOP_TRUSTED_REPO_ROOT=str(trusted),
            AGENT_LOOP_TRUSTED_BASE_REF="refs/heads/main",
            AGENT_LOOP_REVIEW_BIN=str(stub),
            AGENT_LOOP_REVIEW_BIN_SHA256=digest,
            AGENT_LOOP_REVIEW_INSTALL_ROOT=str(stub),
            AGENT_LOOP_REVIEW_INSTALL_SHA256=digest,
            AGENT_LOOP_REAL_GIT=shutil.which("git") or "git",
            REVIEW_ARGV_LOG=str(record),
        )
        env.update(extra or {})
        record.unlink(missing_ok=True)
        result = subprocess.run(
            [str(LAUNCHER), "--engine", engine],
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        calls = (
            [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
            if record.exists()
            else []
        )
        return result, calls

    return run


def test_codex_reviewer_gets_pinned_model_and_effort(launch: Launch) -> None:
    result, calls = launch("codex")
    assert result.returncode == 0, result.stderr
    [call] = calls
    argv = call["argv"]
    assert argv[: len(CODEX_FLAGS)] == CODEX_FLAGS
    # Launch root, then the review worktree, then the pinned settings, then the prompt.
    tail = argv[len(CODEX_FLAGS) + 1 :]
    assert tail[:1] == ["--add-dir"]
    assert tail[2:6] == ["-m", "codex-review", "-c", 'model_reasoning_effort="high"']
    assert len(tail) == 7 and tail[6].startswith("Read ")


def test_claude_reviewer_gets_pinned_model_and_effort(launch: Launch) -> None:
    # An inherited effort variable outranks --effort in the Claude CLI, so the
    # launcher must replace it with the pinned value.
    result, calls = launch("claude", extra={"CLAUDE_CODE_EFFORT_LEVEL": "low"})
    assert result.returncode == 0, result.stderr
    [call] = calls
    argv = call["argv"]
    assert argv[:4] == ["--model", "claude-review", "--effort", "medium"]
    assert argv[4 : 4 + len(CLAUDE_FLAGS)] == CLAUDE_FLAGS
    assert argv[-2] == "--print"
    assert call["effort_env"] == "medium"


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_inherit_model_omits_the_model_flag(launch: Launch, engine: str) -> None:
    settings = dict(PINNED)
    settings[f"AGENT_LOOP_{engine.upper()}_MODEL"] = "inherit"
    result, calls = launch(engine, settings=settings)
    assert result.returncode == 0, result.stderr
    [call] = calls
    assert "-m" not in call["argv"] and "--model" not in call["argv"]
    effort = PINNED[f"AGENT_LOOP_{engine.upper()}_EFFORT"]
    if engine == "codex":
        assert f'model_reasoning_effort="{effort}"' in call["argv"]
    else:
        assert call["argv"][:2] == ["--effort", effort]


@pytest.mark.parametrize("engine", ["codex", "claude"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MODEL", None),
        ("EFFORT", None),
        ("MODEL", ""),
        ("EFFORT", ""),
        ("MODEL", "--dangerous"),
        ("MODEL", "two words"),
        ("EFFORT", "-x"),
        ("EFFORT", "high low"),
    ],
)
def test_missing_or_invalid_settings_fail_closed(
    launch: Launch, engine: str, field: str, value: str | None
) -> None:
    settings = dict(PINNED)
    name = f"AGENT_LOOP_{engine.upper()}_{field}"
    if value is None:
        settings.pop(name)
    else:
        settings[name] = value
    result, calls = launch(engine, settings=settings)
    assert result.returncode != 0
    assert calls == []
    assert f"pinned {engine} reviewer {field.lower()}" in result.stderr


def test_retired_effort_policy_query_is_gone() -> None:
    assert (
        subprocess.run(
            [str(LAUNCHER), "--contract-version"], capture_output=True, text=True, check=False
        ).stdout.strip()
        == "4"
    )
    retired = subprocess.run(
        [str(LAUNCHER), "--claude-effort-policy"], capture_output=True, text=True, check=False
    )
    assert retired.returncode == 2
    assert "CLAUDE_EFFORT_POLICY" not in LAUNCHER.read_text(encoding="utf-8")
