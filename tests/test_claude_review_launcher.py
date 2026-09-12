"""Execution-level contract for the automatic Claude review launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
HEAD = "a" * 40


@pytest.mark.parametrize("inherited", [None, "0", "1"])
def test_launcher_disables_background_tasks(
    tmp_path: Path, inherited: str | None
) -> None:
    launcher = tmp_path / "run-claude-review.sh"
    shutil.copyfile(
        ROOT / ".codex/skills/critique/scripts/run-claude-review.sh", launcher
    )
    shutil.copyfile(
        ROOT / ".codex/skills/critique/scripts/review-launch-state.py",
        tmp_path / "review-launch-state.py",
    )
    # Only the launcher is under test; authorize the synthetic PR locally.
    (tmp_path / "local-review-handoff.py").write_text(
        "import sys\nassert sys.argv[1] == 'authorize-pass'\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scripts = {
        "git": (
            "import sys\n"
            "args = sys.argv[1:]\n"
            f"if args == ['rev-parse', 'HEAD']: print({HEAD!r})\n"
            "elif args == ['ls-remote', '--exit-code', 'origin', 'refs/heads/feature']:\n"
            f"    print({HEAD!r} + '\\trefs/heads/feature')\n"
            "elif args == ['status', '--porcelain']: pass\n"
            "else: raise SystemExit('unexpected git call')\n"
        ),
        "gh": (
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['repo', 'view']: print('example/repository')\n"
            "elif args[:2] == ['api', 'user']: print('reviewer')\n"
            "elif args[:2] == ['pr', 'view']:\n"
            f"    print({HEAD!r} + '\\tfeature\\texample/repository\\treviewer')\n"
            "else: raise SystemExit('unexpected gh call')\n"
        ),
        "claude": (
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['PROBE_RESULT']).write_text(json.dumps({\n"
            "    'background': os.environ.get('CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'),\n"
            "    'argv': sys.argv[1:],\n"
            "}))\n"
        ),
    }
    for name, source in scripts.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env python3\n" + source)
        path.chmod(0o755)
    result_file = tmp_path / "invocation.json"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_REVIEW_CLI": str(bin_dir / "claude"),
        "PROBE_RESULT": str(result_file),
    }
    env.pop("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", None)
    if inherited is not None:
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = inherited
    subprocess.run(
        [
            "bash",
            str(launcher),
            "--repo",
            "example/repository",
            "--pr",
            "123",
            "--base",
            HEAD,
            "--head",
            HEAD,
            "--round",
            "1",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    invocation = json.loads(result_file.read_text())
    assert invocation["background"] == "1"
    assert invocation["argv"][:6] == [
        "--effort",
        "low",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--print",
    ]
