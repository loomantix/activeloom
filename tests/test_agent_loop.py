"""Deterministic integration coverage for the agent-loop wrapper.

Each test drives the real `.claude/skills/agent-loop/scripts/agent-loop.sh`
against a throwaway git repo + bare remote, with `gh`/`ready.py`/`claude`
replaced by tiny stubs on `PATH`. The cases mirror the Codex wrapper's safety
suite: allowlisting, ready/dependency gates, worktree isolation, dry-run, hook
ordering, claim/assignee-identity races, worker failure + recovery,
capacity/timeout retry with model fallback, private bounded logs, fresh-base
publication, and conflict-marker rejection.
"""

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
AGENT_LOOP = REPO_ROOT / ".claude/skills/agent-loop/scripts/agent-loop.sh"


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
    # Hermetic commits: the worker/hook stubs commit inside worktrees that share
    # this repo's config, so pin signing off regardless of any ambient global
    # `commit.gpgsign = true` (which would otherwise block the throwaway commits).
    _run_git("config", "commit.gpgsign", "false", cwd=repo)

    script = repo / ".claude/skills/agent-loop/scripts/agent-loop.sh"
    ready = repo / ".claude/skills/issues/scripts/ready.py"
    script.parent.mkdir(parents=True)
    ready.parent.mkdir(parents=True)
    shutil.copy2(AGENT_LOOP, script)
    for helper in (
        "agent-loop-state.py",
        "config-doctor.py",
        "hook-gh-guard",
        "hook-git-guard",
        "review-push.sh",
    ):
        shutil.copy2(AGENT_LOOP.parent / helper, script.parent / helper)
    ledger_source = REPO_ROOT / ".claude/skills/critique/scripts/review-ledger.js"
    ledger_target = repo / ".claude/skills/critique/scripts/review-ledger.js"
    ledger_target.parent.mkdir(parents=True)
    shutil.copy2(ledger_source, ledger_target)
    # Sync ships a sibling `package.json` declaring the bundle as ESM; copy it
    # so the fixture receives what a consumer receives.
    shutil.copy2(
        REPO_ROOT / ".claude/skills/critique/scripts/package.json",
        ledger_target.parent / "package.json",
    )
    # claude-platform has no root manifest, which is the most permissive module
    # resolution context there is — the bundle would load here even with no
    # sibling manifest at all. Give the fixture consumer a CommonJS root, the
    # context that breaks an undeclared ESM `.js`, so this suite actually
    # exercises what a consumer repo does rather than what upstream does.
    (repo / "package.json").write_text(
        '{"name": "fixture-consumer", "private": true, "type": "commonjs"}\n',
        encoding="utf-8",
    )
    _write_executable(
        ready,
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "state = os.environ.get('AGENT_STATE_DIR')\n"
        "if state:\n"
        "    with (pathlib.Path(state) / 'ready-args.log').open('a') as handle:\n"
        "        handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "raw = os.environ.get('AGENT_READY_JSON', '[]')\n"
        # Opt-in blockers: {"<issue>": [<open blocker>, ...]}, filtered like
        # ready.py, so a test can prove which blockers the wrapper ignores.
        "blockers = os.environ.get('AGENT_READY_BLOCKERS')\n"
        "if blockers:\n"
        "    import json\n"
        "    args = sys.argv[1:]\n"
        "    ignored = {int(args[i + 1]) for i, arg in enumerate(args[:-1]) if arg == '--ignore-blocker'}\n"
        "    table = json.loads(blockers)\n"
        "    raw = json.dumps([issue for issue in json.loads(raw)\n"
        "        if not set(table.get(str(issue['number']), [])) - ignored])\n"
        "print(raw)\n",
    )
    (repo / "agent-loop-instructions.md").write_text(
        "# Local-only worker instructions\n", encoding="utf-8"
    )
    (repo / ".claude/skills/agent-loop/prompt.txt").write_text(
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
import json, os, pathlib, re, subprocess, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ['AGENT_STATE_DIR'])
input_payload = json.load(sys.stdin) if '--input' in args else None
with (state / 'gh.log').open('a') as handle:
    handle.write(' '.join(args) + '\n')
issues = json.loads(os.environ.get('AGENT_ISSUES_JSON', '{}'))
def advance_base_once():
    advance_marker = state / 'base-advanced'
    if not os.environ.get('AGENT_ADVANCE_BASE_ON_REVIEW') or advance_marker.exists():
        return
    clone = state / 'base-clone'
    subprocess.run(['/usr/bin/git', 'clone', os.environ['REMOTE_PATH'], str(clone)], check=True, capture_output=True)
    subprocess.run(['/usr/bin/git', '-C', str(clone), 'config', 'user.name', 'Test'], check=True)
    subprocess.run(['/usr/bin/git', '-C', str(clone), 'config', 'user.email', 'test@example.invalid'], check=True)
    (clone / 'fresh-base.txt').write_text('fresh\n')
    subprocess.run(['/usr/bin/git', '-C', str(clone), 'add', 'fresh-base.txt'], check=True)
    subprocess.run(['/usr/bin/git', '-C', str(clone), 'commit', '-m', 'chore: advance base'], check=True, capture_output=True)
    subprocess.run(['/usr/bin/git', '-C', str(clone), 'push', 'origin', 'main'], check=True, capture_output=True)
    advance_marker.touch()
if args[:2] == ['api', 'user']:
    actor_file = state / 'gh-actor'
    actor = actor_file.read_text().strip() if actor_file.exists() else 'tester'
    print(actor if '--jq' in args else json.dumps({'login': actor}))
elif args[:2] == ['issue', 'view']:
    number = args[2]
    issue = issues.get(number, {'number': int(number), 'title': 'fixture', 'body': '', 'state': 'OPEN', 'labels': [{'name': 'dev: agent'}], 'assignees': []})
    claimed = state / ('claimed-' + number)
    view_counter = state / ('issue-views-' + number)
    view_count = int(view_counter.read_text() if view_counter.exists() else '0') + 1
    view_counter.write_text(str(view_count))
    if claimed.exists() or (
        view_count > 1
        and ('AGENT_VERIFIED_ASSIGNEE' in os.environ or 'AGENT_VERIFIED_ASSIGNEES' in os.environ)
    ):
        issue = dict(issue)
        if os.environ.get('AGENT_VERIFIED_ASSIGNEES'):
            issue['assignees'] = json.loads(os.environ['AGENT_VERIFIED_ASSIGNEES'])
        else:
            login = os.environ.get('AGENT_VERIFIED_ASSIGNEE', 'tester')
            issue['assignees'] = ([{'login': login}] if login else [])
    if args[3:] == ['--json', 'assignees']:
        if os.environ.get('AGENT_VERIFIED_ASSIGNEES'):
            assignees = json.loads(os.environ['AGENT_VERIFIED_ASSIGNEES'])
        else:
            login = os.environ.get('AGENT_VERIFIED_ASSIGNEE', 'tester')
            assignees = ([{'login': login}] if login else [])
        print(json.dumps({'assignees': assignees}))
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
        claimed.unlink(missing_ok=True)
elif args[:2] == ['repo', 'view']:
    if 'nameWithOwner' in ' '.join(args):
        print('fixture/consumer')
    elif 'owner' in ' '.join(args):
        print('fixture')
    elif 'name' in ' '.join(args):
        print('consumer')
    else:
        print('fixture/consumer')
elif args[:2] == ['pr', 'view']:
    joined = ' '.join(args)
    if '--json number' in joined:
        print('1')
    elif args[-2:] == ['--jq', '.baseRefOid']:
        base_name_file = state / 'pr-base-branch'
        base_name = base_name_file.read_text() if base_name_file.exists() else 'main'
        base_head = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/' + base_name],
            check=True, capture_output=True, text=True
        ).stdout.split()[0]
        base_oid_file = state / 'pr-base-oid'
        if base_oid_file.exists():
            base_head = base_oid_file.read_text().strip()
        print(os.environ.get('AGENT_PR_BASE_OID', base_head))
    elif 'state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid' in joined:
        branch = (state / 'pr-branch').read_text()
        remote_head = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch],
            check=True, capture_output=True, text=True
        ).stdout.split()[0]
        base_name_file = state / 'pr-base-branch'
        base_name = base_name_file.read_text() if base_name_file.exists() else 'main'
        base_head = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/' + base_name],
            check=True, capture_output=True, text=True
        ).stdout.split()[0]
        # A file overrides the env var so a hook can move the base mid-run.
        base_oid_file = state / 'pr-base-oid'
        if base_oid_file.exists():
            base_head = base_oid_file.read_text().strip()
        print('\t'.join([
            os.environ.get('AGENT_PR_STATE', 'OPEN'),
            os.environ.get(
                'AGENT_PR_IS_DRAFT',
                'false' if (state / 'pr-ready').exists() else 'true',
            ),
            os.environ.get('AGENT_PR_HEAD_REF_NAME', branch),
            os.environ.get('AGENT_PR_HEAD_OID', remote_head),
            os.environ.get('AGENT_PR_BASE_REF_NAME', base_name),
            os.environ.get('AGENT_PR_BASE_OID', base_head),
        ]))
    elif 'headRefOid' in joined:
        branch = (state / 'pr-branch').read_text()
        remote_head = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch],
            check=True, capture_output=True, text=True
        ).stdout.split()[0]
        print(os.environ.get('AGENT_PR_HEAD_OID', remote_head))
    else:
        number = args[2]
        row = json.loads(os.environ.get('AGENT_PRS_JSON', '{}')).get(number)
        if row:
            print('\t'.join(str(value) for value in row))
        else:
            sys.exit(1)
elif args[:2] == ['pr', 'create']:
    if os.environ.get('AGENT_PR_CREATE_FAIL'):
        print('pr create failed (stub)', file=sys.stderr)
        sys.exit(1)
    for transient in ('pr-ready', 'reviews.json', 'review-threads.json', 'issue-comments.json'):
        (state / transient).unlink(missing_ok=True)
    branch = subprocess.run(
        ['git', 'branch', '--show-current'], check=True, capture_output=True, text=True
    ).stdout.strip()
    (state / 'pr-branch').write_text(branch)
    (state / 'pr-base-branch').write_text(args[args.index('--base') + 1] if '--base' in args else 'main')
    print('https://example.invalid/pr/1')
elif args[:2] == ['pr', 'edit']:
    if os.environ.get('AGENT_PR_EDIT_FAIL'):
        print('pr edit failed (stub)', file=sys.stderr)
        sys.exit(1)
    (state / 'pr-edited').touch()
elif args[:2] == ['pr', 'ready']:
    if os.environ.get('AGENT_PR_READY_FAIL'):
        print('pr ready failed (stub)', file=sys.stderr)
        sys.exit(1)
    (state / 'pr-ready').touch()
elif args[:2] == ['pr', 'close']:
    (state / 'pr-closed').touch()
elif args[:2] == ['pr', 'review']:
    body = args[args.index('--body') + 1]
    advance_base_once()
    reviews_file = state / 'reviews.json'
    reviews = json.loads(reviews_file.read_text()) if reviews_file.exists() else []
    reviews.append({
        'body': body,
        'user': {'login': os.environ.get('AGENT_COMMENT_AUTHOR', 'tester')},
        'commit_id': subprocess.run(
            ['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True
        ).stdout.strip(),
    })
    reviews_file.write_text(json.dumps(reviews))
    with (state / 'pr-comments.log').open('a') as handle:
        handle.write(body.replace('\n', ' ') + '\n')
elif args[:2] == ['api', 'graphql']:
    if os.environ.get('AGENT_GRAPHQL_PARTIAL'):
        # A fetch that fails after emitting a parseable but incomplete page.
        print(json.dumps([{'data': {'repository': {'pullRequest': {
            'reviewThreads': {'nodes': [], 'pageInfo': {
                'hasNextPage': True, 'endCursor': 'cursor'
            }}
        }}}}]))
        sys.exit(1)
    threads_file = state / 'review-threads.json'
    if threads_file.exists():
        nodes = json.loads(threads_file.read_text())
    else:
        nodes = json.loads(os.environ.get('AGENT_REVIEW_THREADS_JSON', '[]'))
    if os.environ.get('AGENT_PROJECT_REVIEW_THREAD_TOPOLOGY'):
        query_arg = next((value for value in args if value.startswith('query=')), '')
        selection = re.search(
            r'reviewThreads\s*\([^)]*\)\s*\{\s*nodes\s*\{(?P<fields>.*?)comments\s*\(',
            query_arg,
            re.S,
        )
        fields = selection.group('fields') if selection else ''
        for node in nodes:
            if not re.search(r'\bid\b', fields):
                node.pop('id', None)
            if not re.search(r'repository\s*\{\s*nameWithOwner\b', fields):
                node.pop('repository', None)
            if not re.search(r'pullRequest\s*\{\s*number\b', fields):
                node.pop('pullRequest', None)
        if not re.search(r'repository\s*\{\s*nameWithOwner\s*\}', query_arg):
            for node in nodes:
                node.pop('repository', None)
        if not re.search(r'pullRequest\s*\{\s*number\s*\}', query_arg):
            for node in nodes:
                node.pop('pullRequest', None)
    default_author = os.environ.get('AGENT_THREAD_AUTHOR', 'tester')
    for node in nodes:
        for comment in node.get('comments', {}).get('nodes', []):
            comment.setdefault('author', {'login': default_author})
    print(json.dumps([{'data': {'repository': {'pullRequest': {
        'reviewThreads': {'nodes': nodes, 'pageInfo': {
            'hasNextPage': False, 'endCursor': None
        }}
    }}}}]))
elif args[0] == 'api' and any(value.endswith('/issues/1/comments') for value in args) and '-X' in args:
    advance_base_once()
    if input_payload is not None:
        body = input_payload['body']
    else:
        form = args[args.index('-f') + 1]
        assert form.startswith('body='), form
        body = form[len('body='):]
    records_file = state / 'issue-comments.json'
    records = json.loads(records_file.read_text()) if records_file.exists() else []
    record = {'id': 1000 + len(records), 'body': body, 'user': {'login': 'tester'}}
    records.append(record)
    records_file.write_text(json.dumps(records))
    with (state / 'pr-comments.log').open('a') as handle:
        handle.write(body.replace('\n', ' ') + '\n')
    print(json.dumps(record))
elif args[0] == 'api' and any(value.startswith('repos/') for value in args):
    endpoint = next(value for value in args if value.startswith('repos/'))
    # Clean-pass attestation lookup: issue comments carry the markers this
    # fixture's hooks post; the review-comment and review endpoints are empty.
    records_file = state / 'issue-comments.json'
    records = json.loads(records_file.read_text()) if records_file.exists() else []
    author = os.environ.get('AGENT_COMMENT_AUTHOR', 'tester')
    for record in records:
        record['user'] = {'login': author}
    endpoint_path = endpoint.split('?', 1)[0]
    if '/compare/' in endpoint_path:
        comparison = endpoint_path.split('/compare/', 1)[1]
        before, after = comparison.split('...', 1)
        print(json.dumps({
            'status': 'ahead',
            'merge_base_commit': {'sha': before},
            'commits': [{'sha': after}],
        }))
    elif endpoint_path.endswith('pulls/1/reviews'):
        reviews_file = state / 'reviews.json'
        reviews = json.loads(reviews_file.read_text()) if reviews_file.exists() else []
        print(json.dumps([reviews] if '--slurp' in args else reviews))
    elif endpoint_path.endswith('issues/1/comments'):
        print(json.dumps([records] if '--slurp' in args else records))
    elif '/issues/comments/' in endpoint_path:
        comment_id = int(endpoint_path.rsplit('/', 1)[1])
        print(json.dumps(next(record for record in records if record['id'] == comment_id)))
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


def _clean_pass_hook(engine: str) -> str:
    return (
        f"printf '{engine}\\n' >> \"$EVENT_LOG\"; "
        "printf '%s\\n' \"$AGENT_LOOP_REVIEW_BASE_SHA\" >> "
        '"$AGENT_STATE_DIR/review-bases.log"; '
        'gh pr review "$AGENT_LOOP_PR_NUMBER" --comment --body '
        f'"<!-- local-review-pass:v1 engine={engine} '
        "round=$AGENT_LOOP_REVIEW_ROUND head=$AGENT_LOOP_PR_HEAD_SHA -->"
        '\\nno new material findings"'
    )


def _clean_v3_hook(engine: str) -> str:
    return (
        f"printf '{engine}\\n' >> \"$EVENT_LOG\"; "
        'jq -n --arg engine "$AGENT_LOOP_REVIEW_ENGINE" '
        '--argjson round "$AGENT_LOOP_REVIEW_ROUND" '
        '--arg base "$AGENT_LOOP_REVIEW_BASE_SHA" '
        '--arg head "$AGENT_LOOP_PR_HEAD_SHA" '
        '\'{version:3,status:"clean",engine:$engine,round:$round,'
        "baseSha:$base,beforeSha:$head,afterSha:$head,classification:null,"
        "findingFingerprints:[],finalLaneComplete:true}' "
        '> "$AGENT_LOOP_REVIEW_RESULT_FILE"'
    )


def _cleanup_v3_hook(engine: str) -> str:
    return (
        f"printf '{engine}\\n' >> \"$EVENT_LOG\"; "
        'before="$AGENT_LOOP_PR_HEAD_SHA"; '
        f"printf '{engine} cleanup\\n' > review-cleanup.txt; "
        "git add review-cleanup.txt; git commit -m 'refactor: cleanup'; "
        '"$AGENT_LOOP_REVIEW_PUSH_HELPER"; '
        "after=$(git rev-parse HEAD); "
        "printf '<!-- local-review-refactor:v1 engine="
        f"{engine} head=%s outcome=committed -->\\nCleanup committed.\\n' "
        '"$before" > "$AGENT_LOOP_LOG_DIR/refactor.md"; '
        "node .claude/skills/critique/scripts/review-ledger.js post-pr-comment "
        '--repo fixture/consumer --pr "$AGENT_LOOP_PR_NUMBER" --head "$after" '
        '--body-file "$AGENT_LOOP_LOG_DIR/refactor.md"; '
        'jq -n --arg engine "$AGENT_LOOP_REVIEW_ENGINE" '
        '--argjson round "$AGENT_LOOP_REVIEW_ROUND" '
        '--arg base "$AGENT_LOOP_REVIEW_BASE_SHA" '
        '--arg before "$before" --arg after "$after" '
        '\'{version:3,status:"changed",engine:$engine,round:$round,'
        'baseSha:$base,beforeSha:$before,afterSha:$after,classification:"minor",'
        "findingFingerprints:[],finalLaneComplete:true}' "
        '> "$AGENT_LOOP_REVIEW_RESULT_FILE"'
    )


def _committed_review_hook(engine: str, classification: str = "minor") -> str:
    return (
        f"printf '{engine}\\n' >> \"$EVENT_LOG\"; "
        f"printf '{engine} fix\\n' > \"review-{engine}-$AGENT_LOOP_REVIEW_ROUND.txt\"; "
        f'git add "review-{engine}-$AGENT_LOOP_REVIEW_ROUND.txt"; '
        f"git commit -m 'fix: {engine} review'; "
        "after=$(git rev-parse HEAD); "
        '"$AGENT_LOOP_REVIEW_PUSH_HELPER"; '
        f"printf '{classification}\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\"; "
        'jq -n --arg engine "$AGENT_LOOP_REVIEW_ENGINE" '
        '--arg round "$AGENT_LOOP_REVIEW_ROUND" '
        '--arg before "$AGENT_LOOP_PR_HEAD_SHA" --arg after "$after" '
        "'[{isResolved:true,comments:{nodes:["
        '{body:("<!-- local-review:v1 engine="+$engine+" round="+$round+" head="+$before+" fingerprint=fixture-"+$engine+" -->\\nFinding"),databaseId:1,author:{login:"tester"}},'
        '{body:("<!-- local-review-disposition:v1 engine="+$engine+" round="+$round+" head="+$after+" fingerprint=fixture-"+$engine+" outcome=fixed -->\\nFixed in `"+$after+"`.\\n\\nValidation: fixture passed."),databaseId:2,author:{login:"tester"}}'
        "],pageInfo:{hasNextPage:false}}}]' "
        '> "$AGENT_STATE_DIR/review-threads.json"; '
        "gh api repos/{owner}/{repo}/issues/1/comments -X POST "
        '-f body="<!-- local-review-complete:v1 engine='
        f"{engine} round=$AGENT_LOOP_REVIEW_ROUND "
        'before=$AGENT_LOOP_PR_HEAD_SHA head=$after -->"'
    )


def _config(tmp_path: Path, **overrides: str | int) -> str:
    values: dict[str, str | int] = {
        "base_branch": "main",
        "setup_hook": "printf 'setup\\n' >> \"$EVENT_LOG\"",
        "validation_hook": "printf 'validate\\n' >> \"$EVENT_LOG\"",
        # A conforming no-fix review pass: it reviews, commits nothing, and
        # attests the exact head it read.
        "claude_review_hook": _clean_pass_hook("claude"),
        "codex_review_hook": _clean_pass_hook("codex"),
        "worker_hook": "printf 'worker\\n' >> \"$EVENT_LOG\"; printf 'done\\n' > result.txt; git add result.txt; git commit -m 'fix: worker'",
        "worker_retries": 1,
        "worker_timeout_seconds": 5,
        "hook_timeout_seconds": 10,
        "review_contract_version": 2,
        "config_doctor": "false",
        "review_max_rounds": 4,
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
    if values["review_contract_version"] == 3:
        for key in ("codex_review_hook", "claude_review_hook"):
            values[key] = (
                ': "$AGENT_LOOP_REVIEW_PUSH_HELPER" '
                f'"$AGENT_LOOP_REVIEW_RESULT_FILE" write-result; {values[key]}'
            )
    return "\n".join(f"{key} = {value}" for key, value in values.items()) + "\n"


def _config_v3(tmp_path: Path, **overrides: str | int) -> str:
    values: dict[str, str | int] = {
        "review_contract_version": 3,
        "codex_review_hook": _clean_v3_hook("codex"),
        "claude_review_hook": _clean_v3_hook("claude"),
    }
    values.update(overrides)
    return _config(tmp_path, **values)


def _environment(
    fixture: tuple[Path, Path, Path, Path],
    issues: list[dict[str, object]],
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    repo, _, bin_dir, state_dir = fixture
    # Hooks run under `bash -lc`, a login shell that re-sources profile files. On
    # a developer box those dotfiles prepend real tool paths (e.g. a genuine
    # `claude` in ~/.local/bin), shadowing the stubs this suite installs in
    # bin_dir. Point HOME at an empty dir so the login shell finds no profile to
    # reorder PATH, keeping the run hermetic here and on CI alike.
    home = bin_dir.parent / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    # The temporary ready.py/gh/claude fixtures are black-box shell dependencies,
    # not coverage targets. pytest-cov exports COV_CORE_* for subprocess
    # collection; letting these standalone stubs auto-start coverage can produce
    # statement data that pytest-cov 6 cannot combine with this repo's branch
    # data.
    for key in [name for name in env if name.startswith("COV_CORE_")]:
        env.pop(key)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HOME": str(home),
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
    return env


def _run(
    fixture: tuple[Path, Path, Path, Path],
    args: list[str],
    *,
    issues: list[dict[str, object]],
    config: str,
    extra_env: dict[str, str] | None = None,
    timeout: int = 30,
    stdin: int | None = None,
) -> subprocess.CompletedProcess[str]:
    repo = fixture[0]
    (repo / ".claude/skills/agent-loop/agent-loop.config").write_text(
        config, encoding="utf-8"
    )
    env = _environment(fixture, issues, extra_env)
    return subprocess.run(
        [str(repo / ".claude/skills/agent-loop/scripts/agent-loop.sh"), *args],
        cwd=repo,
        env=env,
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_script_remains_executable_and_valid_bash() -> None:
    assert stat.S_IMODE(AGENT_LOOP.stat().st_mode) == 0o755
    subprocess.run(["bash", "-n", str(AGENT_LOOP)], check=True)


@pytest.mark.parametrize(
    ("missing_token", "expected_error"),
    [
        ("AGENT_LOOP_REVIEW_PUSH_HELPER", "must use AGENT_LOOP_REVIEW_PUSH_HELPER"),
        ("AGENT_LOOP_REVIEW_RESULT_FILE", "must write AGENT_LOOP_REVIEW_RESULT_FILE"),
        ("write-result", "must use review-ledger.js write-result"),
    ],
)
def test_v3_hook_contract_is_preflighted_before_claim(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    missing_token: str,
    expected_error: str,
) -> None:
    config = _config(
        tmp_path,
        review_contract_version=3,
        codex_review_hook=_clean_v3_hook("codex"),
        claude_review_hook=_clean_v3_hook("claude"),
    ).replace(missing_token, "missing")
    result = _run(
        consumer,
        ["--issues", "20"],
        issues=[_issue(20)],
        config=config,
    )
    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not (consumer[3] / "claimed-20").exists()


def test_v3_missing_structured_result_is_not_treated_as_clean(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "30"],
        issues=[_issue(30)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook="true",
            claude_review_hook="true",
        ),
    )
    assert result.returncode != 0
    assert "valid contract v3 result" in result.stderr
    comments = consumer[3] / "pr-comments.log"
    assert not comments.exists()


def _counting_hook(engine: str, body: str) -> str:
    return f"printf '{engine}-attempt\\n' >> \"$EVENT_LOG\"; {body}"


def test_review_hook_that_ends_without_a_result_is_retried_once_in_place(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The first Claude attempt ends its turn without writing a result and
    # without touching the PR. Retrying it costs one pass; restarting the round
    # would re-run Codex against an unchanged head.
    claude = _counting_hook(
        "claude",
        'if [ ! -e "$AGENT_STATE_DIR/claude-ended-early" ]; then '
        'touch "$AGENT_STATE_DIR/claude-ended-early"; exit 0; fi; '
        + _clean_v3_hook("claude"),
    )
    result = _run(
        consumer,
        ["--issues", "33"],
        issues=[_issue(33)],
        config=_config_v3(tmp_path, claude_review_hook=claude),
        timeout=90,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "retry: hook-ended-without-result" in result.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8")
    assert events.count("codex\n") == 1
    assert events.count("claude-attempt\n") == 2
    comments = (consumer[3] / "pr-comments.log").read_text(encoding="utf-8")
    assert "local-review-pass:v3 engine=claude round=1" in comments
    assert "round=2" not in comments
    log_dir = next((tmp_path / "logs").glob("*-issue-33-*"))
    phases = [
        json.loads(line)
        for line in (log_dir / "phases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event"] == "retry" for event in phases)


def test_review_hook_that_ends_without_a_result_twice_stops_categorized(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "34"],
        issues=[_issue(34)],
        config=_config_v3(
            tmp_path,
            claude_review_hook=_counting_hook("claude", "echo 'suite still running'; exit 0"),
        ),
        timeout=90,
    )
    assert result.returncode != 0
    assert "valid contract v3 result" in result.stderr
    assert "Stop category: no-result/hook-ended-early" in result.stderr
    assert "suite still running" in result.stderr
    events = (consumer[3] / "events.log").read_text(encoding="utf-8")
    assert events.count("codex\n") == 1
    assert events.count("claude-attempt\n") == 2
    assert not (consumer[3] / "pr-ready").exists()


@pytest.mark.parametrize("trace", ["push", "finding"])
def test_review_hook_that_mutated_before_ending_without_a_result_is_not_retried(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path, trace: str
) -> None:
    if trace == "push":
        body = (
            "printf 'fix\\n' > early-fix.txt; git add early-fix.txt; "
            "git commit -m 'fix: early'; \"$AGENT_LOOP_REVIEW_PUSH_HELPER\"; exit 0"
        )
    else:
        body = (
            "jq -n '[{id:\"THREAD-EARLY\",isResolved:false,"
            "repository:{nameWithOwner:\"fixture/consumer\"},pullRequest:{number:1},"
            "comments:{nodes:[{body:\"Finding.\",databaseId:77,author:{login:\"tester\"}}],"
            "pageInfo:{hasNextPage:false}}}]' > \"$AGENT_STATE_DIR/review-threads.json\"; exit 0"
        )
    result = _run(
        consumer,
        ["--issues", "35"],
        issues=[_issue(35)],
        config=_config_v3(tmp_path, claude_review_hook=_counting_hook("claude", body)),
        timeout=90,
    )
    assert result.returncode != 0
    assert "retry: hook-ended-without-result" not in result.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8")
    assert events.count("claude-attempt\n") == 1


def test_v3_clean_results_attest_and_converge(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "31"],
        issues=[_issue(31)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook=_clean_v3_hook("codex"),
            claude_review_hook=_clean_v3_hook("claude"),
        ),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert (consumer[3] / "pr-ready").exists()
    comments = (consumer[3] / "pr-comments.log").read_text(encoding="utf-8")
    assert "local-review-pass:v3 engine=codex round=1" in comments
    assert "local-review-pass:v3 engine=claude round=1" in comments


def test_batch_resume_finalizes_interrupted_first_issue_then_processes_second(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        review_contract_version=3,
        codex_review_hook=_clean_v3_hook("codex"),
        claude_review_hook=_clean_v3_hook("claude"),
    )
    first = _run(
        consumer,
        ["--issues", "70,71", "--iterations", "2"],
        issues=[_issue(70), _issue(71)],
        config=config,
        extra_env={"AGENT_INTERRUPT_AFTER_CHILD_FINALIZED": "1"},
        timeout=120,
    )
    assert first.returncode != 0
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert batch["cursor"] == 0
    assert batch["issues"][0]["status"] == "active"
    assert batch["issues"][0]["childRunState"]
    assert batch["issues"][1]["status"] == "pending"

    second = _run(
        consumer,
        ["--resume-batch", str(batch_file)],
        issues=[_issue(70, assigned=True), _issue(71)],
        config=config,
        timeout=120,
    )
    assert second.returncode == 0, second.stderr + second.stdout
    final = json.loads(batch_file.read_text(encoding="utf-8"))
    assert final["cursor"] == 2
    assert [row["status"] for row in final["issues"]] == [
        "finalized",
        "finalized",
    ]
    branches = _run_git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent-loop",
        cwd=consumer[1],
    ).stdout
    assert "issue-70" in branches and "issue-71" in branches


def test_batch_hooks_do_not_inherit_the_batch_lock(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    setup_hook = (
        'test -z "${AGENT_LOOP_BATCH_LOCK_FD:-}"; '
        "for fd in /proc/self/fd/*; do "
        'case "$(readlink "$fd" 2>/dev/null || true)" in '
        "*-batch-*.json.lock) exit 71 ;; esac; done"
    )
    result = _run(
        consumer,
        ["--issues", "78,79", "--iterations", "1"],
        issues=[_issue(78), _issue(79)],
        config=_config_v3(tmp_path, setup_hook=setup_hook),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "paused cleanly at the 1-issue iteration cap" in result.stdout


def test_batch_resume_hooks_do_not_inherit_the_batch_lock(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    validation_hook = (
        'test -z "${AGENT_LOOP_BATCH_LOCK_FD:-}"; '
        "for fd in /proc/self/fd/*; do "
        'case "$(readlink "$fd" 2>/dev/null || true)" in '
        "*-batch-*.json.lock) exit 71 ;; esac; done"
    )
    config = _config_v3(tmp_path, validation_hook=validation_hook)
    first = _run(
        consumer,
        ["--issues", "83,84", "--iterations", "2"],
        issues=[_issue(83), _issue(84)],
        config=config,
        extra_env={"AGENT_INTERRUPT_AFTER_CHILD_FINALIZED": "1"},
        timeout=120,
    )
    assert first.returncode != 0
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))

    resumed = _run(
        consumer,
        ["--resume-batch", str(batch_file)],
        issues=[_issue(83, assigned=True), _issue(84)],
        config=config,
        timeout=120,
    )

    assert resumed.returncode == 0, resumed.stderr + resumed.stdout


def test_batch_resume_rejects_a_child_checkpoint_for_another_issue(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    first = _run(
        consumer,
        ["--issues", "80,81", "--iterations", "2"],
        issues=[_issue(80), _issue(81)],
        config=_config_v3(tmp_path),
        extra_env={"AGENT_INTERRUPT_AFTER_CHILD_FINALIZED": "1"},
        timeout=120,
    )
    assert first.returncode != 0
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert batch["issues"][0]["childRunState"]
    batch["allowlist"][0] = 82
    batch["issues"][0]["issue"] = 82
    batch_file.write_text(json.dumps(batch), encoding="utf-8")

    resumed = _run(
        consumer,
        ["--resume-batch", str(batch_file)],
        issues=[_issue(80, assigned=True), _issue(82)],
        config=_config_v3(tmp_path),
        timeout=30,
    )
    assert resumed.returncode != 0
    assert (
        "Batch issue #82 does not match its child review checkpoint" in resumed.stderr
    )
    preserved = json.loads(batch_file.read_text(encoding="utf-8"))
    assert preserved["cursor"] == 0
    assert preserved["issues"][0]["status"] == "active"


def _ends_early_for(issue: int) -> str:
    return (
        f'if [ "$AGENT_LOOP_ISSUE_ID" = {issue} ]; then echo "suite still running"; exit 0; fi; '
        + _clean_v3_hook("claude")
    )


def test_park_mode_parks_a_resumable_failure_and_its_dependent_then_finishes_the_lane(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # One issue whose Claude pass keeps ending without a result used to halt
    # the lane with every later issue unstarted.
    result = _run(
        consumer,
        ["--issues", "50,51,52", "--iterations", "3"],
        issues=[_issue(50), _issue(51, "Depends on #50"), _issue(52)],
        config=_config_v3(
            tmp_path, claude_review_hook=_ends_early_for(50), batch_on_issue_failure="park"
        ),
        extra_env={"AGENT_READY_BLOCKERS": json.dumps({"51": [50]})},
        timeout=300,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 3, output
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["parked", "parked", "finalized"]
    assert batch["issues"][0]["stopCategory"] == "no-result/hook-ended-early"
    assert batch["issues"][1]["stopCategory"] == "blocked-by-parked"
    child = batch["issues"][0]["childRunState"]
    assert "#50 parked (no-result/hook-ended-early): '" in output
    assert f"--resume-run '{child}'" in output
    assert "#51 parked (blocked-by-parked)" in output
    assert not (consumer[3] / "claimed-51").exists()
    child_state = json.loads(Path(child).read_text(encoding="utf-8"))
    assert Path(child_state["worktree"]).exists()


def test_parking_a_stacked_issue_continues_on_the_integration_base(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "60,61,62", "--iterations", "3"],
        issues=[_issue(60), _issue(61, "Depends on #60"), _issue(62)],
        config=_config_v3(
            tmp_path,
            worker_hook=_PER_ISSUE_WORKER,
            claude_review_hook=_ends_early_for(61),
            dependency_gate="batch-stack",
            batch_on_issue_failure="park",
        ),
        extra_env={"AGENT_READY_BLOCKERS": json.dumps({"61": [60]})},
        timeout=180,
    )
    assert result.returncode == 3, result.stderr + result.stdout
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["finalized", "parked", "finalized"]
    independent = json.loads(
        Path(batch["issues"][2]["childRunState"]).read_text(encoding="utf-8")
    )
    assert independent["baseBranch"] == "main"
    assert "stackedOn" not in batch["issues"][2]


def test_failed_resumed_batch_child_can_be_parked_before_starting_the_next_issue(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    options = {
        "worker_hook": _PER_ISSUE_WORKER,
        "claude_review_hook": _ends_early_for(80),
    }
    first = _run(
        consumer,
        ["--issues", "80,81", "--iterations", "2"],
        issues=[_issue(80), _issue(81)],
        config=_config_v3(tmp_path, **options),
        timeout=120,
    )
    assert first.returncode == 1, first.stderr + first.stdout
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    resumed = _run(
        consumer,
        ["--resume-batch", str(batch_file), "--iterations", "2"],
        issues=[_issue(80, assigned=True), _issue(81)],
        config=_config_v3(tmp_path, **options, batch_on_issue_failure="park"),
        timeout=120,
    )
    assert resumed.returncode == 3, resumed.stderr + resumed.stdout
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["parked", "finalized"]


@pytest.mark.parametrize("selection_flag", ["--include-assigned", "--resume"])
def test_parking_preserves_assigned_issue_selection(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path, selection_flag: str
) -> None:
    result = _run(
        consumer,
        ["--issues", "90,91", "--iterations", "2", selection_flag],
        issues=[_issue(90), _issue(91, assigned=True)],
        config=_config_v3(
            tmp_path,
            worker_hook=_PER_ISSUE_WORKER,
            claude_review_hook=_ends_early_for(90),
            batch_on_issue_failure="park",
        ),
        timeout=120,
    )
    assert result.returncode == 3, result.stderr + result.stdout
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["parked", "finalized"]


def test_park_mode_still_stops_on_an_uncertain_push(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The pass pushed a commit and then ended without a result: the remote no
    # longer matches the checkpoint and no result explains the move.
    claude = (
        'if [ "$AGENT_LOOP_ISSUE_ID" = 53 ]; then '
        "printf 'fix\\n' > early-fix.txt; git add early-fix.txt; "
        "git commit -m 'fix: early'; \"$AGENT_LOOP_REVIEW_PUSH_HELPER\"; exit 0; fi; "
        + _clean_v3_hook("claude")
    )
    result = _run(
        consumer,
        ["--issues", "53,54", "--iterations", "2"],
        issues=[_issue(53), _issue(54)],
        config=_config_v3(tmp_path, claude_review_hook=claude, batch_on_issue_failure="park"),
        timeout=120,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "Batch issue #53 was not parked: its head moved past the checkpoint without a matching review result"
        in result.stderr
    )
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["active", "pending"]


def test_stop_mode_is_the_default_and_does_not_park(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "55,56", "--iterations", "2"],
        issues=[_issue(55), _issue(56)],
        config=_config_v3(tmp_path, claude_review_hook=_ends_early_for(55)),
        timeout=120,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "parked" not in (result.stdout + result.stderr).lower()
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["active", "pending"]


_PER_ISSUE_WORKER = (
    "printf 'worker\\n' >> \"$EVENT_LOG\"; "
    'printf "%s\\n" "$AGENT_LOOP_ISSUE_ID" > "result-$AGENT_LOOP_ISSUE_ID.txt"; '
    'git add "result-$AGENT_LOOP_ISSUE_ID.txt"; git commit -m "fix: issue $AGENT_LOOP_ISSUE_ID"'
)


def test_batch_stack_builds_a_dependent_issue_on_its_unmerged_predecessor(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The dependent used to start from the base without its predecessor's
    # change, so the two PRs conflicted and the dependent was reviewed against
    # code that would not exist once the predecessor merged.
    result = _run(
        consumer,
        ["--issues", "60,61", "--iterations", "2"],
        issues=[_issue(60), _issue(61, "Depends on #60")],
        config=_config_v3(
            tmp_path,
            # The dependent's worker must see its predecessor's work.
            worker_hook='if [ "$AGENT_LOOP_ISSUE_ID" = 61 ]; then test -f result-60.txt || exit 9; fi; '
            + _PER_ISSUE_WORKER,
            dependency_gate="batch-stack",
        ),
        extra_env={"AGENT_READY_BLOCKERS": json.dumps({"61": [60]})},
        timeout=180,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["finalized", "finalized"]
    assert batch["issues"][1]["stackedOn"] == 60
    parent = json.loads(Path(batch["issues"][0]["childRunState"]).read_text(encoding="utf-8"))
    child = json.loads(Path(batch["issues"][1]["childRunState"]).read_text(encoding="utf-8"))
    # Built from the predecessor's reviewed head, with its branch as the PR base.
    assert child["baseBranch"] == parent["branch"]
    assert child["baseSha"] == parent["headSha"]
    creates = [line for line in (consumer[3] / "gh.log").read_text(encoding="utf-8").splitlines()
               if line.startswith("pr create")]
    assert "--base main" in creates[0]
    assert f"--base {parent['branch']}" in creates[1]
    # Review and publication are scoped to the dependent's own commits.
    child_log = Path(str(child["logDir"]))
    codex_result = json.loads((child_log / "codex-review-round-1.result.json").read_text(encoding="utf-8"))
    assert codex_result["baseSha"] == parent["headSha"]
    changed = _run_git(
        "diff", "--name-only", f"{parent['headSha']}..{child['headSha']}", cwd=consumer[0]
    ).stdout.split()
    assert changed == ["result-61.txt"]
    assert _run_git("merge-base", "--is-ancestor", str(parent["headSha"]), str(child["headSha"]), cwd=consumer[0])
    assert "--ignore-blocker 60" in (consumer[3] / "ready-args.log").read_text(encoding="utf-8")
    assert f"Stacked PR: after {parent['branch']} merges, retarget it" in result.stdout
    body = (child_log / "pr-body-final.md").read_text(encoding="utf-8")
    assert f"Stacked on `{parent['branch']}`" in body


def test_batch_stack_resumes_an_interrupted_stacked_issue(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The parent issue stays open until its branch merges. A resumed stacked
    # issue has no batch state loaded and used to drop out of the ready queue.
    config = _config_v3(tmp_path, worker_hook=_PER_ISSUE_WORKER, dependency_gate="batch-stack")
    blockers = {"AGENT_READY_BLOCKERS": json.dumps({"69": [68]})}
    issues = [_issue(68), _issue(69, "Depends on #68")]
    first = _run(
        consumer, ["--issues", "68,69", "--iterations", "1"],
        issues=issues, config=config, extra_env=blockers, timeout=180,
    )
    assert first.returncode == 0, first.stderr + first.stdout
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    second = _run(
        consumer, ["--resume-batch", str(batch_file)],
        issues=issues, config=config,
        extra_env={**blockers, "AGENT_INTERRUPT_AFTER_CHILD_FINALIZED": "1"}, timeout=180,
    )
    assert second.returncode != 0
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["finalized", "active"]
    third = _run(
        consumer, ["--resume-batch", str(batch_file)],
        issues=[_issue(68), _issue(69, "Depends on #68", assigned=True)],
        config=config, extra_env=blockers, timeout=180,
    )
    assert third.returncode == 0, third.stderr + third.stdout
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["finalized", "finalized"]
    assert batch["issues"][1]["stackedOn"] == 68


def test_batch_stack_rejects_a_parent_checkpoint_from_another_issue(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    config = _config_v3(tmp_path, worker_hook=_PER_ISSUE_WORKER, dependency_gate="batch-stack")
    blockers = {"AGENT_READY_BLOCKERS": json.dumps({"69": [68]})}
    issues = [_issue(68), _issue(69, "Depends on #68")]
    first = _run(
        consumer, ["--issues", "68,69", "--iterations", "1"],
        issues=issues, config=config, extra_env=blockers, timeout=180,
    )
    assert first.returncode == 0, first.stderr + first.stdout
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    parent_state_file = Path(batch["issues"][0]["childRunState"])
    parent_state = json.loads(parent_state_file.read_text(encoding="utf-8"))
    parent_state["issue"] = 67
    parent_state_file.write_text(json.dumps(parent_state), encoding="utf-8")

    resumed = _run(
        consumer, ["--resume-batch", str(batch_file)],
        issues=issues, config=config, extra_env=blockers, timeout=180,
    )
    assert resumed.returncode == 1
    assert "dependency issue #68 has no matching finalized review checkpoint" in resumed.stderr


def test_merged_to_base_gate_still_holds_a_dependent_batch_issue(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "62,63", "--iterations", "2"],
        issues=[_issue(62), _issue(63, "Depends on #62")],
        config=_config_v3(tmp_path, worker_hook=_PER_ISSUE_WORKER, dependency_gate="merged-to-base"),
        timeout=180,
    )
    assert result.returncode == 1, result.stderr + result.stdout
    assert "Ordered batch stopped at dependency-blocked issue #63" in result.stderr
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["finalized", "pending"]
    assert "--ignore-blocker" not in (consumer[3] / "ready-args.log").read_text(encoding="utf-8")


def test_batch_stack_parks_an_issue_whose_dependency_bailed(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    worker = (
        'if [ "$AGENT_LOOP_ISSUE_ID" = 64 ]; then '
        "printf 'agent-bail: spec-gap\\n' > \"$AGENT_LOOP_HANDOFF_FILE\"; exit 0; fi; "
        + _PER_ISSUE_WORKER
    )
    result = _run(
        consumer,
        ["--issues", "64,65", "--iterations", "2"],
        issues=[_issue(64), _issue(65, "Depends on #64")],
        config=_config_v3(
            tmp_path, worker_hook=worker, dependency_gate="batch-stack",
            batch_on_issue_failure="park",
        ),
        extra_env={"AGENT_READY_BLOCKERS": json.dumps({"65": [64]})},
        timeout=120,
    )
    assert result.returncode == 3, result.stderr + result.stdout
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["bailed", "parked"]
    assert batch["issues"][1]["stopCategory"] == "blocked-by-dependency"
    assert not (consumer[3] / "claimed-65").exists()
    assert not any((tmp_path / "worktrees").glob("*-issue-65-*"))


@pytest.mark.parametrize(
    ("body", "warns"),
    [
        ("Queued after #66 (same component).", True),
        ("Depends on #66\nQueued after #66 (same component).", False),
        ("Touches the same component as #660.", False),
    ],
)
def test_batch_preflight_warns_on_a_prose_only_dependency(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path, body: str, warns: bool
) -> None:
    bail_all = "printf 'agent-bail: spec-gap\\n' > \"$AGENT_LOOP_HANDOFF_FILE\"; exit 0"
    result = _run(
        consumer,
        ["--issues", "66,67", "--iterations", "2"],
        issues=[_issue(66), _issue(67, body)],
        config=_config_v3(tmp_path, worker_hook=bail_all),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert ("Issue #67 mentions earlier batch issue #66 without 'Depends on #66'" in result.stderr) == warns


def test_every_recovery_message_names_a_known_stop_category() -> None:
    # A stop without a category leaves a supervisor guessing from glyph lines.
    text = AGENT_LOOP.read_text(encoding="utf-8")
    declared = re.search(r'^STOP_CATEGORIES="([^"]+)"', text, re.M)
    assert declared is not None
    categories = set(declared.group(1).split())
    lines = text.splitlines()
    calls = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if (
            "recovery_message " not in line
            or stripped.startswith("#")
            or stripped.startswith("recovery_message()")
        ):
            continue
        match = re.search(r'recovery_message "((?:[^"\\]|\\.)*)"(.*)$', line)
        assert match is not None, f"line {index + 1} passes a non-literal reason: {stripped}"
        rest = match.group(2)
        if rest.strip() == "\\":
            rest = " " + lines[index + 1].strip()
        category = re.match(r"\s+([a-z][a-z0-9/-]*)", rest)
        assert category is not None and category.group(1) in categories, (
            f"line {index + 1} has no known stop category: {stripped}"
        )
        calls += 1
    assert calls > 100


def test_parkable_stop_categories_are_known_and_resumable() -> None:
    text = AGENT_LOOP.read_text(encoding="utf-8")
    stop = re.search(r'^STOP_CATEGORIES="([^"]+)"', text, re.M)
    parkable = re.search(r'^PARKABLE_STOP_CATEGORIES="([^"]+)"', text, re.M)
    assert stop is not None and parkable is not None
    assert set(parkable.group(1).split()) <= set(stop.group(1).split())
    # Resume restores the round cap and the review deadline from run state,
    # so a run parked on either would stop the same way again.
    assert not set(parkable.group(1).split()) & {"review-cap-exhausted", "budget-exhausted"}


_SUPERVISION_TABLE_JQ = r"""
[.[] | select(.issue != null)] | group_by(.issue)[] as $events
| ($events | map(.event)) as $types
| [ ($events[0].issue | tostring),
    (if ($types | index("pr_ready")) then "finalized"
     elif ($types | index("bail")) then "bailed"
     elif ($types | index("parked")) then "parked"
     else "stopped" end),
    ([$events[] | select(.event == "pass_result") | .round] | max // 0 | tostring),
    ([$events[] | select(.event == "phase_end") | .seconds] | add // 0 | tostring),
    ([$events[] | select(.event == "stop") | .category] | last // ""),
    ([$events[] | select(.event == "parked") | .resumeCommand] | last // "")
  ] | @tsv
"""

_EVENT_KEYS = {
    "event", "runTag", "epoch", "issue", "index", "resumed", "round", "runState",
    "phase", "seconds", "exit", "engine", "status", "classification", "before",
    "after", "reason", "category", "resumable", "resumeCommand",
    "hookPhase", "hookLog", "kind", "ref", "pr", "head", "handoffPath",
    "issues", "configSha256", "finalized", "bailed", "parked",
}


def test_batch_event_stream_builds_a_per_issue_supervision_table(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    worker = (
        'if [ "$AGENT_LOOP_ISSUE_ID" = 71 ]; then '
        "printf 'agent-bail: spec-gap\\n' > \"$AGENT_LOOP_HANDOFF_FILE\"; exit 0; fi; "
        + _PER_ISSUE_WORKER
    )
    result = _run(
        consumer,
        ["--issues", "72,70,71", "--iterations", "3"],
        issues=[_issue(72), _issue(70), _issue(71)],
        config=_config_v3(
            tmp_path, worker_hook=worker, claude_review_hook=_ends_early_for(72),
            batch_on_issue_failure="park",
        ),
        timeout=300,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    events_file = next((tmp_path / "logs").glob("*-batch-*-events.jsonl"))
    table = subprocess.run(
        ["jq", "-rs", _SUPERVISION_TABLE_JQ, str(events_file)],
        check=True, capture_output=True, text=True,
    ).stdout
    rows = {row[0]: row for row in (line.split("\t") for line in table.splitlines())}
    assert rows["70"][1:3] == ["finalized", "1"] and int(rows["70"][3]) >= 0 and rows["70"][4] == ""
    assert rows["71"][1] == "bailed"
    assert rows["72"][1] == "parked"
    assert rows["72"][4] == "no-result/hook-ended-early"
    assert "--resume-run" in rows["72"][5]
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    starts = [event for event in events if event["event"] == "batch_start"]
    assert starts[0]["issues"] == [72, 70, 71] and starts[0]["resumed"] is False
    assert any(event["resumed"] for event in starts[1:])
    end = [event for event in events if event["event"] == "batch_end"][-1]
    assert (end["exit"], end["finalized"], end["bailed"], end["parked"]) == (3, [70], [71], [72])
    assert {"retry", "pass_result", "pr_ready", "bail", "parked", "stop"} <= {
        event["event"] for event in events
    }


def test_stop_events_name_the_hook_log_and_carry_no_free_text(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    noisy = (
        "for i in $(seq 1 60); do echo \"progress line $i\"; done; "
        "printf 'x%.0s' $(seq 1 900); echo; exit 0"
    )
    issue = _issue(74, "BODY-SENTINEL private requirement")
    issue["title"] = "TITLE-SENTINEL"
    result = _run(
        consumer,
        ["--issues", "74"],
        issues=[issue],
        config=_config_v3(tmp_path, claude_review_hook=noisy, output_max_lines=10),
        timeout=90,
    )
    assert result.returncode != 0
    events_file = next((tmp_path / "logs").glob("*-run-*-events.jsonl"))
    text = events_file.read_text(encoding="utf-8")
    assert "SENTINEL" not in text
    assert "progress line" not in text
    events = [json.loads(line) for line in text.splitlines()]
    assert all(set(event) <= _EVENT_KEYS for event in events), [
        sorted(set(event) - _EVENT_KEYS) for event in events
    ]
    stop = [event for event in events if event["event"] == "stop"][-1]
    assert stop["category"] == "no-result/hook-ended-early"
    assert stop["resumable"] is True and "--resume-run" in stop["resumeCommand"]
    assert "progress line 60" in Path(stop["hookLog"]).read_text(encoding="utf-8")
    assert stop["hookPhase"].startswith("configured Claude review hook")


def test_budget_spent_before_post_pass_validation_stops_as_budget_exhausted(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The pass commits after using most of the budget, so its validation finds
    # less than the per-pass floor left. Resume restores that deadline, so a
    # parkable validation-red here would park an issue that cannot resume.
    codex = "sleep 12; " + _minor_committed_v3_hook("codex")
    result = _run(
        consumer,
        ["--issues", "76"],
        issues=[_issue(76)],
        config=_config_v3(
            tmp_path,
            codex_review_hook=codex,
            review_timeout_seconds=130,
            hook_timeout_seconds=60,
        ),
        timeout=120,
    )
    assert result.returncode != 0
    assert "Stop category: budget-exhausted" in result.stderr, result.stderr
    assert "validation-red" not in result.stderr


def test_a_later_issue_stop_does_not_name_the_previous_issue_run_state(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    validation = (
        'if [ "$AGENT_LOOP_ISSUE_ID" = 78 ]; then exit 1; fi; '
        "printf 'validate\\n' >> \"$EVENT_LOG\""
    )
    result = _run(
        consumer,
        ["--issues", "77,78", "--iterations", "2"],
        issues=[_issue(77), _issue(78)],
        config=_config_v3(tmp_path, worker_hook=_PER_ISSUE_WORKER, validation_hook=validation),
        timeout=180,
    )
    assert result.returncode != 0
    events_file = next((tmp_path / "logs").glob("*-batch-*-events.jsonl"))
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    assert [event["issue"] for event in events if event["event"] == "pr_ready"] == [77]
    stop = [event for event in events if event["event"] == "stop"][-1]
    assert (stop["issue"], stop["category"]) == (78, "validation-red")
    assert "--resume-batch" in stop["resumeCommand"]
    assert "--resume-run" not in stop["resumeCommand"]
    assert "Resume review with" not in result.stderr


def test_resumed_run_names_its_issue_and_emits_a_resumed_start(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    fail_marker = consumer[3] / "fail-claude-review"
    fail_marker.touch()
    claude_hook = (
        'if [ -e "$AGENT_STATE_DIR/fail-claude-review" ]; then exit 71; fi; '
        + _clean_v3_hook("claude")
    )
    config = _config_v3(tmp_path, claude_review_hook=claude_hook)
    first = _run(consumer, ["--issues", "75"], issues=[_issue(75)], config=config, timeout=60)
    assert first.returncode != 0
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    fail_marker.unlink()
    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(75, assigned=True)],
        config=config,
        timeout=60,
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "Issue #75 (resumed, round 1)" in resumed.stdout
    events = [
        json.loads(line)
        for line in (state_file.parent / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    start = next(event for event in events if event["event"] == "issue_start")
    assert (start["issue"], start["resumed"], start["round"]) == (75, True, 1)
    assert any(event["event"] == "pr_ready" and event["issue"] == 75 for event in events)


def test_event_stream_refuses_to_follow_a_symlink(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    fail_marker = consumer[3] / "fail-claude-review"
    fail_marker.touch()
    claude_hook = (
        'if [ -e "$AGENT_STATE_DIR/fail-claude-review" ]; then exit 71; fi; '
        + _clean_v3_hook("claude")
    )
    config = _config_v3(tmp_path, claude_review_hook=claude_hook)
    first = _run(consumer, ["--issues", "76"], issues=[_issue(76)], config=config, timeout=60)
    assert first.returncode != 0
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    fail_marker.unlink()
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    (state_file.parent / "events.jsonl").symlink_to(target)
    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(76, assigned=True)],
        config=config,
        timeout=60,
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "event stream is not a private regular file" in resumed.stderr
    assert target.read_text(encoding="utf-8") == ""


def test_batch_iteration_cap_pauses_with_durable_cursor(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "72,73", "--iterations", "1"],
        issues=[_issue(72), _issue(73)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook=_clean_v3_hook("codex"),
            claude_review_hook=_clean_v3_hook("claude"),
        ),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "paused cleanly at the 1-issue iteration cap" in result.stdout
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert batch["cursor"] == 1
    assert [row["status"] for row in batch["issues"]] == ["finalized", "pending"]


def test_batch_cursor_issue_cannot_skip_to_a_later_ready_issue(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    batch_file = tmp_path / "logs/manual-batch.json"
    helper = consumer[0] / ".claude/skills/agent-loop/scripts/agent-loop-state.py"
    created = subprocess.run(
        [
            "python3",
            str(helper),
            "batch-create",
            "--file",
            str(batch_file),
            "--run-id",
            "manual",
            "--repo",
            "fixture/consumer",
            "--base-branch",
            "main",
            "--issues",
            "74,75",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    result = _run(
        consumer,
        ["--resume-batch", str(batch_file)],
        issues=[_issue(74), _issue(75)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook=_clean_v3_hook("codex"),
            claude_review_hook=_clean_v3_hook("claude"),
        ),
        extra_env={"AGENT_READY_JSON": json.dumps([_issue(75)])},
    )
    assert result.returncode != 0
    assert "Ordered batch cursor issue #74 is not ready" in result.stderr
    assert "--expected-status 'pending' --status bailed" in result.stderr
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert batch["cursor"] == 0
    assert [row["status"] for row in batch["issues"]] == ["pending", "pending"]


def test_batch_resume_rejects_contract_v2_before_state_mutation(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    batch_file = tmp_path / "logs/contract-drift-batch.json"
    helper = consumer[0] / ".claude/skills/agent-loop/scripts/agent-loop-state.py"
    created = subprocess.run(
        [
            "python3",
            str(helper),
            "batch-create",
            "--file",
            str(batch_file),
            "--run-id",
            "contract-drift",
            "--repo",
            "fixture/consumer",
            "--base-branch",
            "main",
            "--issues",
            "74,75",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    before = batch_file.read_bytes()

    result = _run(
        consumer,
        ["--resume-batch", str(batch_file)],
        issues=[_issue(74), _issue(75)],
        config=_config(tmp_path, review_contract_version=2),
    )

    assert result.returncode != 0
    assert "--resume-batch requires review_contract_version = 3" in result.stderr
    assert batch_file.read_bytes() == before


def test_batch_checkpoints_before_failed_worktree_cleanup(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    _write_executable(
        consumer[2] / "git",
        """#!/usr/bin/env bash
if [ "$1" = worktree ] && [ "$2" = remove ] && [ ! -e "$AGENT_STATE_DIR/cleanup-failed" ]; then
    touch "$AGENT_STATE_DIR/cleanup-failed"
    exit 75
fi
exec "$AGENT_TEST_REAL_GIT" "$@"
""",
    )
    result = _run(
        consumer,
        ["--issues", "76,77", "--iterations", "1"],
        issues=[_issue(76), _issue(77)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook=_clean_v3_hook("codex"),
            claude_review_hook=_clean_v3_hook("claude"),
        ),
        extra_env={"AGENT_TEST_REAL_GIT": real_git},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "cleanup failed and was preserved" in result.stderr
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert batch["cursor"] == 1
    assert batch["issues"][0]["status"] == "finalized"
    child_state = Path(batch["issues"][0]["childRunState"])
    child = json.loads(child_state.read_text(encoding="utf-8"))
    assert child["phase"] == "finalized"
    assert Path(child["worktree"]).exists()


def test_v3_cleanup_only_minor_transition_requires_ledger_evidence(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "32"],
        issues=[_issue(32)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook=_cleanup_v3_hook("codex"),
            claude_review_hook=_clean_v3_hook("claude"),
        ),
    )
    assert result.returncode != 0
    assert "valid contract v3 result" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_v3_finalization_reuses_sealed_pseudo_v3_history(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    historical_threads = [
        {
            "id": "PSEUDO-THREAD",
            "isResolved": True,
            "repository": {"nameWithOwner": "fixture/consumer"},
            "pullRequest": {"number": 1},
            "comments": {
                "nodes": [
                    {
                        "body": (
                            "Historical finding.\n\n"
                            "<!-- local-review:v3 engine=claude "
                            "fingerprint=historical-finding -->"
                        ),
                        "databaseId": 101,
                        "author": {"login": "tester"},
                    },
                    {
                        "body": "Fixed before contract v3 was finalized.",
                        "databaseId": 102,
                        "author": {"login": "tester"},
                    },
                ],
                "pageInfo": {"hasNextPage": False},
            },
        }
    ]

    result = _run(
        consumer,
        ["--issues", "92"],
        issues=[_issue(92)],
        config=_config_v3(tmp_path),
        extra_env={
            "AGENT_REVIEW_THREADS_JSON": json.dumps(historical_threads),
            "AGENT_PROJECT_REVIEW_THREAD_TOPOLOGY": "1",
        },
        timeout=60,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    assert json.loads(state_file.read_text(encoding="utf-8"))["phase"] == "finalized"


def test_v3_converged_recovery_uses_each_engine_result_digest(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    fail_marker = consumer[3] / "fail-final-validation"
    fail_marker.touch()
    validation = (
        'if [ -e "$AGENT_STATE_DIR/fail-final-validation" ] && '
        '[ -e "$AGENT_LOOP_LOG_DIR/final-reviewed-head-validation.log" ]; '
        "then exit 71; fi"
    )
    config = _config_v3(tmp_path, validation_hook=validation)

    first = _run(
        consumer,
        ["--issues", "93"],
        issues=[_issue(93)],
        config=config,
        timeout=60,
    )
    assert first.returncode != 0
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["phase"] == "converged"
    assert state["codexResultSha256"] != state["claudeResultSha256"]

    fail_marker.unlink()
    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(93, assigned=True)],
        config=config,
        timeout=60,
    )

    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "Recovered converged review checkpoint" in resumed.stdout


def test_v3_final_round_changed_pass_resumes_without_reusing_identity(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    fail_marker = consumer[3] / "fail-codex-review-validation"
    fail_marker.touch()
    validation = (
        'if [ -e "$AGENT_STATE_DIR/fail-codex-review-validation" ] && '
        '[ -e "$AGENT_LOOP_LOG_DIR/codex-review-round-1-validation.log" ]; '
        "then exit 71; fi"
    )
    material_hook = (
        "printf 'codex-material\\n' >> \"$EVENT_LOG\"; "
        'before="$AGENT_LOOP_PR_HEAD_SHA"; '
        "printf 'material fix\\n' > final-round-fix.txt; "
        "git add final-round-fix.txt; git commit -m 'fix: final round finding'; "
        '"$AGENT_LOOP_REVIEW_PUSH_HELPER"; after=$(git rev-parse HEAD); '
        "printf -v finding '%s\\n%s' \"<!-- local-review:v3 engine=codex "
        "round=$AGENT_LOOP_REVIEW_ROUND head=$before fingerprint=final-round-material "
        "occurrence=1 severity=major lens=recovery "
        "content-sha256=4be82179d3761dd716ff1e62c19138fc105495b9a66528678e0e76e253adb577 -->\" 'Finding.'; "
        "printf -v disposition '%s\\n%s' \"<!-- local-review-disposition:v3 "
        "engine=codex round=$AGENT_LOOP_REVIEW_ROUND head=$after "
        "fingerprint=final-round-material occurrence=1 outcome=fixed "
        "content-sha256=13079c2612a9ead4818ab21ef90bf6b7c457916144d8cbaeeff74befa4f4cc8d -->\" 'Fixed.'; "
        "jq -n --arg finding \"$finding\" --arg disposition \"$disposition\" "
        "'[{id:\"THREAD-FINAL\",isResolved:true,"
        "repository:{nameWithOwner:\"fixture/consumer\"},pullRequest:{number:1},"
        "comments:{nodes:[{body:$finding,databaseId:1,author:{login:\"tester\"}},"
        "{body:$disposition,databaseId:2,author:{login:\"tester\"}}],"
        "pageInfo:{hasNextPage:false}}}]' > \"$AGENT_STATE_DIR/review-threads.json\"; "
        "jq -n --argjson round \"$AGENT_LOOP_REVIEW_ROUND\" "
        "--arg base \"$AGENT_LOOP_REVIEW_BASE_SHA\" --arg before \"$before\" "
        "--arg after \"$after\" "
        "'{version:3,status:\"changed\",engine:\"codex\",round:$round,"
        "baseSha:$base,beforeSha:$before,afterSha:$after,classification:\"material\","
        "findingFingerprints:[\"final-round-material\"],finalLaneComplete:true}' "
        '> "$AGENT_LOOP_REVIEW_RESULT_FILE"'
    )
    config = _config_v3(
        tmp_path,
        validation_hook=validation,
        codex_review_hook=material_hook,
        review_max_rounds=1,
    )

    first = _run(
        consumer,
        ["--issues", "94"],
        issues=[_issue(94)],
        config=config,
        timeout=60,
    )
    assert first.returncode != 0
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["phase"] == "reviewing"
    assert state["round"] == 1
    assert state["reviewEngine"] == "codex"

    fail_marker.unlink()
    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(94, assigned=True)],
        config=config,
        timeout=60,
    )

    assert resumed.returncode != 0
    assert "did not converge within 1 round(s)" in resumed.stderr
    assert "resuming its remaining leg" in resumed.stdout
    assert "Recovered authenticated Codex evidence" in resumed.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8")
    assert events.count("codex-material\n") == 1
    assert events.count("claude\n") == 1
    assert "conflicting attestation" not in resumed.stderr


def test_v3_final_round_clean_interruption_resumes_without_exhausting_cap(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    fail_marker = consumer[3] / "fail-claude-review"
    fail_marker.touch()
    # The interruption lands in the Claude leg itself: a clean Codex pass
    # leaves the head unchanged, so no validation runs between the two legs.
    claude_hook = (
        'if [ -e "$AGENT_STATE_DIR/fail-claude-review" ]; then exit 71; fi; '
        + _clean_v3_hook("claude")
    )
    config = _config_v3(tmp_path, claude_review_hook=claude_hook, review_max_rounds=1)

    first = _run(
        consumer,
        ["--issues", "95"],
        issues=[_issue(95)],
        config=config,
        timeout=60,
    )
    assert first.returncode != 0
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    fail_marker.unlink()

    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(95, assigned=True)],
        config=config,
        timeout=60,
    )

    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "resuming its remaining leg" in resumed.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8")
    assert events.count("codex\n") == 1
    assert events.count("claude\n") == 1
    assert json.loads(state_file.read_text(encoding="utf-8"))["phase"] == "finalized"


def test_interrupted_claude_leg_below_the_cap_resumes_in_the_same_round(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Below the round cap, resuming used to start a new round and re-run Codex
    # against the head its round-1 result already covered.
    fail_marker = consumer[3] / "fail-claude-review"
    fail_marker.touch()
    claude_hook = (
        'if [ -e "$AGENT_STATE_DIR/fail-claude-review" ]; then exit 71; fi; '
        + _clean_v3_hook("claude")
    )
    config = _config_v3(tmp_path, claude_review_hook=claude_hook, review_max_rounds=4)

    first = _run(
        consumer, ["--issues", "96"], issues=[_issue(96)], config=config, timeout=60
    )
    assert first.returncode != 0
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert (state["round"], state["reviewEngine"]) == (1, "claude")
    fail_marker.unlink()

    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(96, assigned=True)],
        config=config,
        timeout=60,
    )

    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "resuming its remaining leg in the same round" in resumed.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8")
    assert events.count("codex\n") == 1
    assert events.count("claude\n") == 1
    comments = (consumer[3] / "pr-comments.log").read_text(encoding="utf-8")
    assert "local-review-pass:v3 engine=claude round=1" in comments
    assert "round=2" not in comments
    assert json.loads(state_file.read_text(encoding="utf-8"))["phase"] == "finalized"


def test_interrupted_claude_leg_restarts_at_codex_when_the_base_advanced(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The same-round shortcut relies on the Codex result covering the head the
    # Claude leg will read. Integrating an advanced base moves that head.
    fail_marker = consumer[3] / "fail-claude-review"
    fail_marker.touch()
    claude_hook = (
        'if [ -e "$AGENT_STATE_DIR/fail-claude-review" ]; then exit 71; fi; '
        + _clean_v3_hook("claude")
    )
    config = _config_v3(tmp_path, claude_review_hook=claude_hook, review_max_rounds=4)
    first = _run(
        consumer, ["--issues", "97"], issues=[_issue(97)], config=config, timeout=60
    )
    assert first.returncode != 0
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    fail_marker.unlink()

    clone = tmp_path / "base-advance"
    _run_git("clone", str(consumer[1]), str(clone))
    _run_git("config", "user.name", "Test", cwd=clone)
    _run_git("config", "user.email", "test@example.invalid", cwd=clone)
    _run_git("config", "commit.gpgsign", "false", cwd=clone)
    (clone / "advanced-base.txt").write_text("advanced\n", encoding="utf-8")
    _run_git("add", "advanced-base.txt", cwd=clone)
    _run_git("commit", "-m", "chore: advance base", cwd=clone)
    _run_git("push", "origin", "main", cwd=clone)

    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(97, assigned=True)],
        config=config,
        timeout=90,
    )

    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "Base advanced since the checkpoint" in resumed.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8")
    assert events.count("codex\n") == 2
    assert events.count("claude\n") == 1
    comments = (consumer[3] / "pr-comments.log").read_text(encoding="utf-8")
    assert "local-review-pass:v3 engine=codex round=2" in comments
    assert "local-review-pass:v3 engine=claude round=2" in comments


_STRANDING_CLAUDE_HOOK = (
    # First run: commit a fix and stop before publishing it, as a pass killed
    # mid-push does. Later runs review cleanly.
    'if [ -e "$AGENT_STATE_DIR/strand-claude" ]; then '
    "printf 'fix\\n' > stranded-fix.txt; git add stranded-fix.txt; "
    "git commit -m 'test: stranded fix'; exit 0; fi; "
    + _clean_v3_hook("claude")
)


def _strand_a_claude_commit(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path, issue: int
) -> tuple[Path, dict[str, object], str, str]:
    (consumer[3] / "strand-claude").touch()
    config = _config_v3(tmp_path, claude_review_hook=_STRANDING_CLAUDE_HOOK)
    first = _run(consumer, ["--issues", str(issue)], issues=[_issue(issue)], config=config, timeout=60)
    assert first.returncode != 0
    assert "push checkpoint did not match its final head" in first.stderr
    (consumer[3] / "strand-claude").unlink()
    state_file = next((tmp_path / "logs").glob("*/run-state.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    stranded = _run_git("rev-parse", "HEAD", cwd=Path(str(state["worktree"]))).stdout.strip()
    assert stranded != state["headSha"]
    return state_file, state, stranded, config


def test_resume_keeps_stranded_review_commits_on_a_rescue_ref_and_replays_the_pass(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    state_file, state, stranded, config = _strand_a_claude_commit(consumer, tmp_path, 98)
    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(98, assigned=True)],
        config=config,
        timeout=90,
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "recovered: stranded-review-commits -> refs/agent-loop/rescue/" in resumed.stdout
    assert "resuming its remaining leg in the same round" in resumed.stdout
    ref = f"refs/agent-loop/rescue/{state['runId']}/claude-r1"
    # The worktree was removed after publication; the ref lives in the shared
    # repository and still holds the stranded commit.
    assert not Path(str(state["worktree"])).exists()
    assert _run_git("rev-parse", ref, cwd=consumer[0]).stdout.strip() == stranded
    assert f"Rescued review commits kept at {ref}" in resumed.stdout
    final = json.loads(state_file.read_text(encoding="utf-8"))
    assert final["phase"] == "finalized"
    assert final["headSha"] == state["headSha"]


@pytest.mark.parametrize("divergence", ["remote-ahead", "ledger-names-stranded-commit"])
def test_resume_refuses_stranded_commits_it_cannot_prove_unpublished(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path, divergence: str
) -> None:
    state_file, state, stranded, config = _strand_a_claude_commit(consumer, tmp_path, 99)
    if divergence == "remote-ahead":
        clone = tmp_path / "branch-clone"
        _run_git("clone", "--branch", str(state["branch"]), str(consumer[1]), str(clone))
        _run_git("config", "user.name", "Test", cwd=clone)
        _run_git("config", "user.email", "test@example.invalid", cwd=clone)
        _run_git("config", "commit.gpgsign", "false", cwd=clone)
        (clone / "elsewhere.txt").write_text("elsewhere\n", encoding="utf-8")
        _run_git("add", "elsewhere.txt", cwd=clone)
        _run_git("commit", "-m", "chore: pushed elsewhere", cwd=clone)
        _run_git("push", "origin", f"HEAD:refs/heads/{state['branch']}", cwd=clone)
        expected = "Draft PR state does not match the recovery checkpoint"
    else:
        threads = [{
            "id": "THREAD-STRANDED",
            "isResolved": True,
            "repository": {"nameWithOwner": "fixture/consumer"},
            "pullRequest": {"number": 1},
            "comments": {
                "nodes": [{"body": f"Fixed in `{stranded[:9]}`.", "databaseId": 5}],
                "pageInfo": {"hasNextPage": False},
            },
        }]
        (consumer[3] / "review-threads.json").write_text(json.dumps(threads), encoding="utf-8")
        expected = "could not be recovered safely"
    resumed = _run(
        consumer,
        ["--resume-run", str(state_file)],
        issues=[_issue(99, assigned=True)],
        config=config,
        timeout=60,
    )
    assert resumed.returncode != 0
    assert expected in resumed.stderr
    worktree = Path(str(state["worktree"]))
    assert _run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip() == stranded
    refs = _run_git("for-each-ref", "refs/agent-loop/rescue", cwd=consumer[0]).stdout
    assert refs == ""


def test_park_mode_parks_a_stranded_review_commit(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    claude = (
        'if [ "$AGENT_LOOP_ISSUE_ID" = 57 ]; then '
        "printf 'fix\\n' > stranded-fix.txt; git add stranded-fix.txt; "
        "git commit -m 'test: stranded fix'; exit 0; fi; "
        + _clean_v3_hook("claude")
    )
    result = _run(
        consumer,
        ["--issues", "57,58", "--iterations", "2"],
        issues=[_issue(57), _issue(58)],
        config=_config_v3(tmp_path, claude_review_hook=claude, batch_on_issue_failure="park"),
        timeout=180,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["parked", "finalized"]
    assert batch["issues"][0]["stopCategory"] == "push-checkpoint-mismatch"


def test_run_records_wrapper_pid_and_phase_timing(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Every duration in the first consumer reports had to be reconstructed from
    # log mtimes, and the only liveness check available from outside was a
    # `pgrep -f` that matched the monitor's own shell.
    repo = consumer[0]
    (repo / ".claude/skills/agent-loop/agent-loop.config").write_text(
        _config_v3(tmp_path), encoding="utf-8"
    )
    proc = subprocess.Popen(
        [str(repo / ".claude/skills/agent-loop/scripts/agent-loop.sh"), "--issues", "22"],
        cwd=repo,
        env=_environment(consumer, [_issue(22)]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(timeout=90)
    assert proc.returncode == 0, stderr + stdout
    log_dir = next((tmp_path / "logs").glob("*-issue-22-*"))
    assert (log_dir / "wrapper.pid").read_text(encoding="utf-8").strip() == str(proc.pid)
    events = [
        json.loads(line)
        for line in (log_dir / "phases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    phases = [(event["event"], event["phase"]) for event in events]
    assert phases.index(("start", "worker attempt 1")) < phases.index(("end", "worker attempt 1"))
    assert ("end", "worker validation") in phases
    assert ("skipped", "initial-fresh-base validation") in phases
    assert ("end", "final-reviewed-head validation") in phases
    ends = [event for event in events if event["event"] == "end"]
    assert ends and all(
        event["exit"] == 0 and event["seconds"] >= 0 and event["epoch"] > 0 for event in ends
    )


def test_draft_pr_title_is_the_worker_commit_subject(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Consumers that merge with merge commits get the PR title as the merge
    # subject; the generic "agent-loop: resolve #N" landed in history where a
    # conventional subject was expected. The fixture worker commits
    # "fix: worker", and a control character in a subject must not reach gh.
    worker = (
        "printf 'worker\\n' >> \"$EVENT_LOG\"; printf done > result.txt; "
        "git add result.txt; git commit -m \"$(printf 'feat(demo): add\\tresult\\x01 file')\""
    )
    result = _run(
        consumer,
        ["--issues", "21"],
        issues=[_issue(21)],
        config=_config_v3(tmp_path, worker_hook=worker),
        timeout=90,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    create = next(line for line in gh_log.splitlines() if line.startswith("pr create"))
    assert "--title feat(demo): addresult file --body-file" in create
    assert "agent-loop: resolve" not in create


def test_unchanged_head_is_validated_once_then_only_at_the_final_gate(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # One converged run used to validate the same head three times: worker,
    # "Already up to date" base integration, and after each clean pass, then
    # again at the final gate. Only the worker validation and the final gate
    # should run the hook when nothing moved.
    result = _run(
        consumer,
        ["--issues", "18"],
        issues=[_issue(18)],
        config=_config_v3(tmp_path),
        timeout=90,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8")
    assert events.count("validate\n") == 2
    assert result.stdout.count("validation skipped") == 3
    assert "final-reviewed-head validation skipped" not in result.stdout


def test_wrapper_validation_is_recorded_as_the_gating_run_for_the_reviewed_head(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Under agent-loop the engines run focused checks and the wrapper's
    # validation hook is the gating run. Its evidence must name the command
    # and the exact head it ran on, including the head that is marked ready.
    result = _run(
        consumer,
        ["--issues", "23"],
        issues=[_issue(23)],
        config=_config_v3(tmp_path),
        timeout=90,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    log_dir = next((tmp_path / "logs").glob("*-issue-23-*"))
    records = [
        json.loads(line)
        for line in (log_dir / "validation.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    final_head = json.loads((log_dir / "run-state.json").read_text(encoding="utf-8"))["headSha"]
    by_label = {record["label"]: record for record in records}
    assert by_label["final-reviewed-head"]["head"] == final_head
    assert by_label["final-reviewed-head"]["outcome"] == "passed"
    assert by_label["codex-review-round-1"]["outcome"] == "reused"
    assert by_label["codex-review-round-1"]["head"] == final_head
    assert all(record["command"] == "validation_hook" for record in records)
    assert len({record["commandSha256"] for record in records}) == 1
    body = (log_dir / "pr-body-final.md").read_text(encoding="utf-8")
    assert f"validation hook passed on reviewed head `{final_head}`" in body


def test_red_post_pass_validation_blocks_convergence(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    validation = (
        'if [ -e "$AGENT_LOOP_LOG_DIR/codex-review-round-1.result.json" ]; then exit 71; fi'
    )
    result = _run(
        consumer,
        ["--issues", "24"],
        issues=[_issue(24)],
        config=_config_v3(
            tmp_path,
            validation_hook=validation,
            codex_review_hook=_minor_committed_v3_hook("codex"),
        ),
        timeout=90,
    )
    assert result.returncode != 0
    assert "Validation after the configured Codex review hook failed in review round 1" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()
    log_dir = next((tmp_path / "logs").glob("*-issue-24-*"))
    records = [
        json.loads(line)
        for line in (log_dir / "validation.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["label"] == "codex-review-round-1"
    assert (records[-1]["outcome"], records[-1]["exit"]) == ("failed", 71)


def _minor_committed_v3_hook(engine: str) -> str:
    # A changed pass with complete ledger evidence: one finding, fixed in a
    # committed cleanup, classified minor so the round still converges.
    return (
        f"printf '{engine}\\n' >> \"$EVENT_LOG\"; "
        'before="$AGENT_LOOP_PR_HEAD_SHA"; '
        f"printf 'minor fix\\n' > minor-fix-{engine}.txt; "
        f"git add minor-fix-{engine}.txt; git commit -m 'fix: minor finding'; "
        '"$AGENT_LOOP_REVIEW_PUSH_HELPER"; after=$(git rev-parse HEAD); '
        f"printf -v finding '%s\\n%s' \"<!-- local-review:v3 engine={engine} "
        "round=$AGENT_LOOP_REVIEW_ROUND head=$before fingerprint=minor-finding "
        "occurrence=1 severity=minor lens=recovery "
        "content-sha256=4be82179d3761dd716ff1e62c19138fc105495b9a66528678e0e76e253adb577 -->\" 'Finding.'; "
        f"printf -v disposition '%s\\n%s' \"<!-- local-review-disposition:v3 "
        f"engine={engine} round=$AGENT_LOOP_REVIEW_ROUND head=$after "
        "fingerprint=minor-finding occurrence=1 outcome=fixed "
        "content-sha256=13079c2612a9ead4818ab21ef90bf6b7c457916144d8cbaeeff74befa4f4cc8d -->\" 'Fixed.'; "
        "jq -n --arg finding \"$finding\" --arg disposition \"$disposition\" "
        "'[{id:\"THREAD-MINOR\",isResolved:true,"
        "repository:{nameWithOwner:\"fixture/consumer\"},pullRequest:{number:1},"
        "comments:{nodes:[{body:$finding,databaseId:1,author:{login:\"tester\"}},"
        "{body:$disposition,databaseId:2,author:{login:\"tester\"}}],"
        "pageInfo:{hasNextPage:false}}}]' > \"$AGENT_STATE_DIR/review-threads.json\"; "
        "jq -n --argjson round \"$AGENT_LOOP_REVIEW_ROUND\" "
        "--arg base \"$AGENT_LOOP_REVIEW_BASE_SHA\" --arg before \"$before\" "
        "--arg after \"$after\" "
        f"'{{version:3,status:\"changed\",engine:\"{engine}\",round:$round,"
        "baseSha:$base,beforeSha:$before,afterSha:$after,classification:\"minor\","
        "findingFingerprints:[\"minor-finding\"],finalLaneComplete:true}' "
        '> "$AGENT_LOOP_REVIEW_RESULT_FILE"'
    )


def test_changed_review_head_is_revalidated(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # A pass that commits moves the head, and the new head must be validated
    # before the next leg reads it; the clean Claude pass after it is skipped.
    result = _run(
        consumer,
        ["--issues", "19"],
        issues=[_issue(19)],
        config=_config_v3(tmp_path, codex_review_hook=_minor_committed_v3_hook("codex")),
        timeout=90,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8")
    assert events.count("validate\n") == 3
    assert "codex-review-round-1 validation skipped" not in result.stdout
    assert "claude-review-round-1 validation skipped" in result.stdout


def test_v3_review_hook_cannot_self_authorize_direct_push(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    hook = (
        "if AGENT_LOOP_SAFE_REVIEW_PUSH=1 git push origin "
        '"HEAD:refs/heads/$AGENT_LOOP_BRANCH" '
        '2> "$AGENT_STATE_DIR/direct-v3-push.stderr"; then exit 89; fi; '
        + _clean_v3_hook("codex")
    )
    result = _run(
        consumer,
        ["--issues", "91"],
        issues=[_issue(91)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook=hook,
            claude_review_hook=_clean_v3_hook("claude"),
        ),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    rejection = (consumer[3] / "direct-v3-push.stderr").read_text(encoding="utf-8")
    assert "contract-v3 review hooks must publish" in rejection


def test_v3_rejects_github_actor_drift_after_review_hook(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    drifting_hook = (
        "printf 'other-user' > \"$AGENT_STATE_DIR/gh-actor\"; "
        + _clean_v3_hook("codex")
    )
    result = _run(
        consumer,
        ["--issues", "33"],
        issues=[_issue(33)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook=drifting_hook,
            claude_review_hook=_clean_v3_hook("claude"),
        ),
    )
    assert result.returncode != 0
    assert "authenticated GitHub actor changed" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_v3_material_fix_attests_and_forces_another_round(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    hook = tmp_path / "material-v3-hook.py"
    _write_executable(
        hook,
        """#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import subprocess

before = os.environ["AGENT_LOOP_PR_HEAD_SHA"]
Path("material-fix.txt").write_text("fixed\\n", encoding="utf-8")
subprocess.run(["git", "add", "material-fix.txt"], check=True)
subprocess.run(["git", "commit", "-m", "fix: material review finding"], check=True)
subprocess.run([os.environ["AGENT_LOOP_REVIEW_PUSH_HELPER"]], check=True)
after = subprocess.run(
    ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
).stdout.strip()

finding_content = "Material review finding."
disposition_content = f"Fixed in {after}. Validation: fixture."
finding_hash = hashlib.sha256(finding_content.encode()).hexdigest()
disposition_hash = hashlib.sha256(disposition_content.encode()).hexdigest()
finding = (
    f"<!-- local-review:v3 engine=codex round={os.environ['AGENT_LOOP_REVIEW_ROUND']} "
    f"head={before} fingerprint=material-fix occurrence=1 severity=major "
    f"lens=correctness content-sha256={finding_hash} -->\\n{finding_content}"
)
disposition = (
    f"<!-- local-review-disposition:v3 engine=codex "
    f"round={os.environ['AGENT_LOOP_REVIEW_ROUND']} head={after} "
    f"fingerprint=material-fix occurrence=1 outcome=fixed "
    f"content-sha256={disposition_hash} -->\\n{disposition_content}"
)
thread = {
    "id": "THREAD-MATERIAL",
    "isResolved": True,
    "repository": {"nameWithOwner": "fixture/consumer"},
    "pullRequest": {"number": 1},
    "comments": {
        "nodes": [
            {"databaseId": 88, "body": finding, "author": {"login": "tester"}},
            {"databaseId": 89, "body": disposition, "author": {"login": "tester"}},
        ],
        "pageInfo": {"hasNextPage": False},
    },
}
state = Path(os.environ["AGENT_STATE_DIR"])
(state / "review-threads.json").write_text(json.dumps([thread]), encoding="utf-8")
with (state / "events.log").open("a", encoding="utf-8") as handle:
    handle.write("codex-material\\n")
result = {
    "version": 3,
    "status": "changed",
    "engine": "codex",
    "round": int(os.environ["AGENT_LOOP_REVIEW_ROUND"]),
    "baseSha": os.environ["AGENT_LOOP_REVIEW_BASE_SHA"],
    "beforeSha": before,
    "afterSha": after,
    "classification": "material",
    "findingFingerprints": ["material-fix"],
    "finalLaneComplete": True,
}
Path(os.environ["AGENT_LOOP_REVIEW_RESULT_FILE"]).write_text(
    json.dumps(result), encoding="utf-8"
)
""",
    )
    result = _run(
        consumer,
        ["--issues", "34"],
        issues=[_issue(34)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            review_max_rounds=1,
            codex_review_hook=str(hook),
            claude_review_hook=_clean_v3_hook("claude"),
        ),
    )
    assert result.returncode != 0
    assert "did not converge within 1 round(s)" in result.stderr, (
        result.stderr + result.stdout
    )
    assert not (consumer[3] / "pr-ready").exists()
    comments = (consumer[3] / "pr-comments.log").read_text(encoding="utf-8")
    assert "local-review-complete:v3 engine=codex round=1" in comments


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
        "Review order: configured Codex hook -> configured Claude hook" in result.stdout
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
    # Worker validation, then the two clean passes without re-validating the
    # head they did not move, then the final gate. The "Already up to date"
    # base integration and both clean passes reuse the worker validation.
    expected = [
        "setup",
        "worker",
        "validate",
        "codex",
        "claude",
        "validate",
    ]
    assert events == expected * 2
    remote_branches = _run_git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent-loop",
        cwd=consumer[1],
    ).stdout
    assert "issue-4" in remote_branches
    assert "issue-5" in remote_branches


def test_marked_review_thread_requires_reply_and_resolution(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    thread = {
        "isResolved": False,
        "comments": {
            "nodes": [
                {
                    "body": (
                        "<!-- local-review:v1 engine=codex round=1 "
                        "head=abc fingerprint=fixture -->\nFinding"
                    ),
                    "databaseId": 1,
                }
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }
    result = _run(
        consumer,
        ["--issues", "41"],
        issues=[_issue(41)],
        config=_config(tmp_path),
        extra_env={"AGENT_REVIEW_THREADS_JSON": json.dumps([thread])},
    )
    assert result.returncode != 0
    assert "disposition reply and be resolved" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()
    remote_branches = _run_git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent-loop",
        cwd=consumer[1],
    ).stdout
    assert "issue-41" in remote_branches


def test_silent_review_pass_without_attestation_is_not_convergence(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # A hook that exits 0 without committing or posting anything is exactly what a
    # misconfigured or silently declining reviewer looks like. Convergence must
    # not be satisfied by an empty thread list plus a successful exit code.
    result = _run(
        consumer,
        ["--issues", "43"],
        issues=[_issue(43)],
        config=_config(
            tmp_path, codex_review_hook="printf 'codex\\n' >> \"$EVENT_LOG\""
        ),
    )
    assert result.returncode != 0
    assert "did not publish the required clean-pass attestation" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()
    assert (consumer[3] / "pr-branch").exists()


def test_attestation_must_match_the_reviewed_head(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    stale = (
        "printf 'codex\\n' >> \"$EVENT_LOG\"; "
        "gh api repos/{owner}/{repo}/issues/1/comments -X POST "
        '-f body="<!-- local-review-pass:v1 engine=codex '
        'round=$AGENT_LOOP_REVIEW_ROUND head=0000000000000000000000000000000000000000 -->"'
    )
    result = _run(
        consumer,
        ["--issues", "44"],
        issues=[_issue(44)],
        config=_config(tmp_path, codex_review_hook=stale),
    )
    assert result.returncode != 0
    assert "did not publish the required clean-pass attestation" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_clean_pass_attestation_must_be_from_local_reviewer(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "49"],
        issues=[_issue(49)],
        config=_config(tmp_path),
        extra_env={"AGENT_COMMENT_AUTHOR": "untrusted-user"},
    )
    assert result.returncode != 0
    assert "did not publish the required clean-pass attestation" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_committed_review_requires_ledger_and_final_lane_evidence(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    hook = (
        "printf changed > silent-fix.txt; git add silent-fix.txt; "
        "git commit -m 'fix: silent review edit'; "
        "after=$(git rev-parse HEAD); "
        '"$AGENT_LOOP_REVIEW_PUSH_HELPER"; '
        "printf 'minor\\n' > \"$AGENT_LOOP_REVIEW_OUTCOME_FILE\"; "
        "gh api repos/{owner}/{repo}/issues/1/comments -X POST "
        '-f body="<!-- local-review-complete:v1 engine=codex '
        "round=$AGENT_LOOP_REVIEW_ROUND before=$AGENT_LOOP_PR_HEAD_SHA "
        'head=$after -->"'
    )
    result = _run(
        consumer,
        ["--issues", "52"],
        issues=[_issue(52)],
        config=_config(tmp_path, codex_review_hook=hook),
    )
    assert result.returncode != 0
    assert "committed without a resolved same-round finding" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_committed_review_with_structured_evidence_can_converge(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "53"],
        issues=[_issue(53)],
        config=_config(
            tmp_path,
            codex_review_hook=_committed_review_hook("codex"),
        ),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert (consumer[3] / "pr-ready").exists()


@pytest.mark.parametrize(
    ("extra_env", "issue"),
    [
        ({"AGENT_PR_BASE_REF_NAME": "release"}, 54),
        ({"AGENT_PR_IS_DRAFT": "false"}, 55),
    ],
)
def test_review_requires_immutable_open_draft_pr_boundary(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    extra_env: dict[str, str],
    issue: int,
) -> None:
    result = _run(
        consumer,
        ["--issues", str(issue)],
        issues=[_issue(issue)],
        config=_config(tmp_path),
        extra_env=extra_env,
    )
    assert result.returncode != 0
    assert "PR identity, state, head, or base branch changed" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_review_hooks_receive_same_literal_base_sha(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "50"],
        issues=[_issue(50)],
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    bases = (consumer[3] / "review-bases.log").read_text(encoding="utf-8").splitlines()
    assert len(bases) == 2
    assert bases[0] == bases[1]
    assert re.fullmatch(r"[0-9a-f]{40}", bases[0])


def test_failed_ledger_fetch_is_not_a_verified_ledger(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The thread query exits nonzero after emitting a parseable but incomplete
    # page. Without an explicit status check the jq predicate would accept it.
    result = _run(
        consumer,
        ["--issues", "45"],
        issues=[_issue(45)],
        config=_config(tmp_path),
        extra_env={"AGENT_GRAPHQL_PARTIAL": "1"},
    )
    assert result.returncode != 0
    assert "could not load PR review threads" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_resolved_thread_needs_a_reply_after_its_latest_marker(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # A reused fingerprint thread already holding a finding and its reply; a new
    # marker is appended last and the thread resolved with no disposition for it.
    thread = {
        "isResolved": True,
        "comments": {
            "nodes": [
                {
                    "body": "<!-- local-review:v1 engine=codex round=1 head=abc fingerprint=fixture -->\nFirst",
                    "databaseId": 1,
                },
                {"body": "Fixed in deadbeef; validation passed.", "databaseId": 2},
                {
                    "body": "<!-- local-review:v1 engine=codex round=2 head=def fingerprint=fixture -->\nSecond",
                    "databaseId": 3,
                },
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }
    result = _run(
        consumer,
        ["--issues", "46"],
        issues=[_issue(46)],
        config=_config(tmp_path),
        extra_env={"AGENT_REVIEW_THREADS_JSON": json.dumps([thread])},
    )
    assert result.returncode != 0
    assert "disposition reply and be resolved" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_resolved_thread_needs_reply_from_local_reviewer(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    thread = {
        "isResolved": True,
        "comments": {
            "nodes": [
                {
                    "body": "<!-- local-review:v1 engine=codex round=1 head=abc fingerprint=fixture -->\nFinding",
                    "databaseId": 1,
                    "author": {"login": "tester"},
                },
                {
                    "body": "Not a local-review disposition.",
                    "databaseId": 2,
                    "author": {"login": "untrusted-user"},
                },
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }
    result = _run(
        consumer,
        ["--issues", "51"],
        issues=[_issue(51)],
        config=_config(tmp_path),
        extra_env={"AGENT_REVIEW_THREADS_JSON": json.dumps([thread])},
    )
    assert result.returncode != 0
    assert "disposition reply and be resolved" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_unpaginated_thread_comments_fail_closed(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The marker may sit past the first comment page, so a thread whose comments
    # are truncated cannot be cleared — even though no marker is visible on it.
    thread = {
        "isResolved": True,
        "comments": {
            "nodes": [{"body": "unrelated discussion", "databaseId": 1}],
            "pageInfo": {"hasNextPage": True},
        },
    }
    result = _run(
        consumer,
        ["--issues", "47"],
        issues=[_issue(47)],
        config=_config(tmp_path),
        extra_env={"AGENT_REVIEW_THREADS_JSON": json.dumps([thread])},
    )
    assert result.returncode != 0
    assert "disposition reply and be resolved" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_validation_hook_dirt_blocks_ready_and_preserves_the_worktree(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Head attestation compares SHAs only, so an uncommitted validation write
    # would otherwise be published-as-absent and then force-removed.
    validation = (
        "printf 'validate\\n' >> \"$EVENT_LOG\"; "
        '[ ! -e "$AGENT_LOOP_PR_URL_MARK" ] || printf regenerated > seed.txt'
    )
    result = _run(
        consumer,
        ["--issues", "48"],
        issues=[_issue(48)],
        config=_config(tmp_path, validation_hook=validation),
        extra_env={"AGENT_LOOP_PR_URL_MARK": str(consumer[3] / "pr-branch")},
    )
    assert result.returncode != 0
    assert "validation mutated the worktree or HEAD" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()
    worktrees = list((tmp_path / "worktrees").glob("*"))
    assert worktrees, "the worktree holding the uncommitted work must be preserved"


def test_base_movement_after_convergence_is_named_not_a_bare_abort(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Convergence proves base ancestry, not that the base tip is unchanged, and
    # the final reviewed-head validation runs between the two checks. A base
    # commit landing in that window must say so — an unexplained abort on a
    # converged PR invites a manual `gh pr ready`.
    validation = (
        "printf 'validate\\n' >> \"$EVENT_LOG\"; "
        '[ ! -e "$AGENT_LOOP_LOG_DIR/final-reviewed-head-validation.log" ] || '
        'printf \'%s\\n\' "$AGENT_MOVED_BASE_OID" > "$AGENT_STATE_DIR/pr-base-oid"'
    )
    result = _run(
        consumer,
        ["--issues", "51"],
        issues=[_issue(51)],
        config=_config(tmp_path, validation_hook=validation),
        extra_env={"AGENT_MOVED_BASE_OID": "b" * 40},
    )
    assert result.returncode != 0
    assert "PR base advanced or diverged" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()
    assert (consumer[3] / "pr-branch").exists(), "the draft PR must be preserved"
    assert list((tmp_path / "worktrees").glob("*")), "the worktree must be preserved"


def test_pr_body_rewrite_failure_stops_before_ready(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # A failed PR mutation has an uncertain remote outcome. Recovery must stop
    # before any later readiness mutation.
    result = _run(
        consumer,
        ["--issues", "52"],
        issues=[_issue(52)],
        config=_config(tmp_path),
        extra_env={"AGENT_PR_EDIT_FAIL": "1"},
    )
    assert result.returncode != 0
    assert not (consumer[3] / "pr-ready").exists()
    assert "Could not update the PR body" in result.stderr


def test_pr_ready_failure_is_named_so_it_is_not_silently_left_draft(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "53"],
        issues=[_issue(53)],
        config=_config(tmp_path),
        extra_env={"AGENT_PR_READY_FAIL": "1"},
    )
    assert result.returncode != 0
    assert "could not mark PR ready" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


def test_review_round_cap_preserves_draft_without_marking_ready(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    codex_hook = _committed_review_hook("codex", "material")
    result = _run(
        consumer,
        ["--issues", "42"],
        issues=[_issue(42)],
        config=_config(
            tmp_path,
            codex_review_hook=codex_hook,
            review_max_rounds=2,
        ),
    )
    assert result.returncode != 0
    assert "did not converge within 2 round(s)" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()
    assert (consumer[3] / "pr-branch").exists()


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


def test_hooks_and_default_worker_do_not_inherit_the_wrapper_stdin(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # `codex exec` reads stdin to EOF when it is not a TTY. A hook that inherits
    # an open pipe from whatever launched the wrapper blocks until the pass
    # times out, so the wrapper must hand every hook (and the default worker,
    # which runs through the same bounded runner) /dev/null even when its own
    # stdin is a pipe that never closes. The pipe here stays open for the whole
    # run: a `read` that sees EOF at once proves the redirect, a `read` that
    # has to wait for its timeout proves the leak.
    worker = (
        '[ "$(readlink /proc/self/fd/0)" = /dev/null ] || exit 71; '
        "if read -r -t 3 line; then exit 72; else status=$?; fi; "
        '[ "$status" -eq 1 ] || exit 73; '
        "printf done > result.txt; git add result.txt; git commit -m 'fix: worker'"
    )
    validation = '[ "$(readlink /proc/self/fd/0)" = /dev/null ] || exit 74'
    reader, writer = os.pipe()
    try:
        result = _run(
            consumer,
            ["--issues", "12"],
            issues=[_issue(12)],
            config=_config(tmp_path, worker_hook=worker, validation_hook=validation),
            stdin=reader,
        )
    finally:
        os.close(writer)
        os.close(reader)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("inherited", [None, "0", "1"])
def test_hooks_and_default_worker_run_background_tasks_in_the_foreground(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    inherited: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A one-shot Claude CLI that moves a long command to the background can
    # end its turn with it still running and exit 0 with no result. The
    # wrapper forces foreground execution for every hook, even over an
    # inherited 0.
    record = 'printf "%s\\n" "${CLAUDE_CODE_DISABLE_BACKGROUND_TASKS-unset}" >> "$AGENT_STATE_DIR/background.log"; '
    claude = consumer[2] / "claude"
    _write_executable(
        claude,
        "#!/usr/bin/env bash\n"
        + record
        + "printf 'done\\n' > result.txt\ngit add result.txt\ngit commit -m 'fix: worker'\n",
    )
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", raising=False)
    extra_env = {} if inherited is None else {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": inherited}
    result = _run(
        consumer,
        ["--issues", "19"],
        issues=[_issue(19)],
        config=_config_v3(
            tmp_path,
            worker_hook="",
            validation_hook=record + "true",
            codex_review_hook=record + _clean_v3_hook("codex"),
            claude_review_hook=record + _clean_v3_hook("claude"),
        ),
        extra_env=extra_env,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    values = (consumer[3] / "background.log").read_text(encoding="utf-8").split()
    # worker, worker validation, codex, claude, final validation
    assert len(values) >= 5
    assert set(values) == {"1"}


def test_capacity_failure_uses_fallback_model(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The default worker is the Claude CLI. Stub it so the primary model reports
    # a capacity failure and the retry switches to worker_fallback_model.
    claude = consumer[2] / "claude"
    _write_executable(
        claude,
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$AGENT_STATE_DIR/models.log"
if [[ "$*" == *"--model primary"* ]]; then
  echo 'capacity exhausted' >&2
  exit 9
fi
printf 'done\\n' > result.txt
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
    assert "--model primary" in models
    assert "--model fallback" in models


def test_worker_effort_is_passed_to_the_default_worker(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Before worker_effort existed the default worker's effort came from
    # whatever CLAUDE_CODE_EFFORT_LEVEL the operator exported at launch, which
    # was easy to forget and invisible afterwards.
    claude = consumer[2] / "claude"
    _write_executable(
        claude,
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$AGENT_STATE_DIR/worker-args.log"
printf 'done\\n' > result.txt
git add result.txt
git commit -m 'fix: effort worker'
""",
    )
    result = _run(
        consumer,
        ["--issues", "15"],
        issues=[_issue(15)],
        config=_config(
            tmp_path, worker_hook="", worker_model="primary", worker_effort="medium"
        ),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    args = (consumer[3] / "worker-args.log").read_text(encoding="utf-8")
    assert "--model primary --effort medium" in args

    (consumer[3] / "worker-args.log").unlink()
    result = _run(
        consumer,
        ["--issues", "16"],
        issues=[_issue(16)],
        config=_config(tmp_path, worker_hook="", worker_model="primary"),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    args = (consumer[3] / "worker-args.log").read_text(encoding="utf-8")
    assert "--effort" not in args


def test_worker_effort_must_be_a_single_flag_value(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "17"],
        issues=[_issue(17)],
        config=_config(tmp_path, worker_hook="", worker_effort="medium --foo"),
    )
    assert result.returncode != 0
    assert "worker_effort must be a single flag value" in result.stderr
    assert not (consumer[3] / "claimed-17").exists()


_HANDOFF = (
    "printf 'Classification: agent-bail: spec-gap (bucket A)\\n"
    "Requested labels: add agent-bail: spec-gap, remove dev: agent\\n' "
    '> "$AGENT_LOOP_HANDOFF_FILE"'
)


@pytest.mark.parametrize("exit_status", [0, 3])
def test_worker_handoff_is_a_bail_whatever_the_exit_status(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path, exit_status: int
) -> None:
    worker = (
        'if [ "$AGENT_LOOP_ISSUE_ID" = 40 ]; then '
        f"{_HANDOFF}; exit {exit_status}; fi; "
        "printf 'worker\\n' >> \"$EVENT_LOG\"; printf done > result.txt; "
        "git add result.txt; git commit -m 'fix: worker'"
    )
    result = _run(
        consumer,
        ["--issues", "40,41", "--iterations", "2"],
        issues=[_issue(40), _issue(41)],
        config=_config_v3(tmp_path, worker_hook=worker),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "agent-bail: spec-gap" in result.stdout
    assert "produced no local commit" not in result.stderr
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["bailed", "finalized"]
    assert batch["issues"][0]["classification"] == "spec-gap"
    log_dir = next((tmp_path / "logs").glob("*-issue-40-*"))
    assert (log_dir / "worker-attempt-1.log").exists()
    assert not (log_dir / "worker-attempt-2.log").exists()
    assert str(log_dir / "operator-handoff.md") in result.stdout
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    issue_40 = [line for line in gh_log.splitlines() if line.startswith("issue edit 40")]
    assert issue_40 == ["issue edit 40 --add-assignee @me", "issue edit 40 --remove-assignee @me"]
    assert "--add-label" not in gh_log and "--remove-label" not in gh_log
    assert "issue comment" not in gh_log
    assert not any((tmp_path / "worktrees").glob("*-issue-40-*"))


def test_worker_handoff_with_committed_work_is_an_ambiguous_bail(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    worker = (
        "printf done > result.txt; git add result.txt; git commit -m 'fix: worker'; "
        f"{_HANDOFF}"
    )
    result = _run(
        consumer,
        ["--issues", "42,43", "--iterations", "2"],
        issues=[_issue(42), _issue(43)],
        config=_config_v3(tmp_path, worker_hook=worker),
        timeout=60,
    )
    assert result.returncode != 0
    assert "the bail is ambiguous" in result.stderr
    batch_file = next((tmp_path / "logs").glob("*-batch-*.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert [row["status"] for row in batch["issues"]] == ["active", "pending"]
    assert any((tmp_path / "worktrees").glob("*-issue-42-*"))


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


def test_fresh_base_is_integrated_and_validated_before_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "9"],
        issues=[_issue(9)],
        config=_config(tmp_path),
        extra_env={
            "REMOTE_PATH": str(consumer[1]),
            "AGENT_ADVANCE_BASE_ON_REVIEW": "1",
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branch = _run_git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent-loop",
        cwd=consumer[1],
    ).stdout.strip()
    published = _run_git("show", f"{branch}:fresh-base.txt", cwd=consumer[1]).stdout
    assert published == "fresh\n"
    # The base advanced during round 1, so round 2 merges it before either engine
    # runs and both then review that merged head. That is a complete pass over the
    # final tree, so it must converge in round 2 rather than spending a third.
    assert "convergence round 2/4" in result.stdout
    assert "convergence round 3/4" not in result.stdout
    events = (consumer[3] / "events.log").read_text(encoding="utf-8").splitlines()
    assert events[-1] == "validate"


def test_v3_base_advance_during_review_restarts_without_stale_attestation(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    updater = tmp_path / "advance-base-v3.sh"
    _write_executable(
        updater,
        f"""#!/usr/bin/env bash
set -e
clone="$AGENT_STATE_DIR/base-clone-v3"
if [ ! -d "$clone/.git" ]; then
  /usr/bin/git clone "$REMOTE_PATH" "$clone" >/dev/null 2>&1
  /usr/bin/git -C "$clone" config user.name Test
  /usr/bin/git -C "$clone" config user.email test@example.invalid
  printf 'fresh-v3\n' > "$clone/fresh-base-v3.txt"
  /usr/bin/git -C "$clone" add fresh-base-v3.txt
  /usr/bin/git -C "$clone" commit -m 'chore: advance v3 base' >/dev/null
  /usr/bin/git -C "$clone" push origin main >/dev/null
fi
{_clean_v3_hook("claude")}
""",
    )
    result = _run(
        consumer,
        ["--issues", "35"],
        issues=[_issue(35)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook=_clean_v3_hook("codex"),
            claude_review_hook=str(updater),
        ),
        extra_env={
            "REMOTE_PATH": str(consumer[1]),
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "convergence round 2/4" in result.stdout
    assert "convergence round 3/4" not in result.stdout
    comments = (consumer[3] / "pr-comments.log").read_text(encoding="utf-8")
    assert "local-review-pass:v3 engine=claude round=1" not in comments
    assert "local-review-pass:v3 engine=codex round=2" in comments
    assert (consumer[3] / "pr-ready").exists()


def test_v3_base_advance_does_not_bypass_blocked_result(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    updater = tmp_path / "advance-base-v3-blocked.sh"
    _write_executable(
        updater,
        """#!/usr/bin/env bash
set -e
clone="$AGENT_STATE_DIR/base-clone-v3-blocked"
/usr/bin/git clone "$REMOTE_PATH" "$clone" >/dev/null 2>&1
/usr/bin/git -C "$clone" config user.name Test
/usr/bin/git -C "$clone" config user.email test@example.invalid
printf 'fresh-v3-blocked\n' > "$clone/fresh-base-v3-blocked.txt"
/usr/bin/git -C "$clone" add fresh-base-v3-blocked.txt
/usr/bin/git -C "$clone" commit -m 'chore: advance v3 base' >/dev/null
/usr/bin/git -C "$clone" push origin main >/dev/null
jq -n --arg engine "$AGENT_LOOP_REVIEW_ENGINE" \
  --argjson round "$AGENT_LOOP_REVIEW_ROUND" \
  --arg base "$AGENT_LOOP_REVIEW_BASE_SHA" \
  --arg head "$AGENT_LOOP_PR_HEAD_SHA" \
  '{version:3,status:"blocked",engine:$engine,round:$round,baseSha:$base,beforeSha:$head,afterSha:$head,classification:null,findingFingerprints:[],finalLaneComplete:false,blocker:"ledger mutation failed"}' \
  > "$AGENT_LOOP_REVIEW_RESULT_FILE"
""",
    )
    result = _run(
        consumer,
        ["--issues", "36"],
        issues=[_issue(36)],
        config=_config(
            tmp_path,
            review_contract_version=3,
            codex_review_hook=str(updater),
            claude_review_hook=_clean_v3_hook("claude"),
        ),
        extra_env={"REMOTE_PATH": str(consumer[1])},
    )
    assert result.returncode != 0
    assert "review blocked in round 1: ledger mutation failed" in result.stderr
    assert not (consumer[3] / "pr-ready").exists()


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
    branch = _run_git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent-loop",
        cwd=consumer[1],
    ).stdout.strip()
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
    branches = _run_git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent-loop",
        cwd=consumer[1],
    ).stdout
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


def test_missing_default_claude_fails_before_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    repo, _, bin_dir, state_dir = consumer
    no_claude_bin = tmp_path / "no-claude-bin"
    no_claude_bin.mkdir()
    for command in (
        "bash",
        "dirname",
        "flock",
        "git",
        "jq",
        "node",
        "python3",
        "realpath",
        "timeout",
    ):
        executable = shutil.which(command)
        assert executable is not None
        (no_claude_bin / command).symlink_to(executable)
    (no_claude_bin / "gh").symlink_to(bin_dir / "gh")

    result = _run(
        consumer,
        ["--issues", "14"],
        issues=[_issue(14)],
        config=_config(tmp_path, worker_hook=""),
        extra_env={"PATH": str(no_claude_bin)},
    )
    assert result.returncode != 0
    assert "required command not found for default worker: claude" in result.stderr
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


def test_include_assigned_never_rolls_back_preexisting_assignment(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--include-assigned"],
        issues=[_issue(18, assigned=True)],
        config=_config(tmp_path),
        extra_env={
            "AGENT_VERIFIED_ASSIGNEES": json.dumps(
                [{"login": "tester"}, {"login": "other-user"}]
            )
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "could not be claimed; skipping" in result.stdout
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "--add-assignee @me" not in gh_log
    assert "--remove-assignee @me" not in gh_log


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
    entries = list((tmp_path / "logs").iterdir())
    log_dirs = [entry for entry in entries if entry.is_dir()]
    assert len(log_dirs) == 1
    assert stat.S_IMODE(log_dirs[0].stat().st_mode) == 0o700
    for log_file in log_dirs[0].iterdir():
        assert stat.S_IMODE(log_file.stat().st_mode) & 0o077 == 0
    # The run's event stream sits beside the issue log directory.
    streams = [entry for entry in entries if not entry.is_dir()]
    assert [entry.name.endswith("-events.jsonl") for entry in streams] == [True]
    assert stat.S_IMODE(streams[0].stat().st_mode) == 0o600


def test_untracked_leftover_does_not_abort_batch_after_publish(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # A worker/setup hook can leave a non-ignored untracked file. With
    # `status.showUntrackedFiles=no` the clean-tree gates pass, but a plain
    # `git worktree remove` would still exit non-zero on it — which, post-publish
    # and under `set -e`, would abort the whole batch and fire a bogus recovery
    # banner. The success-path removal must force + tolerate so the run finishes
    # and continues to the next issue.
    _run_git("config", "status.showUntrackedFiles", "no", cwd=consumer[0])
    worker = (
        "printf done > result.txt; git add result.txt; git commit -m 'fix: worker'; "
        "printf scratch > leftover.txt"  # untracked, not ignored
    )
    result = _run(
        consumer,
        ["--issues", "21,22", "--iterations", "2"],
        issues=[_issue(21), _issue(22)],
        config=_config(tmp_path, worker_hook=worker),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Worktree preserved" not in result.stderr
    remote_branches = _run_git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent-loop",
        cwd=consumer[1],
    ).stdout
    assert "issue-21" in remote_branches
    assert "issue-22" in remote_branches
    paths = re.findall(r"^   Worktree: (.+)$", result.stdout, re.MULTILINE)
    assert len(paths) == 2
    assert all(not Path(path).exists() for path in paths)  # removed despite leftover


def test_pr_create_failure_reports_orphaned_pushed_branch(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The push lands the remote branch before `gh pr create` runs. If PR creation
    # fails, the recovery message must name the already-pushed branch so the
    # operator can open the PR or delete it — otherwise a re-run (new RUN_TAG)
    # orphans the first branch and double-PRs the issue.
    result = _run(
        consumer,
        ["--issues", "23"],
        issues=[_issue(23)],
        config=_config(tmp_path),
        extra_env={"AGENT_PR_CREATE_FAIL": "1"},
    )
    assert result.returncode != 0
    assert "could not create draft PR after publishing remote branch" in result.stderr
    # The push really happened, so the branch exists on the remote.
    remote_branches = _run_git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent-loop",
        cwd=consumer[1],
    ).stdout
    assert "issue-23" in remote_branches


def test_malformed_ready_payload_is_a_hard_error_not_empty_backlog(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # ready.py exiting 0 with a non-array payload must be a hard error, not read
    # as "no work" (which would exit 0 and look like an empty backlog).
    result = _run(
        consumer,
        [],
        issues=[_issue(24)],
        config=_config(tmp_path),
        extra_env={"AGENT_READY_JSON": '{"malformed": true}'},
    )
    assert result.returncode == 1
    assert "issue selection failed" in result.stderr
    assert not (tmp_path / "worktrees").exists()


def test_backstop_recovery_is_accurate_when_no_worktree_exists(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Force a bare `set -e` abort before the worktree is created: point
    # worktree_root at a regular file so `mkdir -p` fails. The on_exit backstop
    # must fire, and recovery_message must NOT claim a worktree is preserved at a
    # path that was never created.
    worktree_file = tmp_path / "worktrees-as-file"
    worktree_file.write_text("not a directory\n", encoding="utf-8")
    result = _run(
        consumer,
        ["--issues", "25"],
        issues=[_issue(25)],
        config=_config(tmp_path, worktree_root=str(worktree_file)),
    )
    assert result.returncode != 0
    assert "No worktree exists" in result.stderr
    assert "Worktree preserved" not in result.stderr


def test_timeout_with_committed_work_does_not_retry(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # A timeout that fires AFTER the worker committed must not retry on top of
    # that work — the retry gate is `worktree_has_work`, which a committed change
    # trips regardless of the timeout exit code (124/137).
    worker = (
        "printf done > result.txt; git add result.txt; "
        "git commit -m 'fix: committed then hung'; sleep 5"
    )
    result = _run(
        consumer,
        ["--issues", "26"],
        issues=[_issue(26)],
        config=_config(
            tmp_path,
            worker_hook=worker,
            worker_timeout_seconds=1,
            worker_retries=1,
        ),
    )
    assert result.returncode != 0
    assert "after changing or committing work" in result.stderr
    assert "Retrying worker" not in result.stdout
    match = re.search(r"Worktree preserved: (.+)", result.stderr)
    assert match
    assert Path(match.group(1)).exists()


@pytest.mark.parametrize(
    ("config_key", "config_value", "message"),
    [
        ("review_max_rounds", 5, "review_max_rounds cannot exceed the Deep review cap of 4"),
        ("review_timeout_seconds", 0, "review_timeout_seconds must be a positive integer"),
    ],
)
def test_review_budget_config_fails_before_issue_mutation(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    config_key: str,
    config_value: int,
    message: str,
) -> None:
    config = _config(tmp_path, **{config_key: config_value})
    # Deliberately not --dry-run: that short-circuits before the claim on every
    # path, so it would satisfy the gh-log assertion below whether or not config
    # validation ran.
    result = _run(
        consumer,
        ["--issues", "27"],
        issues=[_issue(27)],
        config=config,
    )
    assert result.returncode != 0
    assert message in result.stderr
    gh_log = consumer[3] / "gh.log"
    assert not gh_log.exists() or "issue edit" not in gh_log.read_text(encoding="utf-8")
