"""Execution-level contract for the automatic Gemini (Antigravity) review launcher."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / ".claude/skills/critique/scripts/run-agy-review.sh"
SYNC_TARGETS = ROOT / "scripts/sync-targets.yml"
LEDGER_VERSION_FILE = ROOT / ".claude/skills/critique/scripts/review-ledger.version"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
AGY_SURFACE_SHA = "3d7ad7c6d1e088faca88d52490bda1f45ce7e1fd"
LEDGER_VERSION = LEDGER_VERSION_FILE.read_text(encoding="utf-8").strip()

# Continuation flags start a fresh one-shot only by their absence, so the
# launcher is asserted against the CLI's complete resume surface.
CONTINUATION_FLAGS = {"--continue", "-c", "--conversation", "--prompt-interactive", "-i"}


def _trusted_environment(
    tmp_path: Path,
    *,
    current_repo: str = "example/repository",
    local_head: str = HEAD,
    pr_head: str = HEAD,
    pr_head_repo: str = "example/repository",
    pr_author: str = "reviewer",
    actor: str = "reviewer",
    remote_head: str = HEAD,
    surface_remote: str = "https://github.com/loomantix/gemini-platform.git",
    surface_head: str = AGY_SURFACE_SHA,
) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
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
        "        if os.environ.get('AGY_TEST_WORKTREE_DIRTY') == '1':\n"
        "            print(' M README.md')\n"
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
        "import sys\n"
        "args = sys.argv[1:]\n"
        f"current_repo = {current_repo!r}\n"
        f"pr_head = {pr_head!r}\n"
        f"pr_head_repo = {pr_head_repo!r}\n"
        f"pr_author = {pr_author!r}\n"
        f"actor = {actor!r}\n"
        "if args[:2] == ['repo', 'view']:\n"
        "    print(current_repo)\n"
        "elif args[:2] == ['api', 'user']:\n"
        "    print(actor)\n"
        "elif args[:2] == ['pr', 'view']:\n"
        "    print('\\t'.join([pr_head, 'feature', pr_head_repo, pr_author]))\n"
        "else:\n"
        "    raise SystemExit('unexpected gh invocation: ' + ' '.join(args))\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _surface_files(surface: Path, *, ledger_version: str = LEDGER_VERSION) -> dict[Path, str]:
    return {
        surface / "REVIEW_WORKFLOW.md": "workflow\n",
        surface / "references/local-review-ledger.md": "ledger\n",
        surface / "references/roles/code-reviewer.md": "role\n",
        surface / "references/roles/silent-failure-hunter.md": "role\n",
        surface / "references/roles/type-design-analyzer.md": "role\n",
        surface / "references/roles/comment-analyzer.md": "role\n",
        surface / "references/roles/pr-test-analyzer.md": "role\n",
        surface / "references/roles/security-reviewer.md": "role\n",
        surface / "skills/critique/SKILL.md": (
            "write-result .agents/references/local-review-ledger.md\n"
        ),
        surface / "skills/critique/scripts/package.json": '{"type":"module"}\n',
        surface / "skills/critique/scripts/review-ledger.js": "ledger\n",
        surface / "skills/critique/scripts/review-ledger.version": f"{ledger_version}\n",
        surface / "skills/critique/scripts/review-ledger.integrity": "sha512-test\n",
        surface / "skills/refactorpass/SKILL.md": "refactor\n",
    }


def _fake_agy(
    tmp_path: Path,
    *,
    review_status: str = "SUCCESS",
    review_payload: str | None = None,
    stale: bool = False,
    ledger_version: str = LEDGER_VERSION,
    deep_contract: str = (
        "AGENT_LOOP_REVIEW_ENGINE AGENT_LOOP_REVIEW_BASE_SHA write-result gemini antigravity\n"
    ),
) -> tuple[Path, Path]:
    surface = tmp_path / "agy-surface" / ".agents"
    skill_parent = "deepcritique.bak.20260817T164958Z" if stale else "deepcritique"
    deep_skill = surface / "skills" / skill_parent / "SKILL.md"
    deep_skill.parent.mkdir(parents=True)
    deep_skill.write_text(deep_contract, encoding="utf-8")
    display_skill = deep_skill
    if stale:
        canonical_skill_dir = surface / "skills/deepcritique"
        canonical_skill_dir.symlink_to(deep_skill.parent, target_is_directory=True)
        display_skill = canonical_skill_dir / "SKILL.md"
    for path, content in _surface_files(surface, ledger_version=ledger_version).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    if review_payload is None:
        review_payload = json.dumps({"status": review_status, "response": "review complete"})
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
        f"    sys.stdout.write({review_payload!r})\n"
        f"    raise SystemExit(int(os.environ.get('AGY_TEST_EXIT_CODE', '0')))\n",
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


def _run(environment: dict[str, str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(),
        check=check,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=environment,
    )


# --------------------------------------------------------------------------
# argv, environment, and one-shot contract
# --------------------------------------------------------------------------


def test_launcher_executes_the_pinned_model_effort_and_permission_contract(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, surface = _fake_agy(tmp_path)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment, check=True)

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
    assert f"relay surface is {surface}" in argv[14]
    assert HEAD in argv[14]
    assert "round 2" in argv[14]
    assert not CONTINUATION_FLAGS.intersection(argv)
    assert invocation["env"] == {"base": HEAD, "engine": "gemini", "round": "2"}
    assert result.stdout == "review complete\n"


def test_launcher_rejects_caller_supplied_overrides(tmp_path: Path) -> None:
    marker = tmp_path / "called"
    fake_agy = tmp_path / "agy"
    fake_agy.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
    fake_agy.chmod(0o755)

    for extra in (
        ("--model", "other"),
        ("--effort", "low"),
        ("--mode", "plan"),
        ("--continue",),
        ("--add-dir", str(tmp_path)),
    ):
        result = subprocess.run(
            [*_command(), *extra],
            check=False,
            cwd=ROOT,
            env={**os.environ, "AGY_REVIEW_CLI": str(fake_agy)},
        )
        assert result.returncode == 2, extra
    assert not marker.exists()


@pytest.mark.parametrize(
    "bad_arguments",
    [
        ["--repo", "not-a-repo", "--pr", "1", "--base", HEAD, "--head", HEAD, "--round", "1"],
        ["--repo", "o/r", "--pr", "0", "--base", HEAD, "--head", HEAD, "--round", "1"],
        ["--repo", "o/r", "--pr", "1", "--base", "short", "--head", HEAD, "--round", "1"],
        ["--repo", "o/r", "--pr", "1", "--base", HEAD, "--head", "HEAD", "--round", "1"],
        ["--repo", "o/r", "--pr", "1", "--base", HEAD, "--head", HEAD, "--round", "0"],
        ["--repo", "o/r", "--pr", "1", "--base", HEAD, "--head", HEAD],
    ],
)
def test_launcher_rejects_malformed_arguments(bad_arguments: list[str]) -> None:
    result = subprocess.run(
        [str(LAUNCHER), *bad_arguments], check=False, capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode == 2


# --------------------------------------------------------------------------
# exact-state preflight — every mismatch stops the launcher before it starts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "extra_environment", "expected"),
    [
        ({"local_head": OTHER_HEAD}, {}, "local HEAD does not match --head"),
        ({"pr_head": OTHER_HEAD}, {}, "PR head does not match --head"),
        ({"remote_head": OTHER_HEAD}, {}, "remote branch head does not match --head"),
        ({"current_repo": "other/repository"}, {}, "current repository does not match --repo"),
        (
            {"pr_head_repo": "fork/repository"},
            {},
            "PR head must be in the requested repository",
        ),
        (
            {"pr_author": "somebody-else"},
            {},
            "PR must be authored by the authenticated GitHub actor",
        ),
        ({}, {"AGY_TEST_WORKTREE_DIRTY": "1"}, "review worktree must be clean"),
    ],
)
def test_launcher_rejects_every_exact_state_mismatch_before_process_start(
    tmp_path: Path,
    overrides: dict[str, str],
    extra_environment: dict[str, str],
    expected: str,
) -> None:
    marker = tmp_path / "called"
    fake_agy = tmp_path / "agy"
    fake_agy.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
    fake_agy.chmod(0o755)
    environment = {
        **_trusted_environment(tmp_path, **overrides),
        "AGY_REVIEW_CLI": str(fake_agy),
        **extra_environment,
    }

    result = _run(environment)

    assert result.returncode == 1
    assert expected in result.stderr
    assert not marker.exists()


# --------------------------------------------------------------------------
# trusted-surface preflight
# --------------------------------------------------------------------------


def test_launcher_rejects_stale_backup_skill_before_review(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path, stale=True)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment)

    assert result.returncode == 1
    assert "stale or unexpected deepcritique skill" in result.stderr
    assert not argv_file.exists()


def test_launcher_rejects_ambiguous_skill_resolution(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, surface = _fake_agy(tmp_path)
    source = fake_agy.read_text(encoding="utf-8")
    duplicate = str(surface / "skills/deepcritique/SKILL.md")
    fake_agy.write_text(
        source.replace(
            "    ]}}}))\n",
            f"        , {{'name': 'deepcritique', 'path': {duplicate!r}}}\n    ]}}}}}}))\n",
        ),
        encoding="utf-8",
    )
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment)

    assert result.returncode == 1
    assert "exactly one deepcritique skill" in result.stderr
    assert not argv_file.exists()


@pytest.mark.parametrize("missing", ["REVIEW_WORKFLOW.md", "references/roles/security-reviewer.md"])
def test_launcher_rejects_incomplete_relay_surface_before_review(
    tmp_path: Path, missing: str
) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, surface = _fake_agy(tmp_path)
    (surface / missing).unlink()
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment)

    assert result.returncode == 1
    assert "relay surface is incomplete" in result.stderr
    assert missing in result.stderr
    assert not argv_file.exists()


def test_launcher_rejects_symlinked_surface_file(tmp_path: Path) -> None:
    # A regular-file boundary: a symlink in the surface can point anywhere,
    # so the pin over the checkout would no longer describe what gets read.
    argv_file = tmp_path / "argv.json"
    fake_agy, surface = _fake_agy(tmp_path)
    target = surface / "REVIEW_WORKFLOW.md"
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("substituted\n", encoding="utf-8")
    target.unlink()
    target.symlink_to(elsewhere)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment)

    assert result.returncode == 1
    assert "relay surface is incomplete" in result.stderr
    assert not argv_file.exists()


def test_launcher_rejects_non_esm_ledger_package(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, surface = _fake_agy(tmp_path)
    (surface / "skills/critique/scripts/package.json").write_text(
        '{"type":"commonjs"}\n', encoding="utf-8"
    )
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment)

    assert result.returncode == 1
    assert "must declare type=module" in result.stderr
    assert not argv_file.exists()


def test_launcher_rejects_mismatched_ledger_protocol_version(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path, ledger_version="0.0.1-not-this-one")
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment)

    assert result.returncode == 1
    assert "speaks review-ledger 0.0.1-not-this-one" in result.stderr
    assert f"this engine vendors {LEDGER_VERSION}" in result.stderr
    assert not argv_file.exists()


@pytest.mark.parametrize(
    "capability",
    ["AGENT_LOOP_REVIEW_ENGINE", "AGENT_LOOP_REVIEW_BASE_SHA", "write-result", "gemini", "antigravity"],
)
def test_launcher_rejects_surface_missing_a_v3_capability(tmp_path: Path, capability: str) -> None:
    argv_file = tmp_path / "argv.json"
    full = "AGENT_LOOP_REVIEW_ENGINE AGENT_LOOP_REVIEW_BASE_SHA write-result gemini antigravity\n"
    fake_agy, _ = _fake_agy(tmp_path, deep_contract=full.replace(capability, "REMOVED"))
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment)

    assert result.returncode == 1
    assert "lacks required capability" in result.stderr
    assert not argv_file.exists()


def test_launcher_rejects_untrusted_surface_remote(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path)
    environment = {
        **_trusted_environment(
            tmp_path, surface_remote="https://github.com/attacker/gemini-platform.git"
        ),
        "AGY_ARGV_FILE": str(argv_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment)

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

    result = _run(environment)

    assert result.returncode == 1
    assert f"not at the pinned gemini-platform commit {AGY_SURFACE_SHA}" in result.stderr
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

    result = _run(environment)

    assert result.returncode == 1
    assert "surface checkout must be clean" in result.stderr
    assert not argv_file.exists()


def test_launcher_neutralizes_config_execution_on_the_nominated_surface(tmp_path: Path) -> None:
    # The surface path is nominated by the reviewer CLI, and Git executes
    # programs named by a repository's own config, so every -C call must
    # disarm that config: the pin decides, not the candidate.
    options_file = tmp_path / "git-options.txt"
    fake_agy, _ = _fake_agy(tmp_path)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(tmp_path / "argv.json"),
        "AGY_GIT_OPTIONS_FILE": str(options_file),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    _run(environment, check=True)

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

    result = _run(environment)

    assert result.returncode == 1
    assert "surface checkout must be clean" in result.stderr
    assert not argv_file.exists()


def test_launcher_pin_matches_the_launcher_source() -> None:
    # The launcher comment promises this constant moves in lockstep with the pin,
    # so assert it rather than mirroring the value by hand.
    assert f'agy_surface_sha="{AGY_SURFACE_SHA}"' in LAUNCHER.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# structured-result validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "exit_code", "expected"),
    [
        (json.dumps({"status": "CANCELED", "response": "stopped"}), "0", "status 'CANCELED'"),
        (
            json.dumps({"status": "ERROR", "response": "", "error": "timeout waiting for response"}),
            "1",
            "timeout waiting for response",
        ),
        # The CLI reports a turn-level ERROR when a single tool call failed, so a
        # narrated success with exit 0 must still fail closed.
        (
            json.dumps({"status": "ERROR", "response": "round complete", "error": "tool call failed"}),
            "0",
            "status 'ERROR'",
        ),
        ("{not json", "0", "invalid JSON"),
        ("", "0", "invalid JSON"),
        (json.dumps({"status": "SUCCESS", "response": "   "}), "0", "without a text response"),
        (json.dumps({"status": "SUCCESS"}), "0", "without a text response"),
        (json.dumps({"status": "SUCCESS", "response": "done"}), "1", "exit 1"),
    ],
)
def test_launcher_requires_a_trustworthy_structured_result(
    tmp_path: Path, payload: str, exit_code: str, expected: str
) -> None:
    fake_agy, _ = _fake_agy(tmp_path, review_payload=payload)
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(tmp_path / "argv.json"),
        "AGY_REVIEW_CLI": str(fake_agy),
        "AGY_TEST_EXIT_CODE": exit_code,
    }

    result = _run(environment)

    assert result.returncode == 1
    assert expected in result.stderr
    assert result.stdout == ""


def test_launcher_failure_directs_the_caller_to_the_ledger(tmp_path: Path) -> None:
    # A narrated success or an already-posted comment is not a passing result;
    # the operator has to read the ledger and re-run rather than relax the gate.
    fake_agy, _ = _fake_agy(
        tmp_path,
        review_payload=json.dumps(
            {"status": "ERROR", "response": "round complete", "error": "tool call failed"}
        ),
    )
    environment = {
        **_trusted_environment(tmp_path),
        "AGY_ARGV_FILE": str(tmp_path / "argv.json"),
        "AGY_REVIEW_CLI": str(fake_agy),
    }

    result = _run(environment)

    assert result.returncode == 1
    assert "read the PR ledger directly" in result.stderr


# --------------------------------------------------------------------------
# process-group management
# --------------------------------------------------------------------------


def _long_running_agy(tmp_path: Path) -> tuple[Path, Path]:
    fake_agy, surface = _fake_agy(tmp_path)
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
    return fake_agy, surface


@pytest.mark.parametrize(
    ("forwarded_signal", "expected_returncode"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)],
)
def test_launcher_forwards_termination_to_the_running_review(
    tmp_path: Path, forwarded_signal: int, expected_returncode: int
) -> None:
    child_pid_file = tmp_path / "child.pid"
    completed_marker = tmp_path / "completed"
    fake_agy, _ = _long_running_agy(tmp_path)
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
        # Under POSIX a non-interactive bash cannot trap a signal that entered
        # as SIG_IGN, so the child is started with the default disposition to
        # keep the forwarding assertion deterministic across runners.
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL),
    )
    for _ in range(200):
        if child_pid_file.exists():
            break
        time.sleep(0.05)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))

    os.kill(process.pid, forwarded_signal)
    stdout, stderr = process.communicate(timeout=15)

    assert process.returncode == expected_returncode, (stdout, stderr)
    assert not completed_marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_launcher_survives_an_inherited_ignored_sighup(tmp_path: Path) -> None:
    # When SIGHUP enters as SIG_IGN the launcher cannot trap it, so the honest
    # contract is that the hangup changes nothing: the review still completes.
    argv_file = tmp_path / "argv.json"
    fake_agy, _ = _fake_agy(tmp_path)
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
    }

    process = subprocess.Popen(
        _command(),
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),
    )
    time.sleep(0.5)
    os.kill(process.pid, signal.SIGHUP)
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 0, (stdout, stderr)
    assert stdout == "review complete\n"
    assert argv_file.exists()


def test_launcher_completes_under_inherited_job_control(tmp_path: Path) -> None:
    # Bash imports `monitor` from an exported SHELLOPTS. With job control on, a
    # forking setsid would detach the reviewer and hand back an empty result.
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

    result = _run(environment, check=True)

    assert result.stdout == "review complete\n"
    assert argv_file.exists()


# --------------------------------------------------------------------------
# the launch-to-$! assignment race, and the proof that the test can fail
# --------------------------------------------------------------------------


def _race_environment(tmp_path: Path, launcher: Path) -> tuple[dict[str, str], Path, Path, Path]:
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
    fake_agy, _ = _fake_agy(tmp_path)
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
    return environment, race_marker, child_pid_file, completed_marker


def _run_race(
    environment: dict[str, str], launcher: Path, child_pid_file: Path, log_dir: Path
) -> subprocess.Popen[bytes]:
    # Streams go to files rather than pipes: an orphaned review holds the
    # inherited pipe open, so `communicate` would block on the very outcome
    # the mutation proof needs to observe.
    command = _command()
    command[0] = str(launcher)
    with open(log_dir / "race.out", "wb") as out, open(log_dir / "race.err", "wb") as err:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=out,
            stderr=err,
            preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL),
        )
    process.wait(timeout=15)
    return process


def _await_reaped(pid: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} outlived the launcher")


def test_launcher_tracks_the_child_when_a_signal_precedes_pid_assignment(tmp_path: Path) -> None:
    environment, race_marker, child_pid_file, completed_marker = _race_environment(
        tmp_path, LAUNCHER
    )

    process = _run_race(environment, LAUNCHER, child_pid_file, tmp_path)

    assert race_marker.exists()
    assert child_pid_file.exists()
    assert process.returncode == 129
    assert not completed_marker.exists()
    _await_reaped(int(child_pid_file.read_text(encoding="utf-8")))


def test_race_coverage_fails_when_the_jobs_table_fallback_is_removed(tmp_path: Path) -> None:
    # Mutation proof: without the jobs-table fallback the launcher exits on the
    # signal while the unattended review keeps running, so the assertions above
    # are load-bearing rather than incidentally satisfied.
    # The launcher reads its ledger-version expectation from the file beside
    # it, so the mutant needs that sibling to reach the race at all.
    mutant_dir = tmp_path / "mutant-scripts"
    mutant_dir.mkdir()
    (mutant_dir / "review-ledger.version").write_text(
        LEDGER_VERSION_FILE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    mutated = mutant_dir / "run-agy-review.sh"
    source = LAUNCHER.read_text(encoding="utf-8")
    fallback = (
        '    if [ -z "$target_pid" ]; then\n'
        '        target_pid="$(jobs -pr | head -n 1)"\n'
        "    fi\n"
    )
    assert fallback in source
    mutated.write_text(source.replace(fallback, ""), encoding="utf-8")
    mutated.chmod(0o755)
    environment, race_marker, child_pid_file, completed_marker = _race_environment(
        tmp_path, mutated
    )

    process = _run_race(environment, mutated, child_pid_file, tmp_path)

    assert race_marker.exists()
    assert process.returncode == 129
    assert not completed_marker.exists()
    # The distinguishing observation: the unattended review survived the signal
    # the launcher was supposed to forward, and is still running unreachable.
    assert child_pid_file.exists()
    orphan_pid = int(child_pid_file.read_text(encoding="utf-8"))
    os.kill(orphan_pid, 0)
    os.kill(orphan_pid, signal.SIGKILL)


# --------------------------------------------------------------------------
# sync surface
# --------------------------------------------------------------------------


def test_launcher_is_executable_in_the_working_tree() -> None:
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR


def test_launcher_is_a_synced_target_with_the_executable_mode() -> None:
    manifest = SYNC_TARGETS.read_text(encoding="utf-8")
    entry = re.search(
        r"- source: \.claude/skills/critique/scripts/run-agy-review\.sh\n"
        r"    destination: (?P<destination>\S+)\n"
        r"    substitutions: \[\]\n"
        r"    mode: '(?P<mode>\d{4})'\n",
        manifest,
    )
    assert entry is not None, "the launcher must ship to consumers through the sync manifest"
    assert entry.group("destination") == ".claude/skills/critique/scripts/run-agy-review.sh"
    assert entry.group("mode") == "0755"


def test_launcher_locks_its_unattended_invocation_region() -> None:
    # The escalation literal lives inside a hashed region, so a flag edit is
    # reviewer-visible through the allowlist rather than silent.
    source = LAUNCHER.read_text(encoding="utf-8")
    start = source.index("# claude-cli-invocations:start")
    end = source.index("# claude-cli-invocations:end")
    assert start < end
    assert "--dangerously-skip-permissions" in source[start:end]
    assert source.count("--dangerously-skip-permissions") == 1
    allowlist = (ROOT / ".claude/claude-cli-invocations.allowlist").read_text(encoding="utf-8")
    assert ".claude/skills/critique/scripts/run-agy-review.sh" in allowlist


def test_review_workflow_documents_the_auto_relay_contract() -> None:
    workflow = (ROOT / ".claude/REVIEW_WORKFLOW.md").read_text(encoding="utf-8")
    deepcritique = (ROOT / ".claude/skills/deepcritique/SKILL.md").read_text(encoding="utf-8")
    assert "run-agy-review.sh" in workflow
    assert "gemini-3.7-flash-high" in workflow
    assert "literal `--effort high`" in workflow
    assert "verify-coverage" in workflow
    assert "run-agy-review.sh" in deepcritique
    assert "verify-coverage" in deepcritique
