"""Integration coverage for the Antigravity agent-loop wrapper's pinned worker settings.

These run the root `.agents/skills/agent-loop` scripts against a stubbed `gh`
and `agy`, with the review profile supplied through ACTIVELOOM_REVIEW_PROFILE.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest



REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = REPO_ROOT / ".agents/skills/agent-loop"
AGENT_LOOP = SKILL / "scripts/agent-loop.sh"
SETTINGS_SCRIPTS = REPO_ROOT / ".agents/skills/review-setup/scripts"
DOCTOR = SKILL / "scripts/config-doctor.py"
WORKER_LAUNCHER = SKILL / "scripts/run-agy-worker.sh"
STATE_HELPER = SKILL / "scripts/agent-loop-state.py"


def _profile(**engines: dict[str, object] | None) -> dict[str, object]:
    """A complete review profile; an engine given as None is removed."""
    defaults = json.loads(
        (SETTINGS_SCRIPTS / "review-profile.defaults.json").read_text(encoding="utf-8")
    )
    settings: dict[str, dict[str, object]] = {
        "claude": {
            "model": "claude-review",
            "effort": "medium",
            "worker": {"model": "claude-worker", "effort": "high"},
        },
        "codex": {
            "model": "codex-review",
            "effort": "high",
            "worker": {"model": "codex-worker", "effort": "high"},
        },
        "gemini": {
            "model": "gemini-review",
            "effort": "high",
            "worker": {
                "model": "worker-primary",
                "effort": "high",
                "fallback": {"model": "worker-fallback", "effort": "medium"},
            },
        },
    }
    for engine, value in engines.items():
        if value is None:
            settings.pop(engine)
        else:
            settings[engine] = value
    return {
        "schema_version": 2,
        "defaults_version": defaults["defaults_version"],
        "confirmed_at": "2026-01-01T00:00:00Z",
        "engines": settings,
        "order": defaults["order"],
    }


def _run_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


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

    script = repo / ".agents/skills/agent-loop/scripts/agent-loop.sh"
    ready = repo / ".agents/skills/issues/scripts/ready.py"
    script.parent.mkdir(parents=True)
    ready.parent.mkdir(parents=True)
    shutil.copy2(AGENT_LOOP, script)
    for guard_name in (
        "hook-git-guard",
        "hook-gh-guard",
        "review-push.sh",
        "config-doctor.py",
        "run-agy-launch.sh",
        "run-agy-worker.sh",
        "run-agy-review.sh",
    ):
        shutil.copy2(AGENT_LOOP.parent / guard_name, script.parent / guard_name)
    shutil.copy2(AGENT_LOOP.parent / "agent-loop-state.py", script.parent / "agent-loop-state.py")
    ledger_source = REPO_ROOT / ".agents/skills/critique/scripts/review-ledger.js"
    ledger_target = repo / ".agents/skills/critique/scripts/review-ledger.js"
    ledger_target.parent.mkdir(parents=True)
    shutil.copy2(ledger_source, ledger_target)
    # Sync ships a sibling `package.json` declaring the bundle as ESM; copy it
    # so the fixture receives what a consumer receives.
    shutil.copy2(
        REPO_ROOT / ".agents/skills/critique/scripts/package.json",
        ledger_target.parent / "package.json",
    )
    # A CommonJS consumer root is the context that breaks an undeclared ESM `.js`.
    (repo / "package.json").write_text(
        '{"name": "fixture-consumer", "private": true, "type": "commonjs"}\n',
        encoding="utf-8",
    )
    settings_target = repo / ".agents/skills/review-setup/scripts"
    settings_target.mkdir(parents=True)
    for name in ("review-settings.py", "review-profile.py", "review-profile.defaults.json"):
        shutil.copy2(SETTINGS_SCRIPTS / name, settings_target / name)
    (tmp_path / "review-profile.json").write_text(json.dumps(_profile()), encoding="utf-8")
    _write_executable(
        ready,
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "state = pathlib.Path(os.environ['AGENT_STATE_DIR'])\n"
        "(state / 'ready-argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        "with (state / 'ready-argv.log').open('a') as handle: handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "key = ('AGENT_POST_PR_READY_JSON' if (state / 'pr-branch').exists() "
        "else ('AGENT_POST_CLAIM_READY_JSON' "
        "if any(state.glob('claimed-*')) else 'AGENT_READY_JSON'))\n"
        "print(os.environ.get(key, os.environ.get('AGENT_READY_JSON', '[]')))\n",
    )
    (repo / "agent-loop-instructions.md").write_text(
        "# Local-only worker instructions\n", encoding="utf-8"
    )
    (repo / ".agents/skills/agent-loop/prompt.txt").write_text(
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
import hashlib, json, os, pathlib, re, signal, subprocess, sys, time
args = sys.argv[1:]
state = pathlib.Path(os.environ['AGENT_STATE_DIR'])
input_payload = json.load(sys.stdin) if '--input' in args else None
with (state / 'gh.log').open('a') as handle:
    handle.write(' '.join(args) + '\n')
issues = json.loads(os.environ.get('AGENT_ISSUES_JSON', '{}'))


def endpoint_pr_number(pattern):
    # A batch opens one PR per issue, so the stub cannot serve a single
    # hardcoded number: the ledger binds every attestation to (repo, PR), and
    # two issues sharing a number make issue 1's evidence look like a
    # conflicting attestation for issue 2. Read the number back out of the
    # endpoint the caller actually asked for.
    for arg in args:
        found = re.search(pattern, arg)
        if found:
            return int(found.group(1))
    return None


def scoped_rows(path, number):
    rows = json.loads(path.read_text()) if path.exists() else []
    # Rows a test seeded by hand carry no scope; treat them as belonging to
    # whichever PR asks, so seeded single-PR fixtures keep working.
    return [row for row in rows if row.get('_pr', number) == number]


def unscoped(rows):
    return [{key: value for key, value in row.items() if key != '_pr'} for row in rows]
if args[:2] == ['auth', 'git-credential']:
    sys.stdin.read()
    print('username=tester')
    print('password=' + os.environ.get('GH_TOKEN', ''))
elif args[:2] == ['api', 'user']:
    # The vendored ledger resolves the actor as JSON; the retired Python asked
    # for it with `--jq`. Answer both so the stub matches real `gh` rather than
    # one caller's argument shape.
    print('tester' if '--jq' in args else json.dumps({'login': 'tester'}))
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
    post_pr_issue = json.loads(os.environ.get('AGENT_POST_PR_ISSUES_JSON', '{}')).get(number)
    if (state / 'pr-branch').exists() and post_pr_issue is not None:
        issue = post_pr_issue
    elif view_count > 2 and final_issue is not None:
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
    if '--json isDraft' in joined:
        print('false' if (state / 'pr-ready').exists() else 'true')
        if os.environ.get('AGENT_FAIL_RECOVERED_FINALIZED_CHECKPOINT') == 'true':
            for state_file in (state.parent / 'logs').glob('*/run-state.json'):
                state_file.unlink()
                state_file.mkdir()
    elif 'state,isDraft,headRefName,headRefOid' in joined:
        branch = (state / 'pr-branch').read_text()
        remote_head = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch],
            check=True, capture_output=True, text=True
        ).stdout.split()[0]
        raced_head = state / 'raced-pr-head'
        head = (
            raced_head.read_text()
            if raced_head.exists()
            else os.environ.get('AGENT_PR_HEAD_OID', remote_head)
        )
        base_branch = os.environ.get('AGENT_PR_BASE_REF_NAME', 'main')
        base_head = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/main'],
            check=True, capture_output=True, text=True
        ).stdout.split()[0]
        base_oid = os.environ.get('AGENT_PR_BASE_OID', base_head)
        draft = os.environ.get(
            'AGENT_PR_IS_DRAFT',
            'false' if (state / 'pr-ready').exists() else 'true',
        )
        print('\t'.join(['OPEN', draft, branch, head, base_branch, base_oid]))
    elif '--json number' in joined:
        print(args[2].rsplit('/', 1)[1] if args[2].startswith('http') else args[2])
    elif 'baseRefOid' in joined:
        # The vendored ledger binds the ledger to the PR's base commit; the
        # retired Python never asked for it, so this branch did not exist and
        # the call fell through to the generic handler, which exits non-zero
        # with no stderr — surfacing as "GitHub operation failed: no diagnostic
        # returned" rather than an unsupported-invocation message.
        base_head = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/main'],
            check=True, capture_output=True, text=True
        ).stdout.split()[0]
        print(os.environ.get('AGENT_PR_BASE_OID', base_head))
    elif 'headRefOid' in joined:
        branch = (state / 'pr-branch').read_text()
        remote_head = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch],
            check=True, capture_output=True, text=True
        ).stdout.split()[0]
        print(os.environ.get('AGENT_PR_HEAD_OID', remote_head))
    else:
        number = args[2]
        source = (
            os.environ.get('AGENT_POST_PR_PRS_JSON', os.environ.get('AGENT_PRS_JSON', '{}'))
            if (state / 'pr-branch').exists()
            else os.environ.get('AGENT_PRS_JSON', '{}')
        )
        rows = json.loads(source)
        row = rows.get(number)
        if row:
            print('\t'.join(str(value) for value in row))
        else:
            sys.exit(1)
elif args[:2] == ['pr', 'create']:
    for transient in ('pr-ready', 'raced-pr-head', 'reviews.json'):
        (state / transient).unlink(missing_ok=True)
    head = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True
    ).stdout.strip()
    (state / 'pr-head').write_text(head)
    branch = subprocess.run(
        ['git', 'branch', '--show-current'], check=True, capture_output=True, text=True
    ).stdout.strip()
    (state / 'pr-branch').write_text(branch)
    counter = state / 'pr-counter'
    number = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(number))
    print('https://example.invalid/pr/' + str(number))
elif args[:2] == ['pr', 'edit']:
    (state / 'pr-edited').touch()
elif args[:2] == ['pr', 'ready']:
    if '--undo' in args:
        if os.environ.get('AGENT_FAIL_READY_UNDO') == 'true':
            sys.exit(75)
        (state / 'pr-ready').unlink(missing_ok=True)
    else:
        (state / 'pr-ready').touch()
        if os.environ.get('AGENT_FAIL_FINALIZED_CHECKPOINT') == 'true':
            for state_file in (state.parent / 'logs').glob('*/run-state.json'):
                state_file.unlink()
                state_file.mkdir()
        if os.environ.get('AGENT_RACE_THREAD_ON_READY') == 'true':
            (state / 'review-threads.json').write_text(json.dumps([{
                'isResolved': False,
                'comments': {
                    'nodes': [{
                        'body': '<!-- local-review:v1 engine=gemini round=9 '
                                'head=' + 'a' * 40 + ' fingerprint=ready-race -->',
                        'databaseId': 99,
                        'author': {'login': 'tester'},
                    }],
                    'pageInfo': {'hasNextPage': False},
                },
            }]))
        if os.environ.get('AGENT_DROP_THREADS_ON_READY') == 'true':
            (state / 'review-threads.json').write_text('[]')
        if os.environ.get('AGENT_RACE_CLEAN_FIX_ON_READY') == 'true':
            head = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True
            ).stdout.strip()
            finding_content = 'Late same-round blocker.'
            disposition_content = 'Claimed fixed without a new commit.'
            finding = (
                '<!-- local-review:v3 engine=gemini round=1 head=' + head +
                ' fingerprint=late-clean-fix occurrence=1 severity=blocking '
                'lens=correctness content-sha256=' +
                hashlib.sha256(finding_content.encode()).hexdigest() + ' -->\n' +
                finding_content
            )
            disposition = (
                '<!-- local-review-disposition:v3 engine=gemini round=1 head=' + head +
                ' fingerprint=late-clean-fix occurrence=1 outcome=fixed '
                'content-sha256=' +
                hashlib.sha256(disposition_content.encode()).hexdigest() + ' -->\n' +
                disposition_content
            )
            (state / 'review-threads.json').write_text(json.dumps([{
                'isResolved': True,
                'comments': {
                    'nodes': [
                        {'body': finding, 'databaseId': 101, 'author': {'login': 'tester'}},
                        {'body': disposition, 'databaseId': 102, 'author': {'login': 'tester'}},
                    ],
                    'pageInfo': {'hasNextPage': False},
                },
            }]))
        raced_head = os.environ.get('AGENT_RACE_HEAD_ON_READY')
        if raced_head:
            (state / 'raced-pr-head').write_text(raced_head)
        if os.environ.get('AGENT_FAIL_READY_AFTER_MUTATION') == 'true':
            sys.exit(76)
        if os.environ.get('AGENT_INTERRUPT_AFTER_READY') == 'true':
            os.kill(os.getppid(), signal.SIGTERM)
            time.sleep(0.1)
elif args[:2] == ['pr', 'review']:
    body = args[args.index('--body') + 1]
    reviews_file = state / 'reviews.json'
    reviews = json.loads(reviews_file.read_text()) if reviews_file.exists() else []
    reviews.append({
        '_pr': int(args[2]) if args[2].isdigit() else 1,
        'body': body,
        'user': {'login': os.environ.get('AGENT_REVIEW_AUTHOR', 'tester')},
        'commit_id': subprocess.run(
            ['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True
        ).stdout.strip(),
    })
    reviews_file.write_text(json.dumps(reviews))
elif args[:2] == ['pr', 'close']:
    (state / 'pr-closed').touch()
elif args[:1] == ['api'] and any('/compare/' in arg for arg in args):
    endpoint = next(arg for arg in args if '/compare/' in arg)
    before = endpoint.rsplit('/compare/', 1)[1].split('...', 1)[0]
    print(json.dumps({'status': 'ahead', 'merge_base_commit': {'sha': before}}))
elif args[:2] == ['api', 'graphql']:
    if os.environ.get('AGENT_MUTATE_RESULT_ON_THREADS_FETCH') == 'true':
        marker = state / 'result-mutated'
        if not marker.exists():
            result_files = list((state.parent / 'logs').glob('*/*.result.json'))
            for result_file in result_files:
                result_file.write_text(result_file.read_text() + '\n')
            if result_files:
                marker.touch()
    threads_file = state / 'review-threads.json'
    nodes = json.loads(
        threads_file.read_text()
        if threads_file.exists()
        else os.environ.get('AGENT_REVIEW_THREADS_JSON', '[]')
    )
    pages = json.loads(os.environ.get('AGENT_REVIEW_THREAD_PAGES_JSON', 'null'))
    if pages is None:
        pages = [nodes]
    query = args[args.index('-f') + 1] if '-f' in args else ''
    if '--paginate' not in args or '$endCursor' not in query or 'after:$endCursor' not in query:
        pages = pages[:1]
    output = []
    for index, page_nodes in enumerate(pages):
        selected_nodes = []
        for node_index, node in enumerate(page_nodes):
            selected_node = dict(node)
            if '\n          id\n' in query:
                selected_node.setdefault('id', f'THREAD-{index}-{node_index}')
            else:
                selected_node.pop('id', None)
            # The vendored ledger refuses a thread it cannot prove belongs to
            # the PR it was asked about, so echo the scope fields back when the
            # query selects them — real `gh` would. Only default them in: a
            # test that deliberately omits them from a node is asserting the
            # refusal and must keep its omission.
            if 'repository' in query and 'nameWithOwner' in query:
                selected_node.setdefault('repository', {'nameWithOwner': 'fixture/consumer'})
            if 'pullRequest' in query and 'number' in query:
                selected_node.setdefault(
                    'pullRequest', {'number': int(os.environ.get('AGENT_LOOP_PR_NUMBER', '1'))}
                )
            selected_nodes.append(selected_node)
        has_next = index + 1 < len(pages)
        output.append({'data': {'repository': {'pullRequest': {
            'reviewThreads': {'nodes': selected_nodes, 'pageInfo': {
                'hasNextPage': has_next,
                'endCursor': f'cursor-{index + 1}' if has_next else None,
            }}
        }}}})
    print(json.dumps(output))
elif args[:1] == ['api'] and endpoint_pr_number(r'/issues/(\d+)/comments\?per_page=100') is not None:
    comments_file = state / 'issue-comments.json'
    number = endpoint_pr_number(r'/issues/(\d+)/comments\?per_page=100')
    print(json.dumps([unscoped(scoped_rows(comments_file, number))]))
elif args[:1] == ['api'] and endpoint_pr_number(r'/issues/(\d+)/comments') is not None and '-X' in args:
    comments_file = state / 'issue-comments.json'
    number = endpoint_pr_number(r'/issues/(\d+)/comments')
    comments = json.loads(comments_file.read_text()) if comments_file.exists() else []
    row = {
        'id': len(comments) + 700,
        'body': input_payload['body'],
        'user': {'login': 'tester'},
    }
    comments.append(dict(row, _pr=number))
    comments_file.write_text(json.dumps(comments))
    print(json.dumps(row))
elif args[:1] == ['api'] and any('/issues/comments/' in arg for arg in args):
    comment_id = int(next(arg.rsplit('/', 1)[1] for arg in args if '/issues/comments/' in arg))
    comments = json.loads((state / 'issue-comments.json').read_text())
    row = next(row for row in comments if row['id'] == comment_id)
    print(json.dumps(unscoped([row])[0]))
elif args[:1] == ['api'] and endpoint_pr_number(r'/issues/(\d+)/comments') is not None:
    comments_file = state / 'issue-comments.json'
    number = endpoint_pr_number(r'/issues/(\d+)/comments')
    print(json.dumps(unscoped(scoped_rows(comments_file, number))))
elif args[:1] == ['api'] and endpoint_pr_number(r'/pulls/(\d+)/comments') is not None:
    print('[]')
elif args[:1] == ['api'] and endpoint_pr_number(r'/pulls/(\d+)/reviews') is not None:
    reviews_file = state / 'reviews.json'
    number = endpoint_pr_number(r'/pulls/(\d+)/reviews')
    reviews = unscoped(scoped_rows(reviews_file, number))
    print(json.dumps([reviews] if '--slurp' in args else reviews))
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


def _config(
    tmp_path: Path,
    *,
    auto_clean_attestation: bool = True,
    auto_committed_evidence: bool = True,
    **overrides: str | int,
) -> str:
    values: dict[str, str | int] = {
        "base_branch": "main",
        "setup_hook": "printf 'setup\\n' >> \"$EVENT_LOG\"",
        "validation_hook": "printf 'validate\\n' >> \"$EVENT_LOG\"",
        "claude_review_hook": "printf 'claude\\n' >> \"$EVENT_LOG\"",
        "gemini_review_hook": "printf 'gemini\\n' >> \"$EVENT_LOG\"",
        "worker_hook": "printf 'worker\\n' >> \"$EVENT_LOG\"; printf 'done\\n' > result.py; git add result.py; git commit -m 'fix: worker'",
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
    for key in ("gemini_review_hook", "claude_review_hook"):
        engine = key.removesuffix("_review_hook")
        command = str(values[key])
        clean_attestation = (
            'gh pr review "$AGENT_LOOP_PR_NUMBER" --comment --body '
            '"<!-- local-review-pass:v1 engine=$AGENT_LOOP_REVIEW_ENGINE '
            'round=$AGENT_LOOP_REVIEW_ROUND head=$AGENT_LOOP_PR_HEAD_SHA -->'
            '\\nno new material findings"'
            if auto_clean_attestation
            else "true"
        )
        committed_evidence = (
            "after=$(git rev-parse HEAD); "
            "jq -n --arg engine \"$AGENT_LOOP_REVIEW_ENGINE\" "
            "--arg round \"$AGENT_LOOP_REVIEW_ROUND\" "
            "--arg before \"$AGENT_LOOP_PR_HEAD_SHA\" --arg after \"$after\" "
            '\'[{isResolved:true,comments:{nodes:['
            '{body:("<!-- local-review:v1 engine="+$engine+" round="+$round+" '
            'head="+$before+" fingerprint=fixture-"+$engine+" -->\\nFinding"),'
            'databaseId:1,author:{login:"tester"}},'
            '{body:("<!-- local-review-disposition:v1 engine="+$engine+" '
            'round="+$round+" head="+$after+" fingerprint=fixture-"+$engine+" '
            'outcome=fixed -->\\nFixed and validated."),databaseId:2,'
            'author:{login:"tester"}}],pageInfo:{hasNextPage:false}}}]\' '
            '> "$AGENT_STATE_DIR/review-threads.json"; '
            'gh pr review "$AGENT_LOOP_PR_NUMBER" --comment --body '
            f'"<!-- local-review-complete:v1 engine={engine} '
            'round=$AGENT_LOOP_REVIEW_ROUND before=$AGENT_LOOP_PR_HEAD_SHA '
            'head=$after -->"'
            if auto_committed_evidence
            else "true"
        )
        values[key] = (
            f"{command}; hook_status=$?; "
            '[ "$hook_status" -eq 0 ] || exit "$hook_status"; '
            'if [ "$(git rev-parse HEAD)" != "$AGENT_LOOP_PR_HEAD_SHA" ]; then '
            '"$AGENT_LOOP_REVIEW_PUSH_HELPER"; '
            f"{committed_evidence}; "
            f"else {clean_attestation}; fi"
        )
    return "\n".join(f"{key} = {value}" for key, value in values.items()) + "\n"


def _config_v3(tmp_path: Path, **overrides: str | int) -> str:
    result_command = (
        "jq -n --arg engine \"$AGENT_LOOP_REVIEW_ENGINE\" "
        "--argjson round \"$AGENT_LOOP_REVIEW_ROUND\" "
        "--arg base \"$AGENT_LOOP_REVIEW_BASE_SHA\" "
        "--arg head \"$AGENT_LOOP_PR_HEAD_SHA\" "
        "'{version:3,status:\"clean\",engine:$engine,round:$round,"
        "baseSha:$base,beforeSha:$head,afterSha:$head,classification:null,"
        "findingFingerprints:[],finalLaneComplete:true}' "
        '> "$AGENT_LOOP_REVIEW_RESULT_FILE"'
    )
    values: dict[str, str | int] = {
        "review_contract_version": 3,
        "gemini_review_hook": result_command,
        "claude_review_hook": result_command,
    }
    values.update(overrides)
    for key in ("gemini_review_hook", "claude_review_hook"):
        values[key] = (
            ': "$AGENT_LOOP_REVIEW_PUSH_HELPER" '
            f'"$AGENT_LOOP_REVIEW_RESULT_FILE" write-result; {values[key]}'
        )
    return _config(
        tmp_path,
        auto_clean_attestation=False,
        auto_committed_evidence=False,
        **values,
    )




def _agent_loop_env(
    fixture: tuple[Path, Path, Path, Path],
    *,
    issues: list[dict[str, object]],
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    _, _, bin_dir, state_dir = fixture
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
            "ACTIVELOOM_REVIEW_PROFILE": str(bin_dir.parent / "review-profile.json"),
        }
    )
    if extra_env:
        env.update(extra_env)
    return env


def _run(
    fixture: tuple[Path, Path, Path, Path],
    args: list[str],
    *,
    issues: list[dict[str, object]],
    config: str,
    extra_env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    repo, _, _, state_dir = fixture
    for state_file in [
        *state_dir.glob("claimed-*"),
        *state_dir.glob("issue-views-*"),
    ]:
        state_file.unlink()
    (repo / ".agents/skills/agent-loop/agent-loop.config").write_text(
        config, encoding="utf-8"
    )
    env = _agent_loop_env(fixture, issues=issues, extra_env=extra_env)
    return subprocess.run(
        [str(repo / ".agents/skills/agent-loop/scripts/agent-loop.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )




def _env_capture(name: str) -> str:
    return (
        "env | grep -E '^AGENT_LOOP_(CLAUDE|CODEX|GEMINI|NONINTERACTIVE)' | sort "
        f'> "$AGENT_STATE_DIR/{name}.log"'
    )


def _agy_worker_stub(bin_dir: Path, *, capacity_model: str | None = None) -> Path:
    """An `agy` that records its argv and commits, or reports capacity for one model."""
    capacity = (
        f'if [[ "$*" == *"--model {capacity_model} "* ]]; then\n'
        "  printf '{\"status\":\"ERROR\",\"error\":\"model at capacity\"}\\n'\n"
        "  exit 0\n"
        "fi\n"
        if capacity_model
        else ""
    )
    agy = bin_dir / "agy"
    _write_executable(
        agy,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$AGENT_STATE_DIR/agy-args.log\"\n"
        + capacity
        + "printf 'done\\n' > result.py\n"
        "git add result.py\n"
        "git commit -m 'fix: agy worker' >/dev/null\n"
        "printf '{\"status\":\"SUCCESS\",\"response\":\"done\"}\\n'\n",
    )
    return agy


def _run_state(tmp_path: Path) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(next((tmp_path / "logs").glob("*/run-state.json")).read_text())
    return state


def _assert_no_issue_mutation(consumer: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    gh_log = consumer[3] / "gh.log"
    gh_calls = gh_log.read_text(encoding="utf-8") if gh_log.exists() else ""
    assert "issue edit" not in gh_calls
    assert "issue view" not in gh_calls
    assert "pr create" not in gh_calls
    assert not list(consumer[3].glob("claimed-*"))
    assert not (tmp_path / "worktrees").exists()
    logs = tmp_path / "logs"
    assert not logs.exists() or not any(logs.iterdir())


def test_settings_are_pinned_once_and_exported_to_every_hook(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "40"],
        issues=[_issue(40)],
        config=_config_v3(
            tmp_path,
            gemini_review_hook=_env_capture("gemini-env")
            + "; jq -n --arg engine \"$AGENT_LOOP_REVIEW_ENGINE\" "
            "--argjson round \"$AGENT_LOOP_REVIEW_ROUND\" "
            "--arg base \"$AGENT_LOOP_REVIEW_BASE_SHA\" "
            "--arg head \"$AGENT_LOOP_PR_HEAD_SHA\" "
            "'{version:3,status:\"clean\",engine:$engine,round:$round,"
            "baseSha:$base,beforeSha:$head,afterSha:$head,classification:null,"
            "findingFingerprints:[],finalLaneComplete:true}' "
            '> "$AGENT_LOOP_REVIEW_RESULT_FILE"',
            worker_hook=_env_capture("worker-env")
            + "; printf 'done\\n' > result.py; git add result.py; git commit -m 'fix: worker'",
        ),
        extra_env={
            "AGENT_LOOP_GEMINI_MODEL": "stale",
            "AGENT_LOOP_CODEX_MODEL": "stale",
            "AGENT_LOOP_GEMINI_WORKER_EFFORT": "stale",
            "AGENT_LOOP_NONINTERACTIVE": "0",
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    expected = {
        "AGENT_LOOP_GEMINI_WORKER_EFFORT=high",
        "AGENT_LOOP_GEMINI_WORKER_MODEL=worker-primary",
        "AGENT_LOOP_GEMINI_WORKER_SOURCE=user profile",
        "AGENT_LOOP_NONINTERACTIVE=1",
    }
    for name in ("worker-env.log", "gemini-env.log"):
        assert set((consumer[3] / name).read_text(encoding="utf-8").splitlines()) == expected
    assert (result.stdout + result.stderr).count("Pinned gemini worker settings") == 1
    settings = _run_state(tmp_path)["reviewSettings"]
    assert isinstance(settings, dict)
    assert settings["repo"] == "fixture/consumer"
    assert settings["worker_settings"]["gemini"]["model"] == "worker-primary"
    assert "review_settings" not in settings or not settings["review_settings"]


def test_default_worker_takes_model_and_effort_from_the_profile_not_config(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    agy = _agy_worker_stub(consumer[2])
    result = _run(
        consumer,
        ["--issues", "15"],
        issues=[_issue(15)],
        config=_config_v3(
            tmp_path,
            worker_hook="",
            worker_model="config-model",
            worker_fallback_model="config-fallback",
            worker_effort="low",
        ),
        extra_env={"AGY_CLI": str(agy)},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    args = (consumer[3] / "agy-args.log").read_text(encoding="utf-8")
    assert "--model worker-primary --effort high " in args
    assert "config-model" not in args
    assert "worker_model, worker_fallback_model, and worker_effort are ignored" in result.stderr


def test_worker_capacity_failure_switches_to_the_pinned_fallback_once(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    agy = _agy_worker_stub(consumer[2], capacity_model="worker-primary")
    result = _run(
        consumer,
        ["--issues", "7"],
        issues=[_issue(7)],
        config=_config_v3(tmp_path, worker_hook="", worker_retries=1),
        extra_env={"AGY_CLI": str(agy)},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    calls = (consumer[3] / "agy-args.log").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert "--model worker-primary --effort high " in calls[0]
    assert "--model worker-fallback --effort medium " in calls[1]
    assert "Capacity fallback: gemini worker model worker-fallback, effort medium" in (
        result.stdout + result.stderr
    )
    settings = _run_state(tmp_path)["reviewSettings"]
    assert isinstance(settings, dict)
    assert settings["worker_fallback_engines"] == ["gemini"]


def test_worker_capacity_failure_without_a_fallback_retries_on_the_settings_in_use(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    gemini = dict(_profile()["engines"]["gemini"])  # type: ignore[index]
    gemini["worker"] = {"model": "worker-primary", "effort": "high"}
    (tmp_path / "review-profile.json").write_text(
        json.dumps(_profile(gemini=gemini)), encoding="utf-8"
    )
    agy = _agy_worker_stub(consumer[2], capacity_model="worker-primary")
    result = _run(
        consumer,
        ["--issues", "9"],
        issues=[_issue(9)],
        config=_config_v3(tmp_path, worker_hook="", worker_retries=1),
        extra_env={"AGY_CLI": str(agy)},
        timeout=60,
    )
    assert result.returncode != 0
    calls = (consumer[3] / "agy-args.log").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert all("--model worker-primary --effort high " in call for call in calls)
    assert "retrying on the settings in use" in result.stdout


def _without_worker_keys() -> dict[str, object]:
    gemini = dict(_profile()["engines"]["gemini"])  # type: ignore[index]
    gemini.pop("worker")
    return _profile(gemini=gemini)


@pytest.mark.parametrize(
    ("profile", "missing"),
    [
        (None, "gemini.worker.model, gemini.worker.effort"),
        (_without_worker_keys(), "gemini.worker.model, gemini.worker.effort"),
        ("gemini-unavailable", None),
    ],
    ids=["no-profile", "missing-key", "unavailable-engine"],
)
def test_unresolvable_settings_fail_closed_before_any_issue_mutation(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    profile: object,
    missing: str | None,
) -> None:
    profile_path = tmp_path / "review-profile.json"
    if profile is None:
        profile_path.unlink()
    elif profile == "gemini-unavailable":
        gemini = dict(_profile()["engines"]["gemini"])  # type: ignore[index]
        gemini["availability"] = "unavailable"
        profile_path.write_text(json.dumps(_profile(gemini=gemini)), encoding="utf-8")
    else:
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
    result = _run(
        consumer,
        ["--issues", "41,42"],
        issues=[_issue(41), _issue(42)],
        config=_config_v3(tmp_path),
    )
    assert result.returncode == 4, result.stderr + result.stdout
    action = [line for line in result.stderr.splitlines() if "review-setup" in line]
    assert len(action) == 1, result.stderr
    assert action[0].startswith("agent-loop: ")
    assert "rerun agent-loop" in action[0]
    if missing is not None:
        assert missing in action[0]
    else:
        assert "unavailable" in result.stderr
    _assert_no_issue_mutation(consumer, tmp_path)


def test_config_doctor_runs_after_pinning_and_before_any_issue_mutation(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    doctor = consumer[0] / ".agents/skills/agent-loop/scripts/config-doctor.py"
    _write_executable(
        doctor,
        "import os, pathlib, sys\n"
        "state = pathlib.Path(os.environ['AGENT_STATE_DIR'])\n"
        "(state / 'doctor-env.log').write_text(\n"
        "    os.environ.get('AGENT_LOOP_GEMINI_WORKER_MODEL', '') + ' '\n"
        "    + os.environ.get('AGENT_LOOP_GEMINI_WORKER_EFFORT', ''))\n"
        "print('agent-loop config doctor: refused', file=sys.stderr)\n"
        "sys.exit(1)\n",
    )
    result = _run(
        consumer,
        ["--issues", "41"],
        issues=[_issue(41)],
        config=_config_v3(tmp_path, config_doctor="true", claude_effort_policy="low"),
    )
    assert result.returncode == 1, result.stderr + result.stdout
    assert "agent-loop config doctor: refused" in result.stderr
    assert "claude_effort_policy is retired and ignored" in result.stderr
    assert (consumer[3] / "doctor-env.log").read_text(encoding="utf-8") == "worker-primary high"
    _assert_no_issue_mutation(consumer, tmp_path)


def _failing_review_config(tmp_path: Path) -> str:
    return _config_v3(
        tmp_path,
        claude_review_hook=(
            'if [ -e "$AGENT_STATE_DIR/fail-claude-review" ]; then exit 71; fi; '
            + _env_capture("claude-env")
            + "; jq -n --arg engine \"$AGENT_LOOP_REVIEW_ENGINE\" "
            "--argjson round \"$AGENT_LOOP_REVIEW_ROUND\" "
            "--arg base \"$AGENT_LOOP_REVIEW_BASE_SHA\" "
            "--arg head \"$AGENT_LOOP_PR_HEAD_SHA\" "
            "'{version:3,status:\"clean\",engine:$engine,round:$round,"
            "baseSha:$base,beforeSha:$head,afterSha:$head,classification:null,"
            "findingFingerprints:[],finalLaneComplete:true}' "
            '> "$AGENT_LOOP_REVIEW_RESULT_FILE"'
        ),
        review_max_rounds=1,
    )


def test_resume_keeps_the_pinned_settings_after_the_profile_changes(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    fail_marker = consumer[3] / "fail-claude-review"
    fail_marker.touch()
    config = _failing_review_config(tmp_path)
    first = _run(consumer, ["--issues", "43"], issues=[_issue(43)], config=config, timeout=60)
    assert first.returncode != 0
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    fail_marker.unlink()
    # A profile edit, or its removal, applies to the next run, not this one.
    (tmp_path / "review-profile.json").unlink()

    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(43, assigned=True)],
        config=config,
        timeout=60,
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    claude_env = (consumer[3] / "claude-env.log").read_text(encoding="utf-8")
    assert "AGENT_LOOP_GEMINI_WORKER_MODEL=worker-primary" in claude_env
    assert "AGENT_LOOP_NONINTERACTIVE=1" in claude_env
    assert "Pinned " not in resumed.stdout + resumed.stderr
    assert "Review settings restored from run state: worker worker-primary/high" in resumed.stdout
    assert json.loads(state_file.read_text())["phase"] == "finalized"


def test_resume_without_recorded_settings_fails_closed_without_a_profile(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    fail_marker = consumer[3] / "fail-claude-review"
    fail_marker.touch()
    config = _failing_review_config(tmp_path)
    first = _run(consumer, ["--issues", "47"], issues=[_issue(47)], config=config, timeout=60)
    assert first.returncode != 0
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    # A checkpoint written before settings were recorded falls back to the profile.
    state = json.loads(state_file.read_text())
    state.pop("reviewSettings")
    state_file.write_text(json.dumps(state))
    (state_file.parent / "review-settings.json").unlink()
    (tmp_path / "review-profile.json").unlink()
    fail_marker.unlink()
    calls_before = (consumer[3] / "gh.log").read_text(encoding="utf-8")

    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(47, assigned=True)],
        config=config,
        timeout=60,
    )
    assert resumed.returncode == 4, resumed.stderr + resumed.stdout
    assert "gemini.worker.model" in resumed.stderr
    calls = (consumer[3] / "gh.log").read_text(encoding="utf-8")[len(calls_before) :]
    assert "issue view" not in calls
    assert "pr " not in calls
    assert json.loads(state_file.read_text())["phase"] == state["phase"]


# The Agy worker launcher.


def test_worker_launcher_passes_the_given_effort_to_agy(tmp_path: Path) -> None:
    agy = tmp_path / "agy"
    _write_executable(
        agy,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$ARGS_LOG\"\n"
        "printf '{\"status\":\"SUCCESS\",\"response\":\"done\"}\\n'\n",
    )
    env = {
        **os.environ,
        "AGY_CLI": str(agy),
        "ARGS_LOG": str(tmp_path / "args.log"),
        "AGENT_LOOP_PROMPT": "prompt",
    }
    result = subprocess.run(
        [str(WORKER_LAUNCHER), "--model", "worker-primary", "--effort", "medium"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    args = (tmp_path / "args.log").read_text(encoding="utf-8").splitlines()
    assert args[args.index("--model") + 1] == "worker-primary"
    assert args[args.index("--effort") + 1] == "medium"
    assert "high" not in args


@pytest.mark.parametrize(
    "argv",
    [["--model", "worker-primary"], ["--effort", "high"], ["--model", "m", "--effort", ""]],
    ids=["no-effort", "no-model", "empty-effort"],
)
def test_worker_launcher_requires_a_model_and_an_effort(tmp_path: Path, argv: list[str]) -> None:
    result = subprocess.run(
        [str(WORKER_LAUNCHER), *argv],
        env={**os.environ, "AGY_CLI": str(tmp_path / "missing"), "AGENT_LOOP_PROMPT": "p"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


# The config doctor.


def _doctor_project(tmp_path: Path, extra_config: str = "") -> Path:
    project = tmp_path / "doctor-consumer"
    scripts = project / ".agents/skills/agent-loop/scripts"
    ledger_dir = project / ".agents/skills/critique/scripts"
    scripts.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)
    for name in (
        "agent-loop-state.py",
        "review-push.sh",
        "run-agy-launch.sh",
        "run-agy-worker.sh",
        "run-agy-review.sh",
    ):
        shutil.copy2(SKILL / "scripts" / name, scripts)
    for name in ("review-ledger.js", "package.json"):
        shutil.copy2(REPO_ROOT / ".agents/skills/critique/scripts" / name, ledger_dir)
    (project / "package.json").write_text(
        '{"name": "fixture-consumer", "private": true, "type": "commonjs"}\n',
        encoding="utf-8",
    )
    (project / ".agents/skills/agent-loop/prompt.txt").write_text(
        "Read AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY. "
        "Create a local commit only; do not push.\n",
        encoding="utf-8",
    )
    (project / "agent-loop-instructions.md").write_text(
        "The worker reads AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY.\n",
        encoding="utf-8",
    )
    (project / ".agents/skills/agent-loop/agent-loop.config").write_text(
        "# consumer settings\n"
        "review_contract_version = 3\n"
        'gemini_review_hook = "$AGENT_LOOP_AGY_REVIEW_LAUNCHER" --engine gemini\n'
        'claude_review_hook = "$AGENT_LOOP_AGY_REVIEW_LAUNCHER" --engine claude\n'
        "worker_hook =\n" + extra_config,
        encoding="utf-8",
    )
    return project


def _doctor(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DOCTOR), "--project-dir", str(project), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_doctor_accepts_a_config_without_worker_model_keys(tmp_path: Path) -> None:
    result = _doctor(_doctor_project(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "compatible" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    "key", ["claude_effort_policy", "worker_model", "worker_fallback_model", "worker_effort"]
)
def test_doctor_refuses_a_non_empty_retired_key_and_names_it(
    tmp_path: Path, key: str
) -> None:
    result = _doctor(_doctor_project(tmp_path, f"{key} = gemini-3.7-flash-high\n"))
    assert result.returncode == 1
    assert f"{key} is retired" in result.stderr
    assert "Remove it from the config" in result.stderr


def test_doctor_warns_about_an_empty_retired_key(tmp_path: Path) -> None:
    result = _doctor(_doctor_project(tmp_path, "worker_model =\n"))
    assert result.returncode == 0, result.stderr
    assert "warning: worker_model is retired and has no effect" in result.stderr
    assert "remove it from the config" in result.stderr


def test_the_config_template_passes_the_doctor(tmp_path: Path) -> None:
    project = _doctor_project(tmp_path)
    shutil.copy2(
        SKILL / "agent-loop.config.template", project / ".agents/skills/agent-loop/agent-loop.config"
    )
    result = _doctor(project)
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr


# The run-state helper.


def _pin_file(path: Path, worker: dict[str, object], switched: list[str] | None = None) -> Path:
    value: dict[str, object] = {
        "version": 1,
        "repo": "fixture/consumer",
        "worker_settings": {"gemini": worker},
    }
    if switched is not None:
        value["worker_fallback_engines"] = switched
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _state(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(STATE_HELPER), *args], capture_output=True, text=True, check=False
    )


def test_run_state_records_restores_and_only_extends_worker_pins(tmp_path: Path) -> None:
    worker: dict[str, object] = {
        "model": "worker-primary",
        "effort": "high",
        "fallback": {"model": "worker-fallback", "effort": "medium"},
    }
    pins = _pin_file(tmp_path / "pins.json", worker)
    state = tmp_path / "run-state.json"
    created = _state(
        "create", "--file", str(state), "--run-id", "run", "--repo", "fixture/consumer",
        "--issue", "1", "--issue-title-sha256", "a" * 64, "--issue-body-sha256", "b" * 64,
        "--base-branch", "main", "--branch", "agent-loop/issue-1", "--worktree", str(tmp_path),
        "--log-dir", str(tmp_path), "--pr", "1", "--pr-url", "https://example.invalid/pr/1",
        "--base-sha", "1" * 40, "--head-sha", "2" * 40, "--review-settings-file", str(pins),
    )
    assert created.returncode == 0, created.stderr
    assert json.loads(state.read_text())["reviewSettings"]["worker_settings"]["gemini"] == worker

    # A fallback switch extends the pins and is recorded.
    _pin_file(pins, worker, ["gemini"])
    assert _state("settings-save", "--file", str(state), "--pin-file", str(pins)).returncode == 0
    # A pin file that changes a pinned pair is refused.
    _pin_file(pins, {**worker, "model": "other"})
    refused = _state("settings-save", "--file", str(state), "--pin-file", str(pins))
    assert refused.returncode != 0
    assert "changes settings this run already pinned" in refused.stderr

    # Restore rewrites a pin file that disagrees with the recorded pins.
    restored = _state("settings-restore", "--file", str(state), "--pin-file", str(pins))
    assert restored.returncode == 0, restored.stderr
    assert json.loads(pins.read_text())["worker_fallback_engines"] == ["gemini"]
    assert json.loads(pins.read_text())["worker_settings"]["gemini"] == worker


# The built-in Agy review launcher's reviewer settings.


REVIEW_LAUNCHER = SKILL / "scripts/run-agy-review.sh"
REVIEW_SURFACE_FILES = (
    "REVIEW_WORKFLOW.md",
    "references/local-review-ledger.md",
    "references/roles/code-reviewer.md",
    "references/roles/silent-failure-hunter.md",
    "references/roles/type-design-analyzer.md",
    "references/roles/comment-analyzer.md",
    "references/roles/pr-test-analyzer.md",
    "references/roles/security-reviewer.md",
    "skills/deepcritique/SKILL.md",
    "skills/critique/SKILL.md",
    "skills/critique/scripts/review-ledger.js",
    "skills/refactorpass/SKILL.md",
)


def _review_launcher_fixture(tmp_path: Path, profile: dict[str, Any]) -> dict[str, Any]:
    """A committed trusted surface, the packaged helper, a stub agy, and a profile."""
    source = tmp_path / "source"
    surface = source / ".agents"
    scripts = surface / "skills/agent-loop/scripts"
    scripts.mkdir(parents=True)
    for name in ("run-agy-review.sh", "run-agy-launch.sh"):
        shutil.copy2(SKILL / "scripts" / name, scripts / name)
    (scripts / "run-agy-review.sh").chmod(0o755)
    profile_scripts = surface / "skills/review-setup/scripts"
    profile_scripts.mkdir(parents=True)
    for name in ("review-profile.py", "review-profile.defaults.json"):
        shutil.copy2(SETTINGS_SCRIPTS / name, profile_scripts / name)
    for relative in REVIEW_SURFACE_FILES:
        target = surface / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("surface\n", encoding="utf-8")

    _run_git("init", "-q", "-b", "main", str(source))
    _run_git("config", "user.email", "test@example.com", cwd=source)
    _run_git("config", "user.name", "Test", cwd=source)
    _run_git("config", "commit.gpgsign", "false", cwd=source)
    _run_git("add", "-A", cwd=source)
    _run_git("commit", "-qm", "surface", cwd=source)
    base_ref = _run_git("rev-parse", "HEAD", cwd=source).stdout.strip()

    profile_path = tmp_path / "review-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    argv_log = tmp_path / "agy-argv.log"
    agy = tmp_path / "agy"
    _write_executable(
        agy,
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" > "$AGY_ARGV_LOG"\n'
        "printf '{\"status\":\"SUCCESS\",\"response\":\"reviewed\"}\\n'\n",
    )
    workdir = tmp_path / "issue-worktree"
    workdir.mkdir()
    return {
        "launcher": scripts / "run-agy-review.sh",
        "surface": surface,
        "workdir": workdir,
        "argv_log": argv_log,
        "env": {
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("AGENT_LOOP_", "ACTIVELOOM_"))
            },
            "GH_REPO": "loomantix/activeloom",
            "ACTIVELOOM_REVIEW_PROFILE": str(profile_path),
            "AGY_CLI": str(agy),
            "AGY_ARGV_LOG": str(argv_log),
            "AGENT_LOOP_REVIEW_BASE_SHA": "b" * 40,
            "AGENT_LOOP_REVIEW_ROUND": "1",
            "AGENT_LOOP_PR_NUMBER": "7",
            "AGENT_LOOP_PR_HEAD_SHA": "c" * 40,
            "AGENT_LOOP_REVIEW_RESULT_FILE": str(tmp_path / "result.json"),
            "AGENT_LOOP_REVIEW_PUSH_HELPER": str(tmp_path / "review-push.sh"),
            "AGENT_LOOP_TRUSTED_AGENTS_ROOT": str(surface),
            "AGENT_LOOP_TRUSTED_BASE_REF": base_ref,
        },
    }


def _run_review_launcher(
    fixture: dict[str, Any], engine: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(fixture["launcher"]), "--engine", engine],
        cwd=fixture["workdir"],
        env={**fixture["env"], "AGENT_LOOP_REVIEW_ENGINE": engine},
        capture_output=True,
        text=True,
        check=False,
    )


def test_review_launcher_takes_the_gemini_model_and_effort_from_the_profile(
    tmp_path: Path,
) -> None:
    fixture = _review_launcher_fixture(
        tmp_path,
        _profile(gemini={"model": "gemini-from-profile", "effort": "medium"}),
    )

    result = _run_review_launcher(fixture, "gemini")

    assert result.returncode == 0, result.stderr
    argv = fixture["argv_log"].read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("--model") + 1] == "gemini-from-profile"
    assert argv[argv.index("--effort") + 1] == "medium"


def test_review_launcher_keeps_a_pinned_agy_model_for_claude_but_takes_its_effort(
    tmp_path: Path,
) -> None:
    """Agy cannot launch a Claude CLI alias, so only the effort is profile-driven."""
    fixture = _review_launcher_fixture(
        tmp_path, _profile(claude={"model": "opus", "effort": "medium"})
    )

    result = _run_review_launcher(fixture, "claude")

    assert result.returncode == 0, result.stderr
    argv = fixture["argv_log"].read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    assert argv[argv.index("--effort") + 1] == "medium"


def test_review_launcher_fails_closed_when_the_profile_lacks_the_engine(
    tmp_path: Path,
) -> None:
    fixture = _review_launcher_fixture(tmp_path, _profile(gemini=None))

    result = _run_review_launcher(fixture, "gemini")

    assert result.returncode != 0
    assert "gemini reviewer settings" in result.stderr
    assert not fixture["argv_log"].exists()
