"""Execution-level contract for the automatic Agy review launcher."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / ".codex/skills/critique/scripts/run-agy-review.sh"
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
        "import sys\n"
        "args = sys.argv[1:]\n"
        f"pr_head = {pr_head!r}\n"
        "if args[:2] == ['repo', 'view']:\n"
        "    print('example/repository')\n"
        "elif args[:2] == ['api', 'user']:\n"
        "    print('reviewer')\n"
        "elif args[:2] == ['pr', 'view']:\n"
        "    print(pr_head + '\\tfeature\\texample/repository\\treviewer')\n"
        "else:\n"
        "    raise SystemExit('unexpected gh invocation: ' + ' '.join(args))\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _fake_agy(tmp_path: Path, *, review_status: str = "SUCCESS", stale: bool = False) -> tuple[Path, Path]:
    surface = tmp_path / "agy-surface" / ".agents"
    skill_parent = "deepcritique.bak.20260817T164958Z" if stale else "deepcritique"
    deep_skill = surface / "skills" / skill_parent / "SKILL.md"
    deep_skill.parent.mkdir(parents=True)
    deep_skill.write_text(
        "AGENT_LOOP_REVIEW_ENGINE AGENT_LOOP_REVIEW_BASE_SHA write-result gemini antigravity\n",
        encoding="utf-8",
    )
    display_skill = deep_skill
    if stale:
        canonical_skill_dir = surface / "skills/deepcritique"
        canonical_skill_dir.symlink_to(deep_skill.parent, target_is_directory=True)
        display_skill = canonical_skill_dir / "SKILL.md"
    required_files = {
        surface / "REVIEW_WORKFLOW.md": "workflow\n",
        surface / "references/local-review-ledger.md": "ledger\n",
        surface / "references/roles/code-reviewer.md": "role\n",
        surface / "references/roles/silent-failure-hunter.md": "role\n",
        surface / "references/roles/type-design-analyzer.md": "role\n",
        surface / "references/roles/comment-analyzer.md": "role\n",
        surface / "references/roles/pr-test-analyzer.md": "role\n",
        surface / "references/roles/security-reviewer.md": "role\n",
        surface / "skills/critique/SKILL.md": (
            "AGENT_LOOP_REVIEW_ENGINE write-result "
            ".agents/references/local-review-ledger.md\n"
        ),
        surface / "skills/critique/scripts/package.json": '{"type":"module"}\n',
        surface / "skills/critique/scripts/review-ledger.js": "ledger\n",
        surface / "skills/critique/scripts/review-ledger.version": "1.1.0\n",
        surface / "skills/critique/scripts/review-ledger.integrity": "sha512-test\n",
        surface / "skills/refactorpass/SKILL.md": "refactor\n",
    }
    for path, content in required_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    fake_agy = tmp_path / "agy"
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "prompt = args[-1]\n"
        "if prompt == '/skills':\n"
        "    print(json.dumps({'status': 'SUCCESS', 'command': {'data': {'skills': [\n"
        f"        {{'name': 'deepcritique', 'path': {str(display_skill)!r}}}\n"
        "    ]}}}))\n"
        "else:\n"
        "    with open(os.environ['AGY_ARGV_FILE'], 'w', encoding='utf-8') as out:\n"
        "        json.dump({'argv': args, 'env': {\n"
        "            'base': os.environ.get('AGENT_LOOP_REVIEW_BASE_SHA'),\n"
        "            'round': os.environ.get('AGENT_LOOP_REVIEW_ROUND'),\n"
        "            'engine': os.environ.get('AGENT_LOOP_REVIEW_ENGINE'),\n"
        "        }}, out)\n"
        f"    print(json.dumps({{'status': {review_status!r}, 'response': 'review complete'}}))\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)
    return fake_agy, surface


def _command() -> list[str]:
    return [
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
    ]


def test_launcher_executes_agy_with_pinned_model_and_high_effort(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, surface = _fake_agy(tmp_path)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = subprocess.run(
        _command(),
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=environment,
    )

    invocation = json.loads(argv_file.read_text(encoding="utf-8"))
    argv = invocation["argv"]
    assert argv[:13] == [
        "--model",
        "gemini-3.7-flash-high",
        "--effort",
        "high",
        "--mode",
        "accept-edits",
        "--dangerously-skip-permissions",
        "--add-dir",
        str(surface),
        "--output-format",
        "json",
        "--print-timeout",
        "60m",
    ]
    assert argv[13] == "--print"
    assert argv[14].startswith("/deepcritique 123\n")
    assert "Continue review on PR #123" in argv[14]
    assert "Use gemini as the active local-review engine identity" in argv[14]
    assert f"Agy relay surface is {surface}" in argv[14]
    assert HEAD in argv[14]
    assert "round 2" in argv[14]
    assert invocation["env"] == {"base": HEAD, "engine": "gemini", "round": "2"}
    assert result.stdout == "review complete\n"


def test_launcher_rejects_caller_supplied_model_or_effort(tmp_path: Path) -> None:
    marker = tmp_path / "called"
    fake_agy = tmp_path / "agy"
    fake_agy.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
    fake_agy.chmod(0o755)

    for extra in (("--model", "other"), ("--effort", "low")):
        result = subprocess.run(
            [*_command(), *extra],
            check=False,
            cwd=ROOT,
            env={**os.environ, "AGY_REVIEW_CLI": str(fake_agy)},
        )
        assert result.returncode == 2
    assert not marker.exists()


def test_launcher_rejects_mismatched_worktree_before_agy(tmp_path: Path) -> None:
    marker = tmp_path / "called"
    fake_agy = tmp_path / "agy"
    fake_agy.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
    fake_agy.chmod(0o755)
    environment = {
        **_trusted_environment(tmp_path, local_head=OTHER_HEAD),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = subprocess.run(
        _command(),
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=environment,
    )

    assert result.returncode == 1
    assert "local HEAD does not match --head" in result.stderr
    assert not marker.exists()


def test_launcher_rejects_canceled_agy_result_even_with_zero_exit(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path, review_status="CANCELED")
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = subprocess.run(
        _command(),
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=environment,
    )

    assert result.returncode == 1
    assert "status 'CANCELED'" in result.stderr


def test_launcher_rejects_stale_backup_skill_before_review(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path, stale=True)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = subprocess.run(
        _command(),
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=environment,
    )

    assert result.returncode == 1
    assert "stale or unexpected deepcritique skill" in result.stderr
    assert not argv_file.exists()


def test_launcher_rejects_incomplete_relay_surface_before_review(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, surface = _fake_agy(tmp_path)
    (surface / "REVIEW_WORKFLOW.md").unlink()
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = subprocess.run(
        _command(),
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=environment,
    )

    assert result.returncode == 1
    assert "relay surface is incomplete" in result.stderr
    assert "REVIEW_WORKFLOW.md" in result.stderr
    assert not argv_file.exists()


def test_launcher_forwards_termination_to_running_agy(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    completed_marker = tmp_path / "completed"
    fake_agy, _ = _fake_agy(tmp_path)
    source = fake_agy.read_text(encoding="utf-8")
    source = source.replace(
        "    with open(os.environ['AGY_ARGV_FILE'], 'w', encoding='utf-8') as out:\n",
        "    import time\n"
        "    with open(os.environ['AGY_CHILD_PID_FILE'], 'w', encoding='utf-8') as pid_out:\n"
        "        pid_out.write(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "    with open(os.environ['AGY_COMPLETED_MARKER'], 'w', encoding='utf-8') as marker:\n"
        "        marker.write('completed')\n"
        "    with open(os.environ['AGY_ARGV_FILE'], 'w', encoding='utf-8') as out:\n",
    )
    fake_agy.write_text(source, encoding="utf-8")
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(tmp_path / "argv.json"),
        "AGY_CHILD_PID_FILE": str(child_pid_file),
        "AGY_COMPLETED_MARKER": str(completed_marker),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    process = subprocess.Popen(
        _command(),
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(100):
        if child_pid_file.exists():
            break
        time.sleep(0.05)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))

    os.kill(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 143, (stdout, stderr)
    assert not completed_marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
