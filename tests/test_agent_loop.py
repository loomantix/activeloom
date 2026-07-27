"""Deterministic integration coverage for the agent-loop wrapper."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_LOOP = REPO_ROOT / ".codex/skills/agent-loop/scripts/agent-loop.sh"


def _run_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _agent_loop_branches(repo: Path) -> str:
    return _run_git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent-loop",
        cwd=repo,
    ).stdout


def _clone_test_repo(remote: Path, clone: Path) -> None:
    _run_git("clone", str(remote), str(clone))
    _run_git("config", "user.name", "Test", cwd=clone)
    _run_git("config", "user.email", "test@example.invalid", cwd=clone)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def consumer(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "consumer"
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()

    _run_git("init", "--bare", str(remote))
    _run_git("init", "-b", "main", str(repo))
    _run_git("config", "user.name", "Test", cwd=repo)
    _run_git("config", "user.email", "test@example.invalid", cwd=repo)

    script = repo / ".codex/skills/agent-loop/scripts/agent-loop.sh"
    ready = repo / ".codex/skills/issues/scripts/ready.py"
    script.parent.mkdir(parents=True)
    ready.parent.mkdir(parents=True)
    shutil.copy2(AGENT_LOOP, script)
    for guard_name in ("hook-git-guard", "hook-gh-guard"):
        shutil.copy2(AGENT_LOOP.parent / guard_name, script.parent / guard_name)
    _write_executable(
        ready,
        "#!/usr/bin/env python3\n"
        "import os, pathlib\n"
        "state = pathlib.Path(os.environ['AGENT_STATE_DIR'])\n"
        "key = ('AGENT_POST_CLAIM_READY_JSON' "
        "if any(state.glob('claimed-*')) else 'AGENT_READY_JSON')\n"
        "print(os.environ.get(key, os.environ.get('AGENT_READY_JSON', '[]')))\n",
    )
    (repo / "agent-loop-instructions.md").write_text(
        "# Local-only worker instructions\n", encoding="utf-8"
    )
    (repo / ".codex/skills/agent-loop/prompt.txt").write_text(
        "Implement #{ISSUE_ID}, commit locally, and do not push or open a PR.\n",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git("add", ".", cwd=repo)
    _run_git("commit", "-m", "test fixture", cwd=repo)
    _run_git("remote", "add", "origin", str(remote), cwd=repo)
    _run_git("push", "-u", "origin", "main", cwd=repo)
    _run_git(
        "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=repo
    )

    gh = bin_dir / "gh"
    _write_executable(
        gh,
        r"""#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ['AGENT_STATE_DIR'])
with (state / 'gh.log').open('a') as handle:
    handle.write(' '.join(args) + '\n')
issues = json.loads(os.environ.get('AGENT_ISSUES_JSON', '{}'))
if args[:2] == ['auth', 'git-credential']:
    sys.stdin.read()
    print('username=tester')
    print('password=' + os.environ.get('GH_TOKEN', ''))
elif args[:3] == ['api', 'user', '--jq']:
    print('tester')
elif args[:2] == ['repo', 'view']:
    print('fixture/consumer')
elif args[:2] == ['issue', 'view']:
    number = args[2]
    issue = issues.get(number, {'number': int(number), 'title': 'fixture', 'body': '', 'state': 'OPEN', 'labels': [{'name': 'dev: agent'}], 'assignees': []})
    claimed = state / ('claimed-' + number)
    view_counter = state / ('issue-views-' + number)
    view_count = int(view_counter.read_text() if view_counter.exists() else '0') + 1
    view_counter.write_text(str(view_count))
    final_issue = json.loads(os.environ.get('AGENT_FINAL_ISSUES_JSON', '{}')).get(number)
    if view_count > 2 and final_issue is not None:
        issue = final_issue
    elif claimed.exists() or view_count > 1:
        issue = json.loads(os.environ.get('AGENT_POST_CLAIM_ISSUES_JSON', '{}')).get(number, issue)
        issue = dict(issue)
        login = os.environ.get('AGENT_VERIFIED_ASSIGNEE', 'tester')
        issue['assignees'] = ([{'login': login}] if login else [])
    if args[3:] == ['--json', 'assignees']:
        print(json.dumps({'assignees': issue['assignees']}))
    elif 'closedByPullRequestsReferences' in ' '.join(args):
        dep = json.loads(os.environ.get('AGENT_ISSUE_DEPENDENCIES', '{}')).get(number, [])
        for row in dep:
            print('\t'.join(str(value) for value in row))
    elif '--jq' in args and '.assignees | length' in args:
        print(1)
    else:
        print(json.dumps(issue))
elif args[:2] == ['issue', 'edit']:
    number = args[2]
    claimed = state / ('claimed-' + number)
    if '--add-assignee' in args:
        claimed.touch()
    elif '--remove-assignee' in args:
        if os.environ.get('AGENT_FAIL_REMOVE_ASSIGNEE') == 'true':
            sys.exit(75)
        claimed.unlink(missing_ok=True)
elif args[:2] == ['pr', 'view']:
    joined = ' '.join(args)
    if 'state,isDraft,headRefName,headRefOid' in joined:
        branch = (state / 'pr-branch').read_text()
        remote_head = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch],
            check=True, capture_output=True, text=True
        ).stdout.split()[0]
        head = os.environ.get('AGENT_PR_HEAD_OID', remote_head)
        print('\t'.join(['OPEN', 'true', branch, head]))
    elif '--json number' in joined:
        print('1')
    elif 'headRefOid' in joined:
        print(os.environ.get('AGENT_PR_HEAD_OID', (state / 'pr-head').read_text()))
    else:
        number = args[2]
        row = json.loads(os.environ.get('AGENT_PRS_JSON', '{}')).get(number)
        if row:
            print('\t'.join(str(value) for value in row))
        else:
            sys.exit(1)
elif args[:2] == ['pr', 'create']:
    head = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True
    ).stdout.strip()
    (state / 'pr-head').write_text(head)
    branch = subprocess.run(
        ['git', 'branch', '--show-current'], check=True, capture_output=True, text=True
    ).stdout.strip()
    (state / 'pr-branch').write_text(branch)
    print('https://example.invalid/pr/1')
elif args[:2] == ['pr', 'edit']:
    (state / 'pr-edited').touch()
elif args[:2] == ['pr', 'ready']:
    (state / 'pr-ready').touch()
elif args[:2] == ['pr', 'close']:
    (state / 'pr-closed').touch()
elif args[:2] == ['api', 'graphql']:
    nodes = json.loads(os.environ.get('AGENT_REVIEW_THREADS_JSON', '[]'))
    print(json.dumps([{'data': {'repository': {'pullRequest': {
        'reviewThreads': {'nodes': nodes, 'pageInfo': {
            'hasNextPage': False, 'endCursor': None
        }}
    }}}}]))
else:
    print('unsupported gh invocation: ' + ' '.join(args), file=sys.stderr)
    sys.exit(2)
""",
    )
    return repo, remote, bin_dir, state_dir


def _issue(number: int, body: str = "", *, assigned: bool = False) -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "state": "OPEN",
        "labels": [{"name": "dev: agent"}],
        "assignees": [{"login": "tester"}] if assigned else [],
    }


def _config(tmp_path: Path, **overrides: str | int) -> str:
    values: dict[str, str | int] = {
        "base_branch": "main",
        "setup_hook": "printf 'setup\\n' >> \"$EVENT_LOG\"",
        "validation_hook": "printf 'validate\\n' >> \"$EVENT_LOG\"",
        "claude_review_hook": "printf 'claude\\n' >> \"$EVENT_LOG\"",
        "codex_review_hook": "printf 'codex\\n' >> \"$EVENT_LOG\"",
        "worker_hook": "printf 'worker\\n' >> \"$EVENT_LOG\"; printf 'done\\n' > result.txt; git add result.txt; git commit -m 'fix: worker'",
        "worker_retries": 1,
        "worker_timeout_seconds": 5,
        "hook_timeout_seconds": 10,
        "review_contract_version": 2,
        "review_max_rounds": 3,
        "retry_on_timeout": "true",
        "retry_delay_seconds": 0,
        "dependency_gate": "ready",
        "branch_prefix": "agent-loop",
        "worktree_root": str(tmp_path / "worktrees"),
        "log_root": str(tmp_path / "logs"),
        "log_max_kb": 128,
        "output_max_lines": 10,
    }
    values.update(overrides)
    for key in ("codex_review_hook", "claude_review_hook"):
        command = str(values[key])
        values[key] = (
            f"{command}; hook_status=$?; "
            '[ "$hook_status" -eq 0 ] || exit "$hook_status"; '
            'if [ "$(git rev-parse HEAD)" != "$AGENT_LOOP_PR_HEAD_SHA" ]; then '
            'git push origin HEAD:"refs/heads/$AGENT_LOOP_BRANCH"; fi'
        )
    return "\n".join(f"{key} = {value}" for key, value in values.items()) + "\n"


def _run(
    fixture: tuple[Path, Path, Path, Path],
    args: list[str],
    *,
    issues: list[dict[str, object]],
    config: str,
    extra_env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    repo, _, bin_dir, state_dir = fixture
    for state_file in [
        *state_dir.glob("claimed-*"),
        *state_dir.glob("issue-views-*"),
    ]:
        state_file.unlink()
    (repo / ".codex/skills/agent-loop/agent-loop.config").write_text(
        config, encoding="utf-8"
    )
    env = os.environ.copy()
    # The temporary ready.py/gh fixtures are black-box shell dependencies, not
    # coverage targets. pytest-cov exports COV_CORE_* for subprocess collection;
    # letting these standalone stubs auto-start coverage can produce statement
    # data that pytest-cov 6 cannot combine with this repo's branch data.
    for key in [name for name in env if name.startswith("COV_CORE_")]:
        env.pop(key)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AGENT_STATE_DIR": str(state_dir),
            "AGENT_ISSUES_JSON": json.dumps(
                {str(issue["number"]): issue for issue in issues}
            ),
            "AGENT_READY_JSON": json.dumps(issues),
            "EVENT_LOG": str(state_dir / "events.log"),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(repo / ".codex/skills/agent-loop/scripts/agent-loop.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_script_remains_executable_and_valid_bash() -> None:
    assert stat.S_IMODE(AGENT_LOOP.stat().st_mode) == 0o755
    subprocess.run(["bash", "-n", str(AGENT_LOOP)], check=True)


def test_issue_allowlist_never_selects_unrelated_ready_work(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "2", "--dry-run"],
        issues=[_issue(1), _issue(2)],
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "Issue #2" in result.stdout
    assert "Issue #1" not in result.stdout
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit" not in gh_log


def test_merged_dependency_gate_requires_commit_on_base(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    base_sha = _run_git("rev-parse", "origin/main", cwd=consumer[0]).stdout.strip()
    blocked = _run(
        consumer,
        ["--issues", "2", "--dry-run"],
        issues=[_issue(2, "Depends on PR #7")],
        config=_config(tmp_path, dependency_gate="merged-to-base"),
        extra_env={"AGENT_PRS_JSON": json.dumps({"7": ["CLOSED", "main", base_sha]})},
    )
    assert blocked.returncode == 0
    assert "NOT merged into origin/main" in blocked.stdout

    merged = _run(
        consumer,
        ["--issues", "2", "--dry-run"],
        issues=[_issue(2, "Depends on PR #7")],
        config=_config(tmp_path, dependency_gate="merged-to-base"),
        extra_env={"AGENT_PRS_JSON": json.dumps({"7": ["MERGED", "main", base_sha]})},
    )
    assert merged.returncode == 0, merged.stderr
    assert "merged into origin/main" in merged.stdout


def test_dependency_parser_failure_blocks_the_run(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    real_python = shutil.which("python3")
    assert real_python is not None
    _write_executable(
        consumer[2] / "python3",
        """#!/usr/bin/env bash
if [ "$1" = -c ]; then exit 74; fi
exec "$AGENT_TEST_REAL_PYTHON" "$@"
""",
    )
    result = _run(
        consumer,
        ["--issues", "44"],
        issues=[_issue(44, "Depends on PR #999")],
        config=_config(tmp_path, dependency_gate="merged-to-base"),
        extra_env={"AGENT_TEST_REAL_PYTHON": real_python},
    )
    assert result.returncode != 0
    assert "could not parse issue dependencies" in result.stderr
    assert "dependency gate failed" in result.stderr
    assert not (consumer[3] / "events.log").exists()


def test_dependency_lookup_failure_blocks_the_run(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "54", "--dry-run"],
        issues=[_issue(54, "Depends on PR #999")],
        config=_config(tmp_path, dependency_gate="merged-to-base"),
    )
    assert result.returncode != 0
    assert "could not verify dependency pr #999" in result.stderr
    assert "dependency gate failed" in result.stderr


@pytest.mark.parametrize("ready_json", ["{}", "not-json"])
def test_malformed_initial_ready_queue_fails_closed(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path, ready_json: str
) -> None:
    result = _run(
        consumer,
        [],
        issues=[_issue(55)],
        config=_config(tmp_path),
        extra_env={"AGENT_READY_JSON": ready_json},
    )
    assert result.returncode != 0
    assert "could not validate ready-queue data" in result.stderr
    assert "issue selection failed" in result.stderr
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit" not in gh_log


def test_dry_run_shows_plan_without_mutation(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    worktrees = tmp_path / "worktrees"
    result = _run(
        consumer,
        ["--issues", "3", "--dry-run"],
        issues=[_issue(3)],
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "Setup hook:" in result.stdout
    assert (
        "Review order: configured Codex hook -> configured Claude hook -> repeat only after material fixes"
        in result.stdout
    )
    assert "Publication:" in result.stdout
    assert "no claim, worktree, hook, push, or PR mutation" in result.stdout
    assert not worktrees.exists()
    assert not (consumer[3] / "events.log").exists()


def test_per_issue_worktrees_and_hook_order(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "4,5", "--iterations", "2"],
        issues=[_issue(4), _issue(5)],
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    paths = re.findall(r"^   Worktree: (.+)$", result.stdout, re.MULTILINE)
    assert len(paths) == 2
    assert paths[0] != paths[1]
    assert all(not Path(path).exists() for path in paths)
    events = (consumer[3] / "events.log").read_text(encoding="utf-8").splitlines()
    expected = [
        "setup",
        "worker",
        "validate",
        "validate",
        "codex",
        "validate",
        "claude",
        "validate",
        "validate",
    ]
    assert events == expected * 2
    remote_branches = _agent_loop_branches(consumer[1])
    assert "issue-4" in remote_branches
    assert "issue-5" in remote_branches
    pr_bodies = list((tmp_path / "logs").glob("*/pr-body.md"))
    assert len(pr_bodies) == 2
    for body_file in pr_bodies:
        body = body_file.read_text(encoding="utf-8")
        assert "configured setup hook completed" in body
        assert "isolated dependency bootstrap" not in body
        assert "Local Codex and Claude review is running" in body
    final_bodies = list((tmp_path / "logs").glob("*/pr-body-final.md"))
    assert len(final_bodies) == 2
    for body_file in final_bodies:
        body = body_file.read_text(encoding="utf-8")
        assert "reported no material fixes in a complete round" in body
        assert "Configured Codex and Claude review hooks reported" in body
        assert "configured non-mutating local validation hook" in body
        assert "local Claude deep grill" not in body


def test_outer_review_environment_is_not_leaked_to_worker(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    worker_hook = (
        "if env | grep -q '^AGENT_LOOP_REVIEW_'; then exit 71; fi; "
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: isolated worker state'"
    )
    result = _run(
        consumer,
        ["--issues", "39"],
        issues=[_issue(39)],
        config=_config(tmp_path, worker_hook=worker_hook),
        extra_env={
            "AGENT_LOOP_REVIEW_BASE": "stale-base",
            "AGENT_LOOP_REVIEW_BASE_SHA": "stale-sha",
            "AGENT_LOOP_REVIEW_ENGINE": "stale-engine",
            "AGENT_LOOP_REVIEW_OUTCOME_FILE": "/tmp/stale-outcome",
            "AGENT_LOOP_REVIEW_ROUND": "99",
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_ambient_gh_repo_is_replaced_with_checkout_repository(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    worker_hook = (
        'test "$GH_REPO" = fixture/consumer; '
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: pinned repository context'"
    )
    result = _run(
        consumer,
        ["--issues", "56"],
        issues=[_issue(56)],
        config=_config(tmp_path, worker_hook=worker_hook),
        extra_env={"GH_REPO": "other/repository"},
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_unclassified_review_fix_defaults_material_and_restarts_at_codex(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    claude_hook = (
        "printf 'claude\\n' >> \"$EVENT_LOG\"; "
        'if [ ! -e "$AGENT_STATE_DIR/claude-fixed" ]; then '
        'touch "$AGENT_STATE_DIR/claude-fixed"; '
        "printf 'claude fix\\n' >> result.txt; git add result.txt; "
        "git commit -m 'fix: claude review'; fi"
    )
    result = _run(
        consumer,
        ["--issues", "18"],
        issues=[_issue(18)],
        config=_config(tmp_path, claude_review_hook=claude_hook),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "reported no material fixes in a complete round after 2 round(s)" in result.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8").splitlines()
    assert events == [
        "setup",
        "worker",
        "validate",
        "validate",
        "codex",
        "validate",
        "claude",
        "validate",
        "codex",
        "validate",
        "claude",
        "validate",
        "validate",
    ]


def test_codex_material_fix_restarts_the_next_round_at_codex(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = (
        "printf 'codex\\n' >> \"$EVENT_LOG\"; "
        "if [ ! -e \"$AGENT_STATE_DIR/codex-fixed\" ]; then "
        "touch \"$AGENT_STATE_DIR/codex-fixed\"; "
        "printf 'codex fix\\n' >> result.txt; git add result.txt; "
        "git commit -m 'fix: codex material review'; fi"
    )
    result = _run(
        consumer,
        ["--issues", "46"],
        issues=[_issue(46)],
        config=_config(tmp_path, codex_review_hook=codex_hook),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8").splitlines()
    assert events == [
        "setup",
        "worker",
        "validate",
        "validate",
        "codex",
        "validate",
        "claude",
        "validate",
        "codex",
        "validate",
        "claude",
        "validate",
        "validate",
    ]


def test_minor_only_review_fixes_converge_without_restarting(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = (
        "printf 'codex\\n' >> \"$EVENT_LOG\"; "
        "printf 'codex minor\\n' >> result.txt; git add result.txt; "
        "git commit -m 'docs: codex minor review polish'; "
        "printf 'minor\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\""
    )
    claude_hook = (
        "printf 'claude\\n' >> \"$EVENT_LOG\"; "
        "printf 'claude minor\\n' >> result.txt; git add result.txt; "
        "git commit -m 'test: claude minor review polish'; "
        "printf 'minor\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\""
    )
    result = _run(
        consumer,
        ["--issues", "36"],
        issues=[_issue(36)],
        config=_config(
            tmp_path,
            codex_review_hook=codex_hook,
            claude_review_hook=claude_hook,
        ),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "reported no material fixes in a complete round after 1 round(s)" in result.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8").splitlines()
    assert events == [
        "setup",
        "worker",
        "validate",
        "validate",
        "codex",
        "validate",
        "claude",
        "validate",
        "validate",
    ]
    branch = _agent_loop_branches(consumer[1]).strip()
    published = _run_git("show", f"{branch}:result.txt", cwd=consumer[1]).stdout
    assert "codex minor" in published
    assert "claude minor" in published


@pytest.mark.parametrize(
    "classification_command",
    [
        "printf 'clean\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\"",
        "printf ' minor\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\"",
        "printf 'minor\\n\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\"",
        "printf 'minor\\0' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\"",
    ],
)
def test_committed_review_with_invalid_classification_blocks_publication(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    classification_command: str,
) -> None:
    claude_hook = (
        "printf 'claude\\n' >> \"$EVENT_LOG\"; "
        "printf 'fix\\n' >> result.txt; git add result.txt; "
        "git commit -m 'fix: classified incorrectly'; "
        + classification_command
    )
    result = _run(
        consumer,
        ["--issues", "37"],
        issues=[_issue(37)],
        config=_config(tmp_path, claude_review_hook=claude_hook),
    )
    assert result.returncode != 0
    assert "outcome must be exactly 'minor' or 'material'" in result.stderr
    assert "Worktree preserved:" in result.stderr
    remote_branches = _agent_loop_branches(consumer[1])
    assert "issue-37" in remote_branches
    assert not (consumer[3] / "pr-ready").exists()


def test_review_classification_without_commit_blocks_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = "printf 'minor\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\""
    result = _run(
        consumer,
        ["--issues", "48"],
        issues=[_issue(48)],
        config=_config(tmp_path, codex_review_hook=codex_hook),
    )
    assert result.returncode != 0
    assert "wrote a fix classification without committing a fix" in result.stderr
    assert "Worktree preserved:" in result.stderr
    assert "issue-48" in _agent_loop_branches(consumer[1])
    assert not (consumer[3] / "pr-ready").exists()


def test_validation_cannot_change_accepted_review_classification(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = (
        "touch \"$AGENT_STATE_DIR/codex-reviewed\"; "
        "printf 'minor fix\\n' >> result.txt; git add result.txt; "
        "git commit -m 'docs: minor review fix'; "
        "printf 'minor\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\""
    )
    validation_hook = (
        "printf 'validate\\n' >> \"$EVENT_LOG\"; "
        "if [ -e \"$AGENT_STATE_DIR/codex-reviewed\" ]; then "
        "printf 'material\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\"; fi"
    )
    result = _run(
        consumer,
        ["--issues", "38"],
        issues=[_issue(38)],
        config=_config(
            tmp_path,
            codex_review_hook=codex_hook,
            validation_hook=validation_hook,
        ),
    )
    assert result.returncode != 0
    assert "classification changed during validation" in result.stderr
    assert "issue-38" in _agent_loop_branches(consumer[1])
    assert not (consumer[3] / "pr-ready").exists()


def test_validation_cannot_create_a_missing_material_outcome(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = (
        "touch \"$AGENT_STATE_DIR/codex-reviewed\"; "
        "printf 'material fix\\n' >> result.txt; git add result.txt; "
        "git commit -m 'fix: material review fix'"
    )
    validation_hook = (
        "printf 'validate\\n' >> \"$EVENT_LOG\"; "
        "if [ -e \"$AGENT_STATE_DIR/codex-reviewed\" ]; then "
        "printf 'material\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\"; fi"
    )
    result = _run(
        consumer,
        ["--issues", "47"],
        issues=[_issue(47)],
        config=_config(
            tmp_path,
            codex_review_hook=codex_hook,
            validation_hook=validation_hook,
        ),
    )
    assert result.returncode != 0
    assert "outcome file changed during validation" in result.stderr
    assert "issue-47" in _agent_loop_branches(consumer[1])
    assert not (consumer[3] / "pr-ready").exists()


def test_later_reviewer_cannot_change_an_accepted_outcome(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = (
        "printf 'minor fix\\n' >> result.txt; git add result.txt; "
        "git commit -m 'docs: minor review fix'; "
        "printf 'minor\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\""
    )
    claude_hook = (
        "printf 'material\\n' > "
        '"$AGENT_LOOP_LOG_DIR/codex-review-round-$AGENT_LOOP_REVIEW_ROUND.outcome"'
    )
    result = _run(
        consumer,
        ["--issues", "49"],
        issues=[_issue(49)],
        config=_config(
            tmp_path,
            codex_review_hook=codex_hook,
            claude_review_hook=claude_hook,
        ),
    )
    assert result.returncode != 0
    assert "Codex review outcome file changed" in result.stderr
    assert "Worktree preserved:" in result.stderr
    assert "issue-49" in _agent_loop_branches(consumer[1])
    assert not (consumer[3] / "pr-ready").exists()


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_final_validation_cannot_change_an_accepted_outcome(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path, engine: str
) -> None:
    review_hook = (
        f'touch "$AGENT_STATE_DIR/{engine}-reviewed"; '
        "printf 'minor fix\\n' >> result.txt; git add result.txt; "
        "git commit -m 'docs: minor review fix'; "
        "printf 'minor\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\""
    )
    validation_hook = (
        "printf 'validate\\n' >> \"$EVENT_LOG\"; "
        'if [ -z "${AGENT_LOOP_REVIEW_ENGINE:-}" ] && '
        f'[ -e "$AGENT_STATE_DIR/{engine}-reviewed" ]; then '
        "printf 'material\\n' > "
        f'"$AGENT_LOOP_LOG_DIR/{engine}-review-round-1.outcome"; fi'
    )
    result = _run(
        consumer,
        ["--issues", "50"],
        issues=[_issue(50)],
        config=_config(
            tmp_path,
            validation_hook=validation_hook,
            **{f"{engine}_review_hook": review_hook},
        ),
    )
    assert result.returncode != 0
    assert f"{engine.title()} review outcome file changed" in result.stderr
    assert "Worktree preserved:" in result.stderr
    assert "issue-50" in _agent_loop_branches(consumer[1])
    assert not (consumer[3] / "pr-ready").exists()


def test_review_cap_preserves_non_converged_worktree_and_blocks_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = (
        "printf 'codex\\n' >> \"$EVENT_LOG\"; "
        "printf 'codex round %s\\n' \"$AGENT_LOOP_REVIEW_ROUND\" >> result.txt; "
        'git add result.txt; git commit -m "fix: codex round $AGENT_LOOP_REVIEW_ROUND"'
    )
    result = _run(
        consumer,
        ["--issues", "19"],
        issues=[_issue(19)],
        config=_config(
            tmp_path,
            codex_review_hook=codex_hook,
            review_max_rounds=2,
        ),
    )
    assert result.returncode != 0
    assert "did not converge within 2 round(s)" in result.stderr
    match = re.search(r"Worktree preserved: (.+)", result.stderr)
    assert match
    assert Path(match.group(1)).exists()
    remote_branches = _agent_loop_branches(consumer[1])
    assert "issue-19" in remote_branches
    assert not (consumer[3] / "pr-ready").exists()


@pytest.mark.parametrize(
    ("resolved", "comments"),
    [
        (
            False,
            [
                {
                    "body": "<!-- local-review:v1 engine=codex round=1 head=a fingerprint=f -->",
                    "databaseId": 1,
                },
                {"body": "Fixed in abc123.", "databaseId": 2},
            ],
        ),
        (
            True,
            [
                {
                    "body": "<!-- local-review:v1 engine=claude round=1 head=a fingerprint=f -->",
                    "databaseId": 1,
                }
            ],
        ),
    ],
)
def test_local_review_thread_requires_reply_and_resolution_before_ready(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    resolved: bool,
    comments: list[dict[str, object]],
) -> None:
    threads = [
        {
            "isResolved": resolved,
            "comments": {
                "nodes": comments,
                "pageInfo": {"hasNextPage": False},
            },
        }
    ]
    result = _run(
        consumer,
        ["--issues", "69"],
        issues=[_issue(69)],
        config=_config(tmp_path),
        extra_env={"AGENT_REVIEW_THREADS_JSON": json.dumps(threads)},
    )
    assert result.returncode != 0
    assert "must contain a disposition reply and be resolved" in result.stderr
    assert "issue-69" in _agent_loop_branches(consumer[1])
    assert not (consumer[3] / "pr-ready").exists()


def test_review_contract_version_is_required_before_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    legacy_config = _config(tmp_path).replace("review_contract_version = 2\n", "")
    result = _run(
        consumer,
        ["--issues", "20"],
        issues=[_issue(20)],
        config=legacy_config,
    )
    assert result.returncode != 0
    assert "review_contract_version = 2" in result.stderr
    gh_log = consumer[3] / "gh.log"
    assert not gh_log.exists() or "issue edit" not in gh_log.read_text(encoding="utf-8")
    assert not (tmp_path / "worktrees").exists()


@pytest.mark.parametrize("timeout_key", ["worker_timeout_seconds", "hook_timeout_seconds"])
def test_zero_timeout_is_rejected_before_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path, timeout_key: str
) -> None:
    result = _run(
        consumer,
        ["--issues", "57"],
        issues=[_issue(57)],
        config=_config(tmp_path, **{timeout_key: 0}),
    )
    assert result.returncode != 0
    assert f"{timeout_key} must be a positive integer" in result.stderr
    gh_log = consumer[3] / "gh.log"
    assert not gh_log.exists() or "issue edit" not in gh_log.read_text(encoding="utf-8")
    assert not (tmp_path / "worktrees").exists()


def test_validation_hook_is_required_before_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "21"],
        issues=[_issue(21)],
        config=_config(tmp_path, validation_hook=""),
    )
    assert result.returncode != 0
    assert "validation_hook must be configured" in result.stderr
    gh_log = consumer[3] / "gh.log"
    assert not gh_log.exists() or "issue edit" not in gh_log.read_text(encoding="utf-8")
    assert not (tmp_path / "worktrees").exists()


def test_issue_context_extraction_failure_stops_before_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    real_jq = shutil.which("jq")
    assert real_jq is not None
    _write_executable(
        consumer[2] / "jq",
        """#!/usr/bin/env bash
if [[ "$*" == *'.body'* ]]; then
    exit 73
fi
exec "$AGENT_TEST_REAL_JQ" "$@"
""",
    )
    result = _run(
        consumer,
        ["--issues", "30"],
        issues=[_issue(30, "Material issue requirements")],
        config=_config(tmp_path),
        extra_env={"AGENT_TEST_REAL_JQ": real_jq},
    )
    assert result.returncode != 0
    assert "issue selection failed" in result.stderr
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit" not in gh_log
    assert not (tmp_path / "worktrees").exists()


def test_failed_claim_rollback_is_reported_and_stops(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "43"],
        issues=[_issue(43)],
        config=_config(tmp_path),
        extra_env={
            "AGENT_VERIFIED_ASSIGNEE": "someone-else",
            "AGENT_FAIL_REMOVE_ASSIGNEE": "true",
        },
    )
    assert result.returncode != 0
    assert "operator action is required" in result.stderr
    assert (consumer[3] / "claimed-43").exists()
    assert "issue-43" not in _agent_loop_branches(consumer[1])


def test_validation_commit_after_claude_blocks_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    validation_hook = (
        "printf 'validate\\n' >> \"$EVENT_LOG\"; "
        'if [ "${AGENT_LOOP_REVIEW_ENGINE:-}" = claude ]; then '
        "printf 'validation fix\\n' >> result.txt; git add result.txt; "
        "git commit -m 'fix: validation mutation'; fi"
    )
    result = _run(
        consumer,
        ["--issues", "22"],
        issues=[_issue(22)],
        config=_config(tmp_path, validation_hook=validation_hook),
    )
    assert result.returncode != 0
    assert "validation mutated the worktree or HEAD" in result.stderr
    assert "Worktree preserved:" in result.stderr
    remote_branches = _agent_loop_branches(consumer[1])
    assert "issue-22" in remote_branches
    assert not (consumer[3] / "pr-ready").exists()


def test_setup_commit_cannot_satisfy_worker_commit_requirement(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    setup_hook = (
        "printf 'setup-only\\n' > setup-only.txt; git add setup-only.txt; "
        "git commit -m 'chore: setup must not commit'"
    )
    result = _run(
        consumer,
        ["--issues", "31"],
        issues=[_issue(31)],
        config=_config(tmp_path, setup_hook=setup_hook, worker_hook=":"),
    )
    assert result.returncode != 0
    assert "Setup hook changed HEAD" in result.stderr
    assert "Worktree preserved:" in result.stderr
    remote_branches = _agent_loop_branches(consumer[1])
    assert "issue-31" not in remote_branches


def test_detached_reviewer_commit_blocks_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = (
        "printf 'codex\\n' >> \"$EVENT_LOG\"; "
        'if [ ! -e "$AGENT_STATE_DIR/detached-reviewed" ]; then '
        'touch "$AGENT_STATE_DIR/detached-reviewed"; '
        "git checkout --detach >/dev/null; "
        "printf 'detached review fix\\n' >> result.txt; git add result.txt; "
        "git commit -m 'fix: detached review'; fi"
    )
    result = _run(
        consumer,
        ["--issues", "23"],
        issues=[_issue(23)],
        config=_config(tmp_path, codex_review_hook=codex_hook),
    )
    assert result.returncode != 0
    assert "moved HEAD away from the issue branch" in result.stderr
    match = re.search(r"Worktree preserved: (.+)", result.stderr)
    assert match
    preserved = Path(match.group(1))
    assert (
        "detached review fix"
        in _run_git("show", "HEAD:result.txt", cwd=preserved).stdout
    )
    remote_branches = _agent_loop_branches(consumer[1])
    assert "issue-23" in remote_branches
    assert not (consumer[3] / "pr-ready").exists()


def test_ambiguous_origin_tag_cannot_spoof_remote_base(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    repo, remote, _, _ = consumer
    stale_base = _run_git(
        "rev-parse", "refs/remotes/origin/main", cwd=repo
    ).stdout.strip()
    clone = tmp_path / "advanced-base"
    _clone_test_repo(remote, clone)
    (clone / "fresh-base.txt").write_text("fresh\n", encoding="utf-8")
    _run_git("add", "fresh-base.txt", cwd=clone)
    _run_git("commit", "-m", "chore: advance base", cwd=clone)
    _run_git("push", "origin", "main", cwd=clone)
    _run_git("update-ref", "refs/tags/origin/main", stale_base, cwd=repo)

    worker_hook = (
        "test -f fresh-base.txt; printf 'worker\\n' >> \"$EVENT_LOG\"; "
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: worker from remote base'"
    )
    result = _run(
        consumer,
        ["--issues", "24"],
        issues=[_issue(24)],
        config=_config(tmp_path, worker_hook=worker_hook),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branch = _agent_loop_branches(remote).strip()
    assert _run_git("show", f"{branch}:fresh-base.txt", cwd=remote).stdout == "fresh\n"


def test_hook_guard_installation_failure_stops_before_hook_execution(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, bin_dir, state_dir = consumer
    base_before = _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    _write_executable(bin_dir / "cp", "#!/usr/bin/env bash\nexit 73\n")
    result = _run(
        consumer,
        ["--issues", "40"],
        issues=[_issue(40)],
        config=_config(tmp_path),
    )
    assert result.returncode != 0
    assert "could not install hook Git guard" in result.stderr
    assert not (state_dir / "events.log").exists()
    assert _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip() == base_before
    assert "issue-40" not in _agent_loop_branches(remote)


def test_hook_origin_url_change_blocks_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, _, _ = consumer
    redirected = tmp_path / "redirected.git"
    _run_git("init", "--bare", str(redirected))
    worker_hook = (
        "git remote set-url --push origin \"$REDIRECTED_REMOTE\"; "
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: worker after redirect'"
    )
    result = _run(
        consumer,
        ["--issues", "41"],
        issues=[_issue(41)],
        config=_config(tmp_path, worker_hook=worker_hook),
        extra_env={"REDIRECTED_REMOTE": str(redirected)},
    )
    assert result.returncode != 0
    assert "hook changed origin fetch/push identity" in result.stderr
    assert "issue-41" not in _agent_loop_branches(remote)
    assert "issue-41" not in _agent_loop_branches(redirected)


def test_hook_origin_fetch_url_change_blocks_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    repo, remote, _, _ = consumer
    redirected = tmp_path / "redirected-fetch.git"
    _run_git("init", "--bare", str(redirected))
    _run_git("config", "remote.origin.pushurl", str(remote), cwd=repo)
    worker_hook = (
        'git remote set-url origin "$REDIRECTED_REMOTE"; '
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: worker after fetch redirect'"
    )
    result = _run(
        consumer,
        ["--issues", "58"],
        issues=[_issue(58)],
        config=_config(tmp_path, worker_hook=worker_hook),
        extra_env={"REDIRECTED_REMOTE": str(redirected)},
    )
    assert result.returncode != 0
    assert "hook changed origin fetch/push identity" in result.stderr
    assert "issue-58" not in _agent_loop_branches(remote)


def test_post_review_git_status_failure_blocks_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, bin_dir, _ = consumer
    real_git = shutil.which("git")
    assert real_git is not None
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
if [ "$1" = status ]; then
    count_file="$AGENT_STATE_DIR/status-count"
    count=$(($(cat "$count_file" 2>/dev/null || echo 0) + 1))
    printf '%s\n' "$count" > "$count_file"
    if [ "$count" -eq 6 ]; then exit 73; fi
fi
exec "$AGENT_TEST_REAL_GIT" "$@"
""",
    )
    result = _run(
        consumer,
        ["--issues", "42"],
        issues=[_issue(42)],
        config=_config(tmp_path),
        extra_env={"AGENT_TEST_REAL_GIT": real_git},
    )
    assert result.returncode != 0
    assert "Could not inspect Git status after the configured Codex review hook" in result.stderr
    assert "issue-42" in _agent_loop_branches(remote)
    assert not (consumer[3] / "pr-ready").exists()


def test_hook_origin_push_is_disabled_until_wrapper_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    repo, remote, _, _ = consumer
    base_before = _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    # A configured pushurl is multi-valued, so merely appending a disabled URL
    # would still push to this real destination before failing on the guard URL.
    _run_git("config", "--add", "remote.origin.pushurl", str(remote), cwd=repo)
    worker_hook = (
        "if git push origin HEAD:refs/heads/main >/dev/null 2>&1; then exit 45; fi; "
        "printf 'worker\\n' >> \"$EVENT_LOG\"; printf 'done\\n' > result.txt; "
        "git add result.txt; git commit -m 'fix: worker after blocked push'"
    )
    codex_hook = (
        'if git -C "$PWD" push origin HEAD:refs/heads/main >/dev/null 2>&1; then exit 46; fi; '
        "printf 'codex\\n' >> \"$EVENT_LOG\""
    )
    result = _run(
        consumer,
        ["--issues", "25"],
        issues=[_issue(25)],
        config=_config(
            tmp_path,
            worker_hook=worker_hook,
            codex_review_hook=codex_hook,
        ),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert (
        _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
        == base_before
    )
    issue_branches = _agent_loop_branches(remote)
    assert "issue-25" in issue_branches


@pytest.mark.parametrize(
    ("alias_name", "alias_value", "alias_invocation"),
    [
        (
            "publish",
            "push",
            "git publish origin HEAD:refs/heads/main",
        ),
        (
            "ship",
            "!git push origin HEAD:refs/heads/main",
            "git ship",
        ),
    ],
)
def test_git_alias_cannot_bypass_hook_publication_guard(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    alias_name: str,
    alias_value: str,
    alias_invocation: str,
) -> None:
    repo, remote, _, _ = consumer
    base_before = _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    _run_git("config", f"alias.{alias_name}", alias_value, cwd=repo)
    worker_hook = (
        f"if {alias_invocation} >/dev/null 2>&1; then exit 47; fi; "
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: worker after blocked alias'"
    )
    result = _run(
        consumer,
        ["--issues", "32"],
        issues=[_issue(32)],
        config=_config(tmp_path, worker_hook=worker_hook),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert (
        _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
        == base_before
    )


@pytest.mark.parametrize(
    "global_option",
    ["--attr-source HEAD", "--shallow-file /dev/null"],
)
def test_value_taking_git_global_option_cannot_hide_push(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    global_option: str,
) -> None:
    _, remote, _, _ = consumer
    worker_hook = (
        f"if git {global_option} push origin HEAD:refs/heads/global-option-bypass "
        ">/dev/null 2>&1; then exit 61; fi; "
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: worker after blocked global option'"
    )
    result = _run(
        consumer,
        ["--issues", "62"],
        issues=[_issue(62)],
        config=_config(tmp_path, worker_hook=worker_hook),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branches = _run_git(
        "for-each-ref", "--format=%(refname:short)", cwd=remote
    ).stdout
    assert "global-option-bypass" not in branches


def test_command_scoped_git_alias_cannot_bypass_hook_guard(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, _, _ = consumer
    worker_hook = (
        "if git -c alias.publish=push publish origin "
        "HEAD:refs/heads/scoped-alias-bypass >/dev/null 2>&1; then exit 62; fi; "
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: worker after blocked scoped alias'"
    )
    result = _run(
        consumer,
        ["--issues", "63"],
        issues=[_issue(63)],
        config=_config(tmp_path, worker_hook=worker_hook),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branches = _run_git(
        "for-each-ref", "--format=%(refname:short)", cwd=remote
    ).stdout
    assert "scoped-alias-bypass" not in branches


def test_git_autocorrection_cannot_turn_typo_into_push(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    repo, remote, _, _ = consumer
    _run_git("config", "help.autocorrect", "immediate", cwd=repo)
    worker_hook = (
        "if git pus origin HEAD:refs/heads/autocorrect-bypass >/dev/null 2>&1; "
        "then exit 63; fi; printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: worker after disabled autocorrect'"
    )
    result = _run(
        consumer,
        ["--issues", "64"],
        issues=[_issue(64)],
        config=_config(tmp_path, worker_hook=worker_hook),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branches = _run_git(
        "for-each-ref", "--format=%(refname:short)", cwd=remote
    ).stdout
    assert "autocorrect-bypass" not in branches


def test_login_startup_git_function_cannot_bypass_hook_guard(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, _, _ = consumer
    real_git = shutil.which("git")
    assert real_git is not None
    bash_env = tmp_path / "hook-bash-env"
    bash_env.write_text(
        'git() { "$AGENT_TEST_REAL_GIT" "$@"; }\nexport -f git\n',
        encoding="utf-8",
    )
    worker_hook = (
        "if git push origin HEAD:refs/heads/startup-function-bypass "
        ">/dev/null 2>&1; then exit 64; fi; "
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: worker after cleared startup function'"
    )
    result = _run(
        consumer,
        ["--issues", "65"],
        issues=[_issue(65)],
        config=_config(tmp_path, worker_hook=worker_hook),
        extra_env={"AGENT_TEST_REAL_GIT": real_git, "BASH_ENV": str(bash_env)},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branches = _run_git(
        "for-each-ref", "--format=%(refname:short)", cwd=remote
    ).stdout
    assert "startup-function-bypass" not in branches


def test_exported_git_function_cannot_bypass_hook_publication_guard(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, _, _ = consumer
    real_git = shutil.which("git")
    assert real_git is not None
    worker_hook = (
        "if git push origin HEAD:refs/heads/function-bypass >/dev/null 2>&1; "
        "then exit 49; fi; "
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: worker after blocked function'"
    )
    result = _run(
        consumer,
        ["--issues", "51"],
        issues=[_issue(51)],
        config=_config(tmp_path, worker_hook=worker_hook),
        extra_env={
            "AGENT_TEST_REAL_GIT": real_git,
            "BASH_FUNC_git%%": '() { "$AGENT_TEST_REAL_GIT" "$@"; }',
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branches = _run_git(
        "for-each-ref", "--format=%(refname:short)", cwd=remote
    ).stdout
    assert "function-bypass" not in branches


def test_hook_preserves_auth_environment_for_git_credential_reads(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    repo, _, _, _ = consumer
    _run_git(
        "config",
        "credential.helper",
        "!gh auth git-credential",
        cwd=repo,
    )
    worker_hook = (
        "credential=\"$(printf 'protocol=https\\nhost=github.com\\n\\n' | "
        'git credential fill)"; '
        "grep -Fx 'password=caller-token' <<<\"$credential\" >/dev/null; "
        'if gh issue view "$AGENT_LOOP_ISSUE_ID" >/dev/null 2>&1; then exit 48; fi; '
        "printf 'done\\n' > result.txt; git add result.txt; "
        "git commit -m 'fix: authenticated git read'"
    )
    result = _run(
        consumer,
        ["--issues", "33"],
        issues=[_issue(33)],
        config=_config(tmp_path, worker_hook=worker_hook),
        extra_env={
            "GH_TOKEN": "caller-token",
            "GITHUB_TOKEN": "caller-token",
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_reviewer_history_rewrite_blocks_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = (
        'git reset --hard "$AGENT_LOOP_REVIEW_BASE_SHA" >/dev/null; '
        "printf 'replacement\\n' > replacement.txt; git add replacement.txt; "
        "git commit -m 'fix: replace reviewed history'"
    )
    result = _run(
        consumer,
        ["--issues", "26"],
        issues=[_issue(26)],
        config=_config(tmp_path, codex_review_hook=codex_hook),
    )
    assert result.returncode != 0
    assert "Configured Codex review hook failed" in result.stderr
    assert "Worktree preserved:" in result.stderr
    remote_branches = _agent_loop_branches(consumer[1])
    assert "issue-26" in remote_branches
    assert not (consumer[3] / "pr-ready").exists()


def test_non_fast_forward_base_rewrite_after_review_blocks_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, _, _ = consumer
    original_base = _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    clone = tmp_path / "base-rewriter"
    _clone_test_repo(remote, clone)

    (clone / "reviewed-base.txt").write_text("reviewed\n", encoding="utf-8")
    _run_git("add", "reviewed-base.txt", cwd=clone)
    _run_git("commit", "-m", "chore: reviewed base", cwd=clone)
    reviewed_base = _run_git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _run_git("push", "origin", "main", cwd=clone)

    _run_git("checkout", "--detach", original_base, cwd=clone)
    (clone / "replacement-base.txt").write_text("replacement\n", encoding="utf-8")
    _run_git("add", "replacement-base.txt", cwd=clone)
    _run_git("commit", "-m", "chore: replacement base", cwd=clone)
    replacement_base = _run_git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _run_git(
        "push", "origin", f"{replacement_base}:refs/heads/rewrite-candidate", cwd=clone
    )
    _run_git("push", "origin", ":refs/heads/rewrite-candidate", cwd=clone)

    claude_hook = (
        'git --git-dir="$REMOTE_PATH" update-ref refs/heads/main "$REPLACEMENT_BASE"; '
        "printf 'claude\\n' >> \"$EVENT_LOG\""
    )
    result = _run(
        consumer,
        ["--issues", "27"],
        issues=[_issue(27)],
        config=_config(tmp_path, claude_review_hook=claude_hook),
        extra_env={
            "REMOTE_PATH": str(remote),
            "REPLACEMENT_BASE": replacement_base,
        },
    )
    assert result.returncode != 0
    assert "moved non-fast-forward during review round" in result.stderr
    assert (
        _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
        == replacement_base
    )
    assert (
        _run_git(
            "merge-base", reviewed_base, replacement_base, cwd=remote
        ).stdout.strip()
        == original_base
    )
    remote_branches = _agent_loop_branches(remote)
    assert "issue-27" in remote_branches
    assert not (consumer[3] / "pr-ready").exists()


def test_fresh_base_merge_uses_sha_validated_before_shared_ref_moves(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    repo, remote, bin_dir, state_dir = consumer
    original_base = _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    clone = tmp_path / "base-race-builder"
    _clone_test_repo(remote, clone)

    (clone / "reviewed-base.txt").write_text("reviewed\n", encoding="utf-8")
    _run_git("add", "reviewed-base.txt", cwd=clone)
    _run_git("commit", "-m", "chore: reviewed base", cwd=clone)
    reviewed_base = _run_git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _run_git("push", "origin", "main", cwd=clone)

    _run_git("checkout", "--detach", original_base, cwd=clone)
    (clone / "replacement-base.txt").write_text("replacement\n", encoding="utf-8")
    _run_git("add", "replacement-base.txt", cwd=clone)
    _run_git("commit", "-m", "chore: unvalidated replacement", cwd=clone)
    replacement_base = _run_git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _run_git(
        "push",
        "origin",
        f"{replacement_base}:refs/heads/replacement-candidate",
        cwd=clone,
    )
    _run_git(
        "fetch",
        "origin",
        "refs/heads/replacement-candidate:refs/test/replacement-candidate",
        cwd=repo,
    )
    _run_git("push", "origin", ":refs/heads/replacement-candidate", cwd=clone)

    real_git = shutil.which("git")
    assert real_git is not None
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
"$AGENT_TEST_REAL_GIT" "$@"
status=$?
if [ "$status" -eq 0 ] && [ "${1:-}" = merge-base ] && \
   [ "${2:-}" = --is-ancestor ] && \
   [ "${3:-}" = "$AGENT_TEST_REVIEWED_BASE" ] && \
   [ "${4:-}" = "$AGENT_TEST_REVIEWED_BASE" ] && \
   [ ! -e "$AGENT_STATE_DIR/base-ref-moved" ]; then
    "$AGENT_TEST_REAL_GIT" update-ref refs/remotes/origin/main \
        "$AGENT_TEST_REPLACEMENT_BASE"
    touch "$AGENT_STATE_DIR/base-ref-moved"
fi
exit "$status"
""",
    )
    result = _run(
        consumer,
        ["--issues", "36"],
        issues=[_issue(36)],
        config=_config(tmp_path),
        extra_env={
            "AGENT_TEST_REAL_GIT": real_git,
            "AGENT_TEST_REVIEWED_BASE": reviewed_base,
            "AGENT_TEST_REPLACEMENT_BASE": replacement_base,
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert (state_dir / "base-ref-moved").exists()
    branch = _agent_loop_branches(remote).strip()
    replacement_is_parent = subprocess.run(
        [
            "git",
            f"--git-dir={remote}",
            "merge-base",
            "--is-ancestor",
            replacement_base,
            branch,
        ],
        check=False,
    )
    assert replacement_is_parent.returncode != 0


def test_remote_branch_created_after_absence_check_is_not_overwritten(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, bin_dir, state_dir = consumer
    base_sha = _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    real_git = shutil.which("git")
    assert real_git is not None
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
if [ "${1:-}" = ls-remote ] && [ ! -e "$AGENT_STATE_DIR/remote-branch-raced" ]; then
    "$AGENT_TEST_REAL_GIT" "$@"
    status=$?
    if [ "$status" -ne 0 ]; then
        "$AGENT_TEST_REAL_GIT" --git-dir="$AGENT_TEST_REMOTE" update-ref \
            "refs/heads/$AGENT_LOOP_BRANCH" "$AGENT_TEST_BASE_SHA"
        touch "$AGENT_STATE_DIR/remote-branch-raced"
    fi
    exit "$status"
fi
exec "$AGENT_TEST_REAL_GIT" "$@"
""",
    )
    result = _run(
        consumer,
        ["--issues", "37"],
        issues=[_issue(37)],
        config=_config(tmp_path),
        extra_env={
            "AGENT_TEST_REAL_GIT": real_git,
            "AGENT_TEST_REMOTE": str(remote),
            "AGENT_TEST_BASE_SHA": base_sha,
        },
    )
    assert result.returncode != 0
    assert (state_dir / "remote-branch-raced").exists()
    branches = _agent_loop_branches(remote).splitlines()
    assert len(branches) == 1
    assert _run_git("rev-parse", branches[0], cwd=remote).stdout.strip() == base_sha
    gh_log = (state_dir / "gh.log").read_text(encoding="utf-8")
    assert "pr create" not in gh_log


def test_remote_branch_rewrite_after_push_blocks_pr_creation(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, bin_dir, state_dir = consumer
    base_sha = _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    real_git = shutil.which("git")
    assert real_git is not None
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
"$AGENT_TEST_REAL_GIT" "$@"
status=$?
if [ "$status" -eq 0 ] && [ "${1:-}" = push ] && \
   [[ " $* " == *" --force-with-lease="* ]] && \
   [ ! -e "$AGENT_STATE_DIR/remote-branch-rewritten" ]; then
    "$AGENT_TEST_REAL_GIT" --git-dir="$AGENT_TEST_REMOTE" update-ref \
        "refs/heads/$AGENT_LOOP_BRANCH" "$AGENT_TEST_BASE_SHA"
    touch "$AGENT_STATE_DIR/remote-branch-rewritten"
fi
exit "$status"
""",
    )
    result = _run(
        consumer,
        ["--issues", "52"],
        issues=[_issue(52)],
        config=_config(tmp_path),
        extra_env={
            "AGENT_TEST_REAL_GIT": real_git,
            "AGENT_TEST_REMOTE": str(remote),
            "AGENT_TEST_BASE_SHA": base_sha,
        },
    )
    assert result.returncode != 0
    assert (state_dir / "remote-branch-rewritten").exists()
    assert "remote issue branch changed after draft publication push" in result.stderr
    gh_log = (state_dir / "gh.log").read_text(encoding="utf-8")
    assert "pr create" not in gh_log


def test_remote_branch_rewrite_after_first_attestation_blocks_pr_creation(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, bin_dir, state_dir = consumer
    base_sha = _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    real_git = shutil.which("git")
    assert real_git is not None
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
"$AGENT_TEST_REAL_GIT" "$@"
status=$?
if [ "$status" -eq 0 ] && [ "${1:-}" = ls-remote ] && \
   [[ " $* " == *" refs/heads/$AGENT_LOOP_BRANCH "* ]] && \
   [ ! -e "$AGENT_STATE_DIR/first-attestation-rewritten" ]; then
    "$AGENT_TEST_REAL_GIT" --git-dir="$AGENT_TEST_REMOTE" update-ref \
        "refs/heads/$AGENT_LOOP_BRANCH" "$AGENT_TEST_BASE_SHA"
    touch "$AGENT_STATE_DIR/first-attestation-rewritten"
fi
exit "$status"
""",
    )
    result = _run(
        consumer,
        ["--issues", "66"],
        issues=[_issue(66)],
        config=_config(tmp_path),
        extra_env={
            "AGENT_TEST_REAL_GIT": real_git,
            "AGENT_TEST_REMOTE": str(remote),
            "AGENT_TEST_BASE_SHA": base_sha,
        },
    )
    assert result.returncode != 0
    assert (state_dir / "first-attestation-rewritten").exists()
    assert "created draft PR head could not be attested" in result.stderr
    gh_log = (state_dir / "gh.log").read_text(encoding="utf-8")
    assert "pr create --draft" in gh_log
    assert "pr close https://example.invalid/pr/1" in gh_log


def test_created_pr_head_mismatch_closes_pr_and_preserves_worktree(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, remote, _, state_dir = consumer
    base_sha = _run_git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    result = _run(
        consumer,
        ["--issues", "67"],
        issues=[_issue(67)],
        config=_config(tmp_path),
        extra_env={"AGENT_PR_HEAD_OID": base_sha},
    )
    assert result.returncode != 0
    assert "created draft PR head could not be attested" in result.stderr
    assert (state_dir / "pr-closed").exists()
    gh_log = (state_dir / "gh.log").read_text(encoding="utf-8")
    assert "pr create" in gh_log
    assert "pr close https://example.invalid/pr/1" in gh_log
    match = re.search(r"Worktree preserved: (.+)", result.stderr)
    assert match and Path(match.group(1)).exists()


def test_default_worker_model_is_passed_as_one_shell_safe_argument(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex = consumer[2] / "codex"
    _write_executable(
        codex,
        """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$AGENT_STATE_DIR/model-args.log"
printf 'done\\n' > result.txt
git add result.txt
git commit -m 'fix: shell-safe model worker'
""",
    )
    injected = tmp_path / "model-injection-ran"
    model = 'model\'; touch "$INJECTED"; #'
    result = _run(
        consumer,
        ["--issues", "28"],
        issues=[_issue(28)],
        config=_config(tmp_path, worker_hook="", worker_model=model),
        extra_env={"INJECTED": str(injected)},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert not injected.exists()
    args = (consumer[3] / "model-args.log").read_text(encoding="utf-8").splitlines()
    assert model in args


def test_worker_receives_issue_body_without_gh(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # `gh` is masked inside every hook, so the worker cannot fetch its own issue
    # over the API. The wrapper must hand the title and body to the worker
    # through the environment, and the shipped prompt must not tell it to run
    # `gh issue view`.
    worker_hook = (
        'if gh issue view "$AGENT_LOOP_ISSUE_ID" >/dev/null 2>&1; then exit 71; fi; '
        'test -n "$AGENT_LOOP_ISSUE_BODY"; '
        'printf "%s" "$AGENT_LOOP_ISSUE_TITLE" > title.txt; '
        'printf "%s" "$AGENT_LOOP_ISSUE_BODY" > body.txt; '
        "git add title.txt body.txt; git commit -m 'fix: worker from env context'"
    )
    result = _run(
        consumer,
        ["--issues", "29"],
        issues=[_issue(29, "Implement the widget with a red border.")],
        config=_config(tmp_path, worker_hook=worker_hook),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branch = _agent_loop_branches(consumer[1]).strip()
    assert (
        _run_git("show", f"{branch}:body.txt", cwd=consumer[1]).stdout
        == "Implement the widget with a red border."
    )
    assert _run_git("show", f"{branch}:title.txt", cwd=consumer[1]).stdout == "Issue 29"


def test_worker_receives_issue_context_refreshed_after_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    updated = _issue(34, "Updated requirements after selection")
    updated["title"] = "Updated issue title"
    worker_hook = (
        'printf "%s" "$AGENT_LOOP_ISSUE_TITLE" > title.txt; '
        'printf "%s" "$AGENT_LOOP_ISSUE_BODY" > body.txt; '
        "git add title.txt body.txt; git commit -m 'fix: refreshed issue context'"
    )
    result = _run(
        consumer,
        ["--issues", "34"],
        issues=[_issue(34, "Stale requirements from selection")],
        config=_config(tmp_path, worker_hook=worker_hook),
        extra_env={
            "AGENT_POST_CLAIM_ISSUES_JSON": json.dumps({"34": updated}),
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branch = _agent_loop_branches(consumer[1]).strip()
    assert (
        _run_git("show", f"{branch}:body.txt", cwd=consumer[1]).stdout
        == "Updated requirements after selection"
    )
    assert (
        _run_git("show", f"{branch}:title.txt", cwd=consumer[1]).stdout
        == "Updated issue title"
    )


def test_new_dependency_added_during_claim_blocks_worker_and_releases_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    updated = _issue(35, "Depends on PR #999")
    result = _run(
        consumer,
        ["--issues", "35"],
        issues=[_issue(35)],
        config=_config(tmp_path, dependency_gate="merged-to-base"),
        extra_env={
            "AGENT_POST_CLAIM_ISSUES_JSON": json.dumps({"35": updated}),
            "AGENT_PRS_JSON": json.dumps({"999": ["CLOSED", "main", ""]}),
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "is no longer ready after claim verification" in result.stdout
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit 35 --remove-assignee @me" in gh_log
    assert not (consumer[3] / "events.log").exists()


def test_default_ready_gate_is_rechecked_after_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    updated = _issue(45, "Depends on #999")
    result = _run(
        consumer,
        ["--issues", "45"],
        issues=[_issue(45)],
        config=_config(tmp_path),
        extra_env={
            "AGENT_POST_CLAIM_ISSUES_JSON": json.dumps({"45": updated}),
            "AGENT_POST_CLAIM_READY_JSON": "[]",
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "is no longer ready after claim verification" in result.stdout
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit 45 --remove-assignee @me" in gh_log
    assert not (consumer[3] / "events.log").exists()


def test_malformed_ready_data_after_claim_releases_new_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "59"],
        issues=[_issue(59)],
        config=_config(tmp_path),
        extra_env={"AGENT_POST_CLAIM_READY_JSON": "{}"},
    )
    assert result.returncode != 0
    assert "could not re-evaluate issue #59" in result.stderr
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit 59 --remove-assignee @me" in gh_log
    assert not (consumer[3] / "events.log").exists()


@pytest.mark.parametrize(
    "invalid_field",
    [{"title": ""}, {"body": []}],
)
def test_invalid_issue_context_after_claim_releases_new_claim(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    invalid_field: dict[str, object],
) -> None:
    updated = _issue(60)
    updated.update(invalid_field)
    result = _run(
        consumer,
        ["--issues", "60"],
        issues=[_issue(60)],
        config=_config(tmp_path),
        extra_env={"AGENT_POST_CLAIM_ISSUES_JSON": json.dumps({"60": updated})},
    )
    assert result.returncode != 0
    assert "could not refresh issue context after claiming issue #60" in result.stderr
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit 60 --remove-assignee @me" in gh_log
    assert not (consumer[3] / "events.log").exists()


def test_issue_context_change_before_publication_preserves_work_and_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    final_issue = _issue(61, "Changed requirements", assigned=True)
    result = _run(
        consumer,
        ["--issues", "61"],
        issues=[_issue(61, "Original requirements")],
        config=_config(tmp_path),
        extra_env={"AGENT_FINAL_ISSUES_JSON": json.dumps({"61": final_issue})},
    )
    assert result.returncode != 0
    assert "changed or is no longer eligible before publication" in result.stderr
    assert "completed work was preserved and the claim was retained" in result.stderr
    assert (consumer[3] / "claimed-61").exists()
    assert "issue-61" not in _agent_loop_branches(consumer[1])
    match = re.search(r"Worktree preserved: (.+)", result.stderr)
    assert match and Path(match.group(1)).exists()


def test_default_shipped_prompt_does_not_depend_on_gh() -> None:
    # The shipped worker prompt runs inside the gh-masked hook, so it must not
    # instruct the worker to call `gh`. Regression guard for the prompt template.
    prompt = (REPO_ROOT / ".codex/skills/agent-loop/prompt.txt.template").read_text(
        encoding="utf-8"
    )
    assert "gh issue view" not in prompt
    assert "AGENT_LOOP_ISSUE_BODY" in prompt
    assert "local classification record" in prompt
    instructions = (
        REPO_ROOT / ".codex/skills/agent-loop/agent-loop-instructions.md.template"
    ).read_text(encoding="utf-8")
    assert "The worker cannot call `gh`" in instructions
    assert "operator must translate that record" in instructions


@pytest.mark.parametrize(
    ("worker_hook", "expected"),
    [
        ("printf dirty > dirty.txt; exit 7", "after changing or committing work"),
        ("exit 7", "without recoverable retry conditions"),
    ],
)
def test_worker_failure_preserves_worktree(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    worker_hook: str,
    expected: str,
) -> None:
    result = _run(
        consumer,
        ["--issues", "6"],
        issues=[_issue(6)],
        config=_config(tmp_path, worker_hook=worker_hook, worker_retries=0),
    )
    assert result.returncode != 0
    assert expected in result.stderr
    match = re.search(r"Worktree preserved: (.+)", result.stderr)
    assert match
    worktree = Path(match.group(1))
    assert worktree.exists()
    if "dirty" in worker_hook:
        assert (worktree / "dirty.txt").exists()


def test_capacity_failure_uses_fallback_model(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex = consumer[2] / "codex"
    _write_executable(
        codex,
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$AGENT_STATE_DIR/models.log"
if [[ "$*" == *" -m primary "* ]]; then
  echo 'capacity exhausted' >&2
  exit 9
fi
printf 'done\n' > result.txt
git add result.txt
git commit -m 'fix: fallback worker'
""",
    )
    result = _run(
        consumer,
        ["--issues", "7"],
        issues=[_issue(7)],
        config=_config(
            tmp_path,
            worker_hook="",
            worker_model="primary",
            worker_fallback_model="fallback",
            worker_retries=1,
        ),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    models = (consumer[3] / "models.log").read_text(encoding="utf-8")
    assert "-m primary" in models
    assert "-m fallback" in models


def test_timeout_retries_only_an_unchanged_worktree(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    retry_mark = tmp_path / "retry-mark"
    worker = (
        'if [ ! -e "$RETRY_MARK" ]; then touch "$RETRY_MARK"; sleep 5; fi; '
        "printf done > result.txt; git add result.txt; git commit -m 'fix: retry worker'"
    )
    result = _run(
        consumer,
        ["--issues", "8"],
        issues=[_issue(8)],
        config=_config(
            tmp_path,
            worker_hook=worker,
            worker_timeout_seconds=1,
            worker_retries=1,
        ),
        extra_env={"RETRY_MARK": str(retry_mark)},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Retrying worker" in result.stdout


def test_worker_state_inspection_failure_preserves_partial_work_without_retry(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, _, bin_dir, state_dir = consumer
    real_git = shutil.which("git")
    assert real_git is not None
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
if [ "${1:-}" = status ] && [ -e "$AGENT_STATE_DIR/fail-next-status" ] && \
   [ ! -e "$AGENT_STATE_DIR/status-failed" ]; then
    touch "$AGENT_STATE_DIR/status-failed"
    exit 72
fi
exec "$AGENT_TEST_REAL_GIT" "$@"
""",
    )
    worker = (
        'if [ ! -e "$AGENT_STATE_DIR/first-attempt" ]; then '
        'touch "$AGENT_STATE_DIR/first-attempt"; '
        "printf 'partial\\n' > partial.txt; "
        'touch "$AGENT_STATE_DIR/fail-next-status"; exit 124; fi; '
        'touch "$AGENT_STATE_DIR/second-attempt"; rm -f partial.txt; exit 73'
    )
    result = _run(
        consumer,
        ["--issues", "53"],
        issues=[_issue(53)],
        config=_config(
            tmp_path,
            worker_hook=worker,
            worker_retries=1,
            retry_delay_seconds=0,
        ),
        extra_env={"AGENT_TEST_REAL_GIT": real_git},
    )
    assert result.returncode != 0
    assert not (state_dir / "second-attempt").exists()
    match = re.search(r"Worktree preserved: (.+)", result.stderr)
    assert match
    assert (Path(match.group(1)) / "partial.txt").read_text(encoding="utf-8") == (
        "partial\n"
    )


def test_fresh_base_is_integrated_and_validated_before_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    remote = consumer[1]
    clone = tmp_path / "advanced-base"
    _clone_test_repo(remote, clone)
    (clone / "fresh-base.txt").write_text("fresh\n", encoding="utf-8")
    _run_git("add", "fresh-base.txt", cwd=clone)
    _run_git("commit", "-m", "chore: advance base", cwd=clone)
    advanced_base = _run_git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _run_git(
        "push", "origin", f"{advanced_base}:refs/heads/advance-candidate", cwd=clone
    )
    _run_git("push", "origin", ":refs/heads/advance-candidate", cwd=clone)

    updater = tmp_path / "advance-base.sh"
    _write_executable(
        updater,
        """#!/usr/bin/env bash
set -e
git --git-dir="$REMOTE_PATH" update-ref refs/heads/main "$ADVANCED_BASE"
printf 'codex\n' >> "$EVENT_LOG"
""",
    )
    claude = (
        'if git cat-file -e "$AGENT_LOOP_REVIEW_BASE_SHA:fresh-base.txt" '
        ">/dev/null 2>&1; then exit 42; fi; "
        "printf 'claude\\n' >> \"$EVENT_LOG\""
    )
    result = _run(
        consumer,
        ["--issues", "9"],
        issues=[_issue(9)],
        config=_config(
            tmp_path,
            codex_review_hook=str(updater),
            claude_review_hook=claude,
        ),
        extra_env={"REMOTE_PATH": str(remote), "ADVANCED_BASE": advanced_base},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branch = _agent_loop_branches(consumer[1]).strip()
    published = _run_git("show", f"{branch}:fresh-base.txt", cwd=consumer[1]).stdout
    assert published == "fresh\n"
    events = (consumer[3] / "events.log").read_text(encoding="utf-8").splitlines()
    assert events[-1] == "validate"


def test_large_worker_writes_survive_and_log_is_bounded(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Regression: the log-size bound must not constrain files the worker writes. A
    # prior `ulimit -f` capped every file the hook wrote and SIGXFSZ-killed (and
    # truncated) legitimate large writes. The worker below writes a repo file and
    # streams stdout both larger than the cap; the file must land intact and the
    # captured log must still be bounded to roughly log_max_kb.
    cap_kb = 64
    worker = (
        "dd if=/dev/zero of=big.bin bs=1024 count=2048 2>/dev/null; "  # 2 MiB file > cap
        "seq 1 200000; "  # ~1.3 MiB of stdout, far over the log cap
        "git add big.bin; git commit -m 'fix: large artifact'"
    )
    result = _run(
        consumer,
        ["--issues", "11"],
        issues=[_issue(11)],
        config=_config(tmp_path, worker_hook=worker, log_max_kb=cap_kb),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branch = _agent_loop_branches(consumer[1]).strip()
    size = _run_git(
        "cat-file", "-s", f"{branch}:big.bin", cwd=consumer[1]
    ).stdout.strip()
    assert int(size) == 2048 * 1024  # written in full, not truncated at the log cap
    logs = list((tmp_path / "logs").glob("*/worker-attempt-1.log"))
    assert logs, "worker log was not captured"
    assert logs[0].stat().st_size <= cap_kb * 1024 + 8192  # bounded to ~log_max_kb


def test_committed_conflict_markers_block_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Regression: `inspect_publication_diff` runs in an `||` context (set -e off), so
    # the `git diff --check` gate must check its status explicitly. A committed
    # conflict marker in the publication diff must block the PR, not sail through.
    worker = (
        r"printf '<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n' > conflict.txt; "
        "git add conflict.txt; git commit -m 'fix: conflicted'"
    )
    result = _run(
        consumer,
        ["--issues", "12"],
        issues=[_issue(12)],
        config=_config(tmp_path, worker_hook=worker),
    )
    assert result.returncode != 0
    assert "conflict markers or whitespace errors" in result.stderr
    branches = _agent_loop_branches(consumer[1])
    assert "issue-12" not in branches  # never published


def test_issue_branch_has_no_upstream_during_worker(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _run_git("config", "push.default", "upstream", cwd=consumer[0])
    worker = (
        "if git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' "
        ">/dev/null 2>&1; then exit 41; fi; "
        "printf done > result.txt; git add result.txt; "
        "git commit -m 'fix: untracked issue branch'"
    )
    result = _run(
        consumer,
        ["--issues", "13"],
        issues=[_issue(13)],
        config=_config(tmp_path, worker_hook=worker),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branch = _agent_loop_branches(consumer[0]).strip()
    upstream = _run_git(
        "for-each-ref",
        "--format=%(upstream:short)",
        f"refs/heads/{branch}",
        cwd=consumer[0],
    ).stdout.strip()
    assert upstream == f"origin/{branch}"
    assert (
        _run_git("rev-parse", branch, cwd=consumer[0]).stdout.strip()
        == _run_git("rev-parse", branch, cwd=consumer[1]).stdout.strip()
    )


def test_missing_default_codex_fails_before_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    repo, _, bin_dir, state_dir = consumer
    no_codex_bin = tmp_path / "no-codex-bin"
    no_codex_bin.mkdir()
    for command in ("bash", "git", "jq", "python3", "timeout"):
        executable = shutil.which(command)
        assert executable is not None
        (no_codex_bin / command).symlink_to(executable)
    (no_codex_bin / "gh").symlink_to(bin_dir / "gh")

    result = _run(
        consumer,
        ["--issues", "14"],
        issues=[_issue(14)],
        config=_config(tmp_path, worker_hook=""),
        extra_env={"PATH": str(no_codex_bin)},
    )
    assert result.returncode != 0
    assert "required command not found for default worker: codex" in result.stderr
    gh_log = state_dir / "gh.log"
    assert not gh_log.exists() or "issue edit" not in gh_log.read_text(encoding="utf-8")
    assert not (tmp_path / "worktrees").exists()


def test_allowlist_does_not_bypass_ready_eligibility(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "15", "--dry-run"],
        issues=[_issue(15, "Blocked by #99")],
        config=_config(tmp_path),
        extra_env={"AGENT_READY_JSON": "[]"},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Allowlisted issue #15 is not ready" in result.stderr
    assert "Issue #15 (" not in result.stdout
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit" not in gh_log


@pytest.mark.parametrize("assigned", [False, True])
def test_claim_and_resume_revalidate_assignee_identity(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    assigned: bool,
) -> None:
    args = ["--issues", "16"]
    if assigned:
        args.append("--resume")
    result = _run(
        consumer,
        args,
        issues=[_issue(16, assigned=assigned)],
        config=_config(tmp_path),
        extra_env={"AGENT_VERIFIED_ASSIGNEE": "other-user"},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "could not be claimed; skipping" in result.stdout
    assert not list((tmp_path / "worktrees").glob("*"))


def test_persistent_logs_are_owner_only(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "17"],
        issues=[_issue(17)],
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    log_dirs = list((tmp_path / "logs").iterdir())
    assert len(log_dirs) == 1
    assert stat.S_IMODE(log_dirs[0].stat().st_mode) == 0o700
    for log_file in log_dirs[0].iterdir():
        assert stat.S_IMODE(log_file.stat().st_mode) & 0o077 == 0
