"""Execution-level contract for the automatic Claude review launcher."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / ".codex/skills/critique/scripts/run-claude-review.sh"
HEAD = "a" * 40


def test_launcher_executes_claude_with_literal_low_effort(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CLAUDE_ARGV_FILE'], 'w', encoding='utf-8') as out:\n"
        "    json.dump({'argv': sys.argv[1:], 'env': {\n"
        "        'base': os.environ.get('AGENT_LOOP_REVIEW_BASE_SHA'),\n"
        "        'round': os.environ.get('AGENT_LOOP_REVIEW_ROUND'),\n"
        "        'engine': os.environ.get('AGENT_LOOP_REVIEW_ENGINE'),\n"
        "    }}, out)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    environment = {
        **os.environ,
        "CLAUDE_ARGV_FILE": str(argv_file),
        "CLAUDE_REVIEW_CLI": str(fake_claude),
    }

    subprocess.run(
        [
            str(LAUNCHER),
            "--repo",
            "example/repository",
            "--pr",
            "123",
            "--base",
            HEAD,
            "--round",
            "2",
        ],
        check=True,
        cwd=ROOT,
        env=environment,
    )

    invocation = json.loads(argv_file.read_text(encoding="utf-8"))
    argv = invocation["argv"]
    assert argv[:6] == [
        "--effort",
        "low",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--print",
    ]
    assert "max" not in argv
    assert argv[6].startswith("/deepcritique 123\n")
    assert "Continue review on PR #123" in argv[6]
    assert HEAD in argv[6]
    assert "round 2" in argv[6]
    assert invocation["env"] == {"base": HEAD, "engine": "claude", "round": "2"}


def test_launcher_rejects_a_caller_supplied_effort(tmp_path: Path) -> None:
    marker = tmp_path / "called"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        f"#!/usr/bin/env bash\ntouch {marker}\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    result = subprocess.run(
        [
            str(LAUNCHER),
            "--repo",
            "example/repository",
            "--pr",
            "123",
            "--base",
            HEAD,
            "--round",
            "2",
            "--effort",
            "max",
        ],
        check=False,
        cwd=ROOT,
        env={**os.environ, "CLAUDE_REVIEW_CLI": str(fake_claude)},
    )

    assert result.returncode == 2
    assert not marker.exists()
