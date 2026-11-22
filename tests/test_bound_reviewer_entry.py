"""Exercise the command a herder gives an interactive reviewer, with offline GitHub."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(".agents")
ENTRY = HARNESS / "skills/hreview/scripts/reviewer-entry.py"
LEDGER = HARNESS / "skills/critique/scripts/review-ledger.js"
pytestmark = pytest.mark.regression


def git(tree: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", tree, *args], text=True).strip()


def commit(tree: Path, message: str) -> str:
    git(tree, "add", ".")
    git(tree, "-c", "user.name=Test", "-c", "user.email=test@example.org",
        "-c", "commit.gpgsign=false", "commit", "-qm", message)
    return git(tree, "rev-parse", "HEAD")


def legacy_ledger(tree: Path) -> None:
    path = tree / LEDGER
    path.write_text("""
const command = process.argv[2];
if (command === '--version') console.log('1.9.0');
else if (command === '--protocol-version') console.log('3');
else if (command === 'classify-changeset')
  console.log(JSON.stringify({skip: false, smallChange: false, legacyPolicy: true}));
else { console.error('unknown command: ' + command); process.exit(2); }
""")
    digest = base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()
    path.with_suffix(".integrity").write_text("sha512-" + digest)
    path.with_suffix(".version").write_text("1.9.0")


def fake_github(tmp: Path, head: str, base: str) -> dict[str, str]:
    gh = tmp / "gh"
    gh.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
store = Path(os.environ['PUBLIC_STORE'])
rows = json.loads(store.read_text()) if store.exists() else []
head = os.environ['TEST_HEAD']
if a[:2] == ['repo','view']: print('sample/project')
elif a[:2] == ['pr','view'] and 'baseRefName,headRefOid,state' in a:
    print(json.dumps(dict(headRefOid=head, baseRefName='release', state='OPEN')))
elif a[:2] == ['pr','view']: print(head)
elif a[:2] == ['api','repos/sample/project/branches/release']: print(os.environ['TEST_BASE'])
elif a[:2] == ['api','user']:
    actor = os.environ.get('TEST_ACTOR', 'reviewer')
    print(actor if '--jq' in a else json.dumps({'login':actor}))
elif 'graphql' in a and any('issueOrPullRequest' in arg for arg in a):
    nodes = [dict(databaseId=r['id'], body=r['body'], author=r['user'],
                  createdAt=r['created_at'], lastEditedAt=None) for r in rows]
    page = dict(nodes=nodes, totalCount=len(nodes), pageInfo=dict(hasNextPage=False,endCursor=None))
    print(json.dumps([{'data':{'repository':{'issueOrPullRequest':{'comments':page}}}}]))
elif a == ['api','repos/sample/project/pulls/3']:
    print(json.dumps(dict(commits=1, changed_files=1, head=dict(sha=head), base=dict(ref='release'))))
elif any('/commits?' in arg for arg in a):
    print(json.dumps([[dict(sha=head, parents=[{}], commit=dict(message='test', verification=dict(verified=True)))]]))
elif any('/files?' in arg for arg in a):
    print('[[{"filename":"subject.py","status":"modified","additions":1,"deletions":0,"patch":"+x"}]]')
elif any('/rules/branches/' in arg for arg in a): print('[[{"type":"required_signatures"}]]')
elif '--paginate' in a: print(json.dumps([rows]))
elif 'POST' in a:
    row = dict(json.load(sys.stdin), id=len(rows)+1, user={'login':'reviewer'}, created_at='2026-01-01T00:00:00Z')
    store.write_text(json.dumps(rows+[row]))
    print(json.dumps(row))
elif any('/issues/comments/' in arg for arg in a): print(json.dumps(rows[int(a[-1].split('/')[-1])-1]))
else: sys.exit('unexpected synthetic gh request: ' + str(a))
""")
    gh.chmod(0o755)
    return dict(os.environ, PATH=str(tmp) + os.pathsep + os.environ["PATH"], TEST_HEAD=head, TEST_BASE=base,
                ACTIVELOOM_SILENT_ROOT=str(tmp / "state"), ACTIVELOOM_REVIEW_SILENT="0",
                ACTIVELOOM_REVIEW_PROFILE=str(tmp / "profile"), PUBLIC_STORE=str(tmp / "public"))


@pytest.mark.parametrize(("legacy", "silent"), [(True, True), (True, False), (False, False)])
def test_bound_pass(tmp_path: Path, legacy: bool, silent: bool) -> None:
    tools, policy, candidate = (tmp_path / name for name in ("tools", "base", "candidate"))
    tools.mkdir()
    git(tools, "init", "-q")
    shutil.copytree(ROOT / HARNESS, tools / HARNESS)
    revision = commit(tools, "installed reviewer")
    git(tools, "worktree", "add", "--detach", str(policy), revision)
    if legacy:
        legacy_ledger(policy)
    skill = HARNESS / "skills/deepcritique/SKILL.md"
    with (policy / skill).open("a") as out:
        out.write("\nSynthetic base-only gate.\n")
    base = commit(policy, "trusted target policy")
    git(tools, "worktree", "add", "--detach", str(candidate), base)
    (candidate / skill).write_text("Synthetic candidate-only gate.\n")
    head = commit(candidate, "candidate change")
    env = fake_github(tmp_path, head, base)
    if silent:
        (tmp_path / "profile").write_text('{"silent":{"repos":["sample/project"]}}')

    args = ["python3", "-IB", str(tools / ENTRY), "--tool-revision", revision,
            "--policy-tree", str(policy), "--policy-revision", base, "--head", head,
            "--repo", "sample/project", "--pr", "3", "--engine", "gemini"]
    def invoke(*extra: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args + list(extra), cwd=candidate, env=env, text=True,
                              capture_output=True, check=check)

    if legacy:
        for tree in (policy, candidate):
            assert subprocess.check_output(["node", tree / LEDGER, "--version"], text=True).strip() == "1.9.0"
            rejected = subprocess.run(["node", tree / LEDGER, "silent-status"], capture_output=True, text=True)
            assert rejected.returncode and "unknown command: silent-status" in rejected.stderr
    env["TEST_BASE"] = head
    stale = invoke("check", check=False)
    assert stale.returncode and "live target branch" in stale.stderr
    env["TEST_BASE"] = base
    checked = json.loads(invoke("check").stdout)
    assert checked["classifier"] == str(policy / LEDGER)
    assert checked["merge_base"] == base
    assert bool(checked["classification"].get("legacyPolicy")) == legacy
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "public").exists()
    if silent:
        env["AGENT_LOOP_REVIEW_ACTOR"] = "other-reviewer"
        for action in ("check", "context"):
            mismatch = invoke(action, check=False)
            assert mismatch.returncode and "actor" in mismatch.stderr
            assert not (tmp_path / "state").exists()
        del env["AGENT_LOOP_REVIEW_ACTOR"]
    context = json.loads(invoke("context").stdout)
    assert context["silent"] is silent
    authorization = tmp_path / "authorization"
    authorization.write_text("Synthetic local review authorization.")
    run = json.loads(subprocess.check_output(
        ["python3", "-IB", checked["controller"], "start-run", "--head", head, "--base", base,
         "--repo", "sample/project", "--pr", "3", "--tier", "deep", "--cycle", "gemini,codex",
         "--authorization-file", authorization], cwd=candidate, env=env, text=True))
    assert run["verified"] and run["first_round"] == 1 and run["max_rounds"] == 4
    enter = ("enter", "--skill", "deepcritique", "--receipt", context["receipt"],
             "--base", base, "--run-id", run["run_id"], "--round", "1")
    wrong_round = invoke(*enter[:-1], "2", check=False)
    assert wrong_round.returncode and "next required pass" in wrong_round.stderr and not wrong_round.stdout
    prompt = invoke(*enter).stdout
    assert "Synthetic base-only gate" in prompt and "Synthetic candidate-only gate" not in prompt
    helper = re.search(r"^node (\S+) --version$", prompt, re.M)
    assert helper and helper[1] == str(tools / LEDGER)
    body = tmp_path / "body"
    body.write_text(f"<!-- local-review-tier:v1 head={head} -->\nSynthetic review tier.\n")
    posted = json.loads(subprocess.check_output(
        ["node", helper[1], "post-pr-comment", "--repo", "sample/project", "--pr", "3",
         "--head", head, "--body-file", body], cwd=candidate, env=env, text=True))
    assert posted["verified"] is True
    assert (posted["comment_id"] > 2**52) is silent
    assert (tmp_path / "public").exists() is not silent

    if silent:
        original = env["ACTIVELOOM_SILENT_ROOT"]
        env["ACTIVELOOM_SILENT_ROOT"] = str(tmp_path / "different-store")
        redirected = invoke(*enter, check=False)
        assert redirected.returncode and not redirected.stdout
        assert not (tmp_path / "different-store").exists()
        env["ACTIVELOOM_SILENT_ROOT"] = original
    env["TEST_ACTOR"] = "different-reviewer"
    refused = invoke(*enter, check=False)
    assert refused.returncode and not refused.stdout
    assert "actor" in refused.stderr or "owner" in refused.stderr
    assert git(candidate, "rev-parse", "HEAD") == head
    assert not git(candidate, "status", "--porcelain", "--ignored")
    assert not git(policy, "status", "--porcelain", "--ignored")

    if legacy and not silent:
        # A self-consistent but incapable controller must stop even if the candidate matches it.
        legacy_ledger(tools)
        args[args.index(revision)] = commit(tools, "unsupported transport")
        refused = invoke("check", check=False)
        assert refused.returncode and ("unknown command" in refused.stderr or "missing commands" in refused.stderr)



@pytest.mark.parametrize("harness", [".agents", ".claude", ".codex"])
def test_herder_checks_before_restart_and_dispatches_bound_entry(harness: str) -> None:
    skill = (ROOT / harness / "skills/hreview/SKILL.md").read_text()
    assert skill.index('check <binding>') < skill.index('context <binding>') < skill.index('$H start-run')
    assert skill.index('check <binding>') < skill.index('$C record-policy-restart')
    assert 'enter <binding> --receipt <saved receipt> --skill <skill> --base <run base> --run-id <run id> --round <round>' in skill
