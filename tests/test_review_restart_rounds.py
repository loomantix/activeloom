"""Restarted runs retain historical evidence and use ledger 1.4 run-local rounds."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def handoff(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / ".codex/skills/critique/scripts/local-review-handoff.py"
    )
    spec = importlib.util.spec_from_file_location("restart_handoff", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_verify_head", lambda *args: None)
    monkeypatch.setattr(
        module,
        "_run_records",
        lambda rows: [
            {
                "comment_id": 20,
                "run_id": "d" * 64,
                "base": "b" * 40,
                "tier": "deep",
                "max_rounds": 4,
            }
        ],
    )
    monkeypatch.setattr(module, "_run_end", lambda *args: None)
    return module


def marker(engine: str, round_number: int) -> str:
    return (
        f"<!-- local-review-pass:v3 engine={engine} round={round_number} "
        f"base={'b' * 40} head={'a' * 40} result-sha256={'c' * 64} -->"
    )


def test_restart_authorizes_fresh_identity_not_occupied_round(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [{"id": 10, "body": marker("claude", 4)}]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base="b" * 40,
        head="a" * 40,
        engine="claude",
        round=1,
    )
    handoff._authorize_pass(args)
    result = json.loads(capsys.readouterr().out)
    assert result["round"] == 1
    assert result["run_round"] == 1
    args.round = 0
    with pytest.raises(handoff.HandoffError, match="positive"):
        handoff._authorize_pass(args)


def test_restart_still_enforces_cap_and_no_skips(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"id": 10, "body": marker("claude", 4)}]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    args = SimpleNamespace(
        repo="example/repo", pr=7, base="b" * 40, head="a" * 40, engine="codex", round=2
    )
    with pytest.raises(handoff.HandoffError, match="skip"):
        handoff._authorize_pass(args)
    rows.append({"id": 21, "body": marker("claude", 1)})
    handoff._authorize_pass(args)
    args.round = 5
    with pytest.raises(handoff.HandoffError, match="exceeds the deep cap"):
        handoff._authorize_pass(args)


def test_first_run_and_duplicate_identity(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    args = SimpleNamespace(
        repo="example/repo", pr=7, base="b" * 40, head="a" * 40, engine="codex", round=1
    )
    handoff._authorize_pass(args)
    assert json.loads(capsys.readouterr().out)["run_round"] == 1
    rows.append({"id": 21, "body": marker("codex", 1)})
    with pytest.raises(handoff.HandoffError, match="already completed"):
        handoff._authorize_pass(args)


def test_historical_changed_attestations_do_not_consume_new_run_budget(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    changed = (
        f"<!-- local-review-complete:v3 engine=claude round=7 "
        f"base={'b' * 40} before={'e' * 40} head={'a' * 40} "
        f"classification=material fingerprints=example result-sha256={'c' * 64} -->"
    )
    monkeypatch.setattr(
        handoff, "_issue_comments", lambda *args: [{"id": 10, "body": changed}]
    )
    handoff._authorize_pass(
        SimpleNamespace(
            repo="example/repo",
            pr=7,
            base="b" * 40,
            head="a" * 40,
            engine="codex",
            round=1,
        )
    )
    assert json.loads(capsys.readouterr().out)["run_round"] == 1


def test_start_run_returns_first_round(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Explicit review authorization.\n")
    rows = [{"id": 10, "body": marker("codex", 3)}]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    monkeypatch.setattr(handoff, "_run_records", lambda rows: [])
    monkeypatch.setattr(handoff, "_post_issue_comment", lambda *args: (20, False))
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base="b" * 40,
        head="a" * 40,
        tier="deep",
        restart=False,
        authorization_file=str(authorization),
    )
    handoff._start_run(args)
    assert json.loads(capsys.readouterr().out)["first_round"] == 1


@pytest.mark.parametrize(("tier", "cap"), [("lean", 2), ("deep", 4)])
def test_restarted_controller_round_finalizes_with_published_ledger(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tier: str,
    cap: int,
) -> None:
    """Exercise the controller/Node boundary and replay without GitHub writes."""
    base, head = "b" * 40, "a" * 40
    content = "Explicitly authorized new review run.\n"
    digest = hashlib.sha256(
        json.dumps(
            {
                "base": base,
                "content": content,
                "max_rounds": cap,
                "start_head": head,
                "supersedes": None,
                "tier": tier,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    rows = [
        {"id": 10, "body": marker("codex", cap), "user": {"login": "reviewer"}},
        {
            "id": 20,
            "user": {"login": "reviewer"},
            "body": (
                f"<!-- local-review-run:v1 id={digest} tier={tier} max-rounds={cap} "
                f"base={base} start-head={head} supersedes=none content-sha256={digest} -->\n"
                + content
            ),
        },
    ]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    monkeypatch.setattr(
        handoff,
        "_run_records",
        lambda _: [
            {
                "comment_id": 20,
                "run_id": digest,
                "base": base,
                "tier": tier,
                "max_rounds": cap,
            }
        ],
    )
    handoff._authorize_pass(
        SimpleNamespace(
            repo="example/repo",
            pr=7,
            base=base,
            head=head,
            engine="codex",
            round=1,
        )
    )
    authorized = json.loads(capsys.readouterr().out)
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(
            {
                "version": 3,
                "status": "clean",
                "engine": "codex",
                "round": authorized["round"],
                "baseSha": base,
                "beforeSha": head,
                "afterSha": head,
                "classification": None,
                "findingFingerprints": [],
                "finalLaneComplete": True,
            }
        )
    )
    state = tmp_path / "comments.json"
    state.write_text(json.dumps(rows))
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
state = Path(os.environ["TEST_COMMENTS"])
rows = json.loads(state.read_text())
if args == ["api", "user"]:
    print(json.dumps({"login": "reviewer"}))
elif args[:2] == ["pr", "view"]:
    print(("b" if "baseRefOid" in args else "a") * 40)
elif args[:3] == ["api", "-X", "POST"]:
    row = {"id": 30, "body": json.load(sys.stdin)["body"], "user": {"login": "reviewer"}}
    rows.append(row)
    state.write_text(json.dumps(rows))
    print(json.dumps(row))
elif "repos/example/repo/issues/comments/30" in args:
    print(json.dumps(rows[-1]))
elif "repos/example/repo/issues/7/comments?per_page=100" in args:
    print(json.dumps([rows]))
else:
    raise SystemExit("Unexpected gh request: " + repr(args))
"""
    )
    gh.chmod(0o755)
    git = tmp_path / "git"
    git.write_text(
        f"#!{sys.executable}\n"
        + """import sys
args = sys.argv[1:]
if args == ["rev-parse", "HEAD"]:
    print("a" * 40)
elif args == ["rev-parse", "--verify", "b" * 40 + "^{commit}"]:
    print("b" * 40)
elif args[:2] == ["merge-base", "--is-ancestor"]:
    pass
else:
    raise SystemExit("Unexpected git request: " + repr(args))
"""
    )
    git.chmod(0o755)
    threads = tmp_path / "threads.json"
    threads.write_text(
        json.dumps(
            [
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False},
                                }
                            }
                        }
                    }
                }
            ]
        )
    )
    env = dict(
        os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", TEST_COMMENTS=str(state)
    )
    env.pop("AGENT_LOOP_REVIEW_ACTOR", None)
    ledger = (
        Path(__file__).resolve().parents[1]
        / ".codex/skills/critique/scripts/review-ledger.js"
    )
    command = [
        "node",
        str(ledger),
        "finalize",
        "--repo",
        "example/repo",
        "--pr",
        "7",
        "--result-file",
        str(result_file),
        "--threads-file",
        str(threads),
        "--expected-threads-sha256",
        hashlib.sha256(threads.read_bytes()).hexdigest(),
    ]
    for replayed in (False, True):
        completed = subprocess.run(
            command, env=env, capture_output=True, text=True, check=False
        )
        assert completed.returncode == 0, completed.stderr
        finalized = json.loads(completed.stdout)
        assert finalized["verified"] is True
        assert finalized["replayed"] is replayed
        assert finalized["comment_id"] == 30
    persisted = json.loads(state.read_text())
    assert persisted[:2] == rows
    assert len(persisted) == 3
