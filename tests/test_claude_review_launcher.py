"""Execution-level contract for the automatic Claude review launcher."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests.review_run_marker import run_comment


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / ".codex/skills/critique/scripts/run-claude-review.sh"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40



def _trusted_environment(
    tmp_path: Path,
    *,
    local_head: str = HEAD,
    pr_head: str = HEAD,
    remote_head: str = HEAD,
) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        f"local_head = {local_head!r}\n"
        f"remote_head = {remote_head!r}\n"
        "if args == ['rev-parse', 'HEAD']:\n"
        "    print(local_head)\n"
        "elif args == ['ls-remote', '--exit-code', 'origin', 'refs/heads/feature']:\n"
        "    print(remote_head + '\\trefs/heads/feature')\n"
        "elif args == ['status', '--porcelain']:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('unexpected git invocation: ' + ' '.join(args))\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        f"pr_head = {pr_head!r}\n"
        f"run_comment = {run_comment()!r}\n"
        "if args[:2] == ['repo', 'view']:\n"
        "    print('example/repository')\n"
        "elif args[:2] == ['api', 'user']:\n"
        "    print('reviewer')\n"
        "elif args[:2] == ['pr', 'view']:\n"
        "    if 'author,headRefName,headRefOid,headRepository' in args:\n"
        "        print(pr_head + '\\tfeature\\texample/repository\\treviewer')\n"
        "    else:\n"
        "        print(pr_head)\n"
        "elif args[:3] == ['api', '--paginate', '--slurp']:\n"
        "    print(json.dumps([[{'id': 10, 'body': run_comment, "
        "'user': {'login': 'reviewer'}}]]))\n"
        "else:\n"
        "    raise SystemExit('unexpected gh invocation: ' + ' '.join(args))\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


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
        **_trusted_environment(tmp_path),
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
            "--head",
            HEAD,
            "--round",
            "1",
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
    assert "round 1" in argv[6]
    assert invocation["env"] == {"base": HEAD, "engine": "claude", "round": "1"}


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
            "--head",
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


def test_launcher_rejects_a_mismatched_worktree_before_claude(tmp_path: Path) -> None:
    marker = tmp_path / "called"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    environment = {
        **_trusted_environment(tmp_path, local_head=OTHER_HEAD),
        "CLAUDE_REVIEW_CLI": str(fake_claude),
    }

    result = subprocess.run(
        [
            str(LAUNCHER),
            "--repo",
            "example/repository",
            "--pr",
            "123",
            "--base",
            HEAD,
            "--head",
            HEAD,
            "--round",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=environment,
    )

    assert result.returncode == 1
    assert "local HEAD does not match --head" in result.stderr
    assert not marker.exists()
