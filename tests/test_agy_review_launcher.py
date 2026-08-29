"""Execution-level contract for the automatic Agy review launcher."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.review_run_marker import run_comment


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / ".codex/skills/critique/scripts/run-agy-review.sh"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
AGY_SURFACE_SHA = "3d7ad7c6d1e088faca88d52490bda1f45ce7e1fd"



def _trusted_environment(
    tmp_path: Path,
    *,
    local_head: str = HEAD,
    pr_head: str = HEAD,
    remote_head: str = HEAD,
    surface_remote: str = "https://github.com/loomantix/gemini-platform.git",
    surface_head: str = AGY_SURFACE_SHA,
) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        f"local_head = {local_head!r}\n"
        f"remote_head = {remote_head!r}\n"
        f"surface_remote = {surface_remote!r}\n"
        f"surface_head = {surface_head!r}\n"
        # Strip leading global options so the launcher may harden its
        # surface calls without every dispatch below shifting position.
        "global_options = []\n"
        "while args and (args[0] == '-c' or args[0].startswith('--')):\n"
        "    if args[0] == '-c':\n"
        "        global_options.append(args[1]); args = args[2:]\n"
        "    else:\n"
        "        global_options.append(args[0]); args = args[1:]\n"
        "target = None\n"
        "if args[:1] == ['-C']:\n"
        "    target, args = args[1], args[2:]\n"
        "    options_file = os.environ.get('AGY_GIT_OPTIONS_FILE')\n"
        "    if options_file:\n"
        "        with open(options_file, 'a', encoding='utf-8') as handle:\n"
        "            handle.write(' '.join(global_options) + '\\n')\n"
        "if target is None:\n"
        "    if args == ['rev-parse', 'HEAD']:\n"
        "        print(local_head)\n"
        "    elif args == ['ls-remote', '--exit-code', 'origin', 'refs/heads/feature']:\n"
        "        print(remote_head + '\\trefs/heads/feature')\n"
        "    elif args == ['status', '--porcelain']:\n"
        "        pass\n"
        "    else:\n"
        "        raise SystemExit('unexpected git invocation: ' + ' '.join(args))\n"
        "elif args == ['rev-parse', '--show-toplevel']:\n"
        "    print(pathlib.Path(target).parent)\n"
        "elif args == ['remote', 'get-url', 'origin']:\n"
        "    print(surface_remote)\n"
        "elif args == ['rev-parse', 'HEAD']:\n"
        "    print(surface_head)\n"
        "elif args == ['status', '--porcelain']:\n"
        "    if os.environ.get('AGY_TEST_SURFACE_DIRTY') == '1':\n"
        "        print(' M .agents/skills/critique/SKILL.md')\n"
        "    if os.environ.get('AGY_TEST_SURFACE_EXCLUDED') == '1':\n"
        "        if 'core.excludesFile=/dev/null' in global_options:\n"
        "            print('?? .agents/skills/critique/EXTRA.md')\n"
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
        "1",
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
    assert len(argv) == 15
    assert argv[13] == "--print"
    assert argv[14].startswith("/deepcritique 123\n")
    assert "Continue review on PR #123" in argv[14]
    assert "Use gemini as the active local-review engine identity" in argv[14]
    assert f"Agy relay surface is {surface}" in argv[14]
    assert HEAD in argv[14]
    assert "round 1" in argv[14]
    assert not {"--continue", "-c", "--conversation", "--prompt-interactive", "-i"}.intersection(argv)
    assert invocation["env"] == {"base": HEAD, "engine": "gemini", "round": "1"}
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


def test_launcher_rejects_dirty_trusted_surface_before_review(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
        "AGY_TEST_SURFACE_DIRTY": "1",
    }

    result = subprocess.run(
        _command(), check=False, capture_output=True, text=True, cwd=ROOT, env=environment
    )

    assert result.returncode == 1
    assert "surface checkout must be clean" in result.stderr
    assert not argv_file.exists()


@pytest.mark.parametrize(
    ("forwarded_signal", "expected_returncode"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)],
)
def test_launcher_forwards_termination_to_running_agy(
    tmp_path: Path, forwarded_signal: int, expected_returncode: int
) -> None:
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
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL),
    )
    for _ in range(100):
        if child_pid_file.exists():
            break
        time.sleep(0.05)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))

    os.kill(process.pid, forwarded_signal)
    stdout, stderr = process.communicate(timeout=8)

    assert process.returncode == expected_returncode, (stdout, stderr)
    assert not completed_marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_launcher_tracks_child_when_signal_precedes_pid_assignment(tmp_path: Path) -> None:
    race_marker = tmp_path / "race-triggered"
    child_pid_file = tmp_path / "race-child.pid"
    completed_marker = tmp_path / "race-child-completed"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        "set -T\n"
        "trap 'if [[ $BASH_COMMAND == \"agy_pid=\\\"\\$!\\\"\" ]]; then "
        "trap - DEBUG; "
        "while [[ ! -s \"$AGY_CHILD_PID_FILE\" ]]; do sleep 0.01; done; "
        ": > \"$AGY_RACE_MARKER\"; kill -HUP \"$$\"; fi' DEBUG\n",
        encoding="utf-8",
    )
    fake_agy, surface = _fake_agy(tmp_path)
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import os, time\n"
        "with open(os.environ['AGY_CHILD_PID_FILE'], 'w', encoding='utf-8') as out:\n"
        "    out.write(str(os.getpid()))\n"
        "time.sleep(30)\n"
        "with open(os.environ['AGY_COMPLETED_MARKER'], 'w', encoding='utf-8') as out:\n"
        "    out.write('completed')\n",
        encoding="utf-8",
    )
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_CHILD_PID_FILE": str(child_pid_file),
        "AGY_COMPLETED_MARKER": str(completed_marker),
        "AGY_RACE_MARKER": str(race_marker),
        "AGY_REVIEW_CLI": str(fake_agy),
        "BASH_ENV": str(bash_env),
    }

    process = subprocess.Popen(
        _command(), cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL)
    )
    try:
        stdout, stderr = process.communicate(timeout=8)
    finally:
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert race_marker.exists()
    assert child_pid_file.exists()
    assert process.returncode == 129, (stdout, stderr)
    assert not completed_marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid_file.read_text(encoding="utf-8")), 0)
    assert surface.exists()


def test_launcher_rejects_untrusted_surface_remote(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path)
    environment = {
        **_trusted_environment(tmp_path, surface_remote="https://github.com/attacker/gemini-platform.git"),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = subprocess.run(
        _command(), check=False, capture_output=True, text=True, cwd=ROOT, env=environment
    )

    assert result.returncode == 1
    assert "untrusted Git remote" in result.stderr
    assert not argv_file.exists()


def test_launcher_rejects_unpinned_surface_commit(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path)
    environment = {
        **_trusted_environment(tmp_path, surface_head="c" * 40),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = subprocess.run(
        _command(), check=False, capture_output=True, text=True, cwd=ROOT, env=environment
    )

    assert result.returncode == 1
    assert f"not at the pinned gemini-platform commit {AGY_SURFACE_SHA}" in result.stderr
    assert not argv_file.exists()


def test_launcher_pin_matches_the_launcher_source() -> None:
    # The launcher comment promises this constant moves in lockstep with the pin,
    # so assert it rather than mirroring the value by hand.
    assert f'agy_surface_sha="{AGY_SURFACE_SHA}"' in LAUNCHER.read_text(encoding="utf-8")


def test_launcher_neutralizes_config_execution_on_the_nominated_surface(tmp_path: Path) -> None:
    # The surface path is nominated by Agy, and Git executes programs named by a
    # repository's own config, so every -C call must disarm that config.
    options_file = tmp_path / "git-options.txt"
    fake_agy, _ = _fake_agy(tmp_path)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(tmp_path / "argv.json"),
        "AGY_GIT_OPTIONS_FILE": str(options_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    subprocess.run(
        _command(), check=True, capture_output=True, text=True, cwd=ROOT, env=environment
    )

    invocations = options_file.read_text(encoding="utf-8").splitlines()
    assert len(invocations) == 4
    for options in invocations:
        assert "core.fsmonitor=" in options
        assert "core.hooksPath=/dev/null" in options
        assert "core.excludesFile=/dev/null" in options
        assert "--no-optional-locks" in options


def test_launcher_rejects_file_hidden_by_global_excludes(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
        "AGY_TEST_SURFACE_EXCLUDED": "1",
    }

    result = subprocess.run(
        _command(), check=False, capture_output=True, text=True, cwd=ROOT, env=environment
    )

    assert result.returncode == 1
    assert "surface checkout must be clean" in result.stderr
    assert not argv_file.exists()


def test_launcher_rejects_blank_review_response(tmp_path: Path) -> None:
    fake_agy, _ = _fake_agy(tmp_path)
    fake_agy.write_text(
        fake_agy.read_text(encoding="utf-8").replace(
            "'response': 'review complete'", "'response': '   '"
        ),
        encoding="utf-8",
    )
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(tmp_path / "argv.json"),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = subprocess.run(
        _command(), check=False, capture_output=True, text=True, cwd=ROOT, env=environment
    )

    assert result.returncode == 1
    assert "without a text response" in result.stderr
    assert result.stdout == ""


def test_launcher_completes_under_inherited_job_control(tmp_path: Path) -> None:
    # Bash imports `monitor` from an exported SHELLOPTS. With job control on, a
    # forking setsid would detach Agy and hand the launcher an empty result.
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path)
    # Delay the payload so a launcher that stopped waiting is caught
    # deterministically rather than by whichever process wins a race.
    fake_agy.write_text(
        fake_agy.read_text(encoding="utf-8").replace(
            "    with open(os.environ['AGY_ARGV_FILE'], 'w', encoding='utf-8') as out:\n",
            "    import time\n"
            "    time.sleep(2)\n"
            "    with open(os.environ['AGY_ARGV_FILE'], 'w', encoding='utf-8') as out:\n",
        ),
        encoding="utf-8",
    )
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
        "SHELLOPTS": "monitor",
    }

    result = subprocess.run(
        _command(), check=True, capture_output=True, text=True, cwd=ROOT, env=environment
    )

    assert result.stdout == "review complete\n"
    assert argv_file.exists()
