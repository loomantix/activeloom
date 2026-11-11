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
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from tests.controller_remote import ControllerRemote
else:
    remote_spec = importlib.util.spec_from_file_location("controller_remote", Path(__file__).with_name("controller_remote.py"))
    assert remote_spec and remote_spec.loader
    remote_module = importlib.util.module_from_spec(remote_spec)
    remote_spec.loader.exec_module(remote_module)
    ControllerRemote = remote_module.ControllerRemote


VerifyCalls = list[tuple[str, tuple[Any, ...]]]
pytestmark = pytest.mark.usefixtures("offline_github")


@pytest.fixture
def verify_calls() -> VerifyCalls:
    """Every head verification the command under test performed, in order.

    The `handoff` fixture used to stub both verifiers to no-ops, which left the
    call sites themselves untested: deleting a `_verify_reviewable_head` call
    from a command still passed. Recording instead of discarding keeps the
    stubs' convenience and makes the invocation assertable.
    """
    return []


@pytest.fixture
def handoff(monkeypatch: pytest.MonkeyPatch, verify_calls: VerifyCalls) -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / ".codex/skills/critique/scripts/local-review-handoff.py"
    )
    spec = importlib.util.spec_from_file_location("restart_handoff", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def record_head(*args: Any) -> None:
        verify_calls.append(("head", args))

    def record_history(*args: Any) -> None:
        verify_calls.append(("history", args))

    monkeypatch.setattr(module, "_verify_head", record_head)
    monkeypatch.setattr(
        module,
        "_scope_signals",
        lambda *_: {"behaviour_commits": 0, "missing_patch_files": []},
    )
    monkeypatch.setattr(module, "_verify_signed_pr_history", record_history)
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


@pytest.fixture
def real_handoff() -> ModuleType:
    """The controller with nothing stubbed, so `_run_records` itself runs.

    The `handoff` fixture replaces `_run_records` with a hand-built record, which
    is convenient for round arithmetic but makes the run marker's own
    authentication unreachable — a test written against `handoff` silently
    exercises the stub instead of the parser.
    """
    path = (
        Path(__file__).resolve().parents[1]
        / ".codex/skills/critique/scripts/local-review-handoff.py"
    )
    spec = importlib.util.spec_from_file_location("restart_handoff_real", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lifecycle_handoff(
    real_handoff: ModuleType, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, verify_calls: VerifyCalls,
) -> ModuleType:
    """Keep real run/lifecycle parsers and record the signed-history boundary."""
    ControllerRemote(real_handoff, monkeypatch, tmp_path)
    monkeypatch.setattr(real_handoff, "_scope_signals", lambda *_: {
        "behaviour_commits": 0, "missing_patch_files": [],
    })
    monkeypatch.setattr(real_handoff, "_verify_signed_pr_history",
                        lambda *args: verify_calls.append(("history", args)))
    return real_handoff


def marker(engine: str, round_number: int) -> str:
    return (
        f"<!-- local-review-pass:v3 engine={engine} round={round_number} "
        f"base={'b' * 40} head={'a' * 40} result-sha256={'c' * 64} -->"
    )


@pytest.mark.parametrize("returncode", [0, 1])
def test_authorize_fix_checks_posted_finding_before_handoff(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    returncode: int,
) -> None:
    rows = [{"id": 21, "body": marker("codex", 1)},
            {"id": 22, "body": marker("codex", 2)}]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *_: rows)
    calls: list[list[str]] = []

    def run(command: list[str], **_: Any) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(
            returncode=returncode,
            stdout=json.dumps({"verified": True, "stance": "convergence",
                               "severity": "blocking", "fingerprint": "window"}),
            stderr="convergence fixes require a posted blocking finding",
        )

    monkeypatch.setattr(handoff.subprocess, "run", run)
    args = handoff._parser().parse_args([
        "authorize-fix", "--repo", "example/repo", "--pr", "7",
        "--head", "a" * 40, "--base", "b" * 40,
        "--run-id", "d" * 64, "--engine", "codex", "--round", "3",
        "--fingerprint", "window", "--occurrence", "1",
    ])
    if returncode:
        with pytest.raises(handoff.HandoffError, match="fix authorization refused"):
            args.handler(args)
        assert capsys.readouterr().out == ""
    else:
        args.handler(args)
        result = json.loads(capsys.readouterr().out)
        assert result["run_id"] == "d" * 64
        assert result["severity"] == "blocking"
    assert len(calls) == 1
    assert calls[0][2:] == [
        "preflight-fix", "--repo", "example/repo", "--pr", "7",
        "--head", "a" * 40, "--engine", "codex", "--round", "3",
        "--fingerprint", "window", "--occurrence", "1",
    ]


@pytest.mark.parametrize("failure", ["stance", "ended-run"])
def test_authorize_fix_rechecks_controller_after_finding_preflight(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    monkeypatch.setattr(handoff, "_issue_comments", lambda *_: [])

    def run(*_: Any, **__: Any) -> SimpleNamespace:
        if failure == "ended-run":
            monkeypatch.setattr(handoff, "_run_end", lambda *_: {"outcome": "aborted"})
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "verified": True, "stance": "convergence" if failure == "stance" else "adversarial",
        }))

    monkeypatch.setattr(handoff.subprocess, "run", run)
    args = SimpleNamespace(repo="example/repo", pr=7, head="a" * 40,
                           base="b" * 40, run_id="d" * 64,
                           engine="codex", round=1, fingerprint="window", occurrence=1)
    with pytest.raises(handoff.HandoffError, match="stance disagrees|run has ended"):
        handoff._authorize_fix(args)


def test_authorize_fix_rejects_stale_run_before_finding_check(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handoff, "_issue_comments", lambda *_: [])
    def unexpected(*_: Any, **__: Any) -> None:
        pytest.fail("must reject stale run before launching finding preflight")
    monkeypatch.setattr(handoff.subprocess, "run", unexpected)
    args = SimpleNamespace(repo="example/repo", pr=7, head="a" * 40,
                           base="b" * 40, run_id="e" * 64,
                           engine="codex", round=1, fingerprint="window", occurrence=1)
    with pytest.raises(handoff.HandoffError, match="active run changed"):
        handoff._authorize_fix(args)


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


def test_authorization_rejects_replacement_run_at_same_head_and_round(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: [])
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base="b" * 40,
        head="a" * 40,
        engine="codex",
        round=1,
        run_id="e" * 64,
    )
    with pytest.raises(handoff.HandoffError, match="active run changed"):
        handoff._authorize_pass(args)
    args.run_id = "d" * 64
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
    lifecycle_handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    handoff = lifecycle_handoff
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Explicit review authorization.\n")
    rows = [{"id": 10, "body": marker("codex", 3)}]
    ControllerRemote.for_controller(handoff).rows = rows
    def post(*args: Any, lifecycle_write: bool = False) -> tuple[int, bool]:
        assert lifecycle_write
        rows.append({"id": 20, "body": args[-1]})
        return 20, False
    monkeypatch.setattr(handoff, "_post_issue_comment", post)
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


@pytest.mark.parametrize(
    ("tier", "trigger"), [("deep", "3"), ("deep", "1,3"), ("lean", "none")]
)
def test_start_run_includes_tier_without_repeating_authorization(
    lifecycle_handoff: ModuleType,
    real_handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tier: str,
    trigger: str,
) -> None:
    handoff = lifecycle_handoff
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Explicit review authorization.")
    rows: list[dict[str, Any]] = []
    ControllerRemote.for_controller(handoff).rows = rows

    def post(*args: object, lifecycle_write: bool = False) -> tuple[int, bool]:
        assert lifecycle_write
        rows.append({"id": 20, "body": args[-1]})
        return 20, False

    monkeypatch.setattr(handoff, "_post_issue_comment", post)
    args = SimpleNamespace(
        repo="example/repo", pr=7, base="b" * 40, head="a" * 40,
        tier=tier, trigger=trigger, restart=False, cycle="codex,claude",
        authorization_file=str(authorization),
    )
    parsed = real_handoff._parser().parse_args([
        "start-run", "--repo", args.repo, "--pr", "7", "--head", args.head,
        "--base", args.base, "--tier", tier, "--trigger", trigger,
        "--cycle", "codex,claude", "--authorization-file", str(authorization),
    ])
    assert parsed.trigger == trigger
    handoff._start_run(args)
    first = json.loads(capsys.readouterr().out)
    body = str(rows[0]["body"])
    assert f"<!-- local-review-tier:v1 tier={tier} trigger={trigger} head={args.head} -->" in body
    assert body.count(authorization.read_text()) == 1
    assert f"{tier.title()} review started" in body
    assert "Codex → Claude" in body
    handoff._start_run(args)
    replay = json.loads(capsys.readouterr().out)
    assert replay["run_id"] == first["run_id"]
    assert replay["replayed"] is True
    assert len(rows) == 1


@pytest.mark.parametrize(
    ("tier", "trigger"),
    [("deep", "none"), ("lean", "3"), ("deep", "1,7"), ("deep", "3,")],
)
def test_start_run_rejects_trigger_that_does_not_match_tier(
    lifecycle_handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tier: str,
    trigger: str,
) -> None:
    handoff = lifecycle_handoff
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Explicit review authorization.")
    args = SimpleNamespace(
        repo="example/repo", pr=7, base="b" * 40, head="a" * 40,
        tier=tier, trigger=trigger, restart=False, cycle="codex,claude",
        authorization_file=str(authorization),
    )
    with pytest.raises(handoff.HandoffError, match="must match the resolved tier"):
        handoff._start_run(args)


@pytest.mark.parametrize(("tier", "cap"), [("lean", 2), ("deep", 4)])
@pytest.mark.parametrize("sequenced", [False, True])
@pytest.mark.regression
def test_restarted_controller_round_finalizes_with_published_ledger(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tier: str,
    cap: int,
    sequenced: bool,
) -> None:
    """Exercise the controller/Node boundary and replay without GitHub writes."""
    base, head = "b" * 40, "a" * 40
    content = "Explicitly authorized new review run.\n"
    if sequenced:
        content = "<!-- local-review-sequence:v1 engines=codex,claude -->\n\n" + content
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
                "sequence": ["codex", "claude"] if sequenced else None,
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
elif "graphql" in args and any("issueOrPullRequest" in arg for arg in args):
    nodes = [{"databaseId": row["id"], "body": row["body"],
              "author": row["user"], "createdAt": "2026-01-01T00:00:00Z",
              "lastEditedAt": None} for row in rows]
    print(json.dumps([{"data": {"repository": {"issueOrPullRequest": {"comments": {
        "totalCount": len(nodes), "nodes": nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}]))
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


@pytest.mark.parametrize("lost_response", [False, True])
def test_handoff_restart_isolated_and_replayable(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    lost_response: bool,
) -> None:
    """A same-head restart must not reuse or read an older reverse handoff."""
    rows: list[dict[str, object]] = []
    runs = [
        {
            "comment_id": 20,
            "run_id": "d" * 64,
            "base": "b" * 40,
            "tier": "deep",
            "max_rounds": 4,
        }
    ]
    lose_next_post = False
    advance_run_on_post = False

    def github(args: list[str], payload: dict[str, object] | None = None) -> object:
        nonlocal lose_next_post
        if args[:3] == ["api", "-X", "POST"]:
            assert payload is not None
            comment_id = (
                max(
                    [int(str(row["id"])) for row in rows]
                    + [int(str(run["comment_id"])) for run in runs]
                )
                + 1
            )
            row: dict[str, object] = {
                "id": comment_id,
                "body": payload["body"],
                "user": {"login": "reviewer"},
            }
            rows.append(row)
            if advance_run_on_post:
                runs.append(
                    {**runs[-1], "comment_id": comment_id + 1, "run_id": "f" * 64}
                )
            if lose_next_post:
                lose_next_post = False
                raise handoff.HandoffError("simulated lost POST response")
            return row
        assert args[0] == "api" and "/issues/comments/" in args[1]
        return next(row for row in rows if str(row["id"]) == args[1].rsplit("/", 1)[1])

    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    monkeypatch.setattr(handoff, "_run_records", lambda _: runs)
    monkeypatch.setattr(handoff, "_current_actor", lambda: "reviewer")
    monkeypatch.setattr(handoff, "_json_output", github)
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base="b" * 40,
        head="a" * 40,
        from_engine="codex",
        to_engine="claude",
        round=1,
        outcome="clean",
        context_file=None,
    )
    handoff._post_handoff(args)
    capsys.readouterr()
    args.from_engine, args.to_engine = "claude", "codex"
    handoff._post_handoff(args)
    capsys.readouterr()
    history = [dict(row) for row in rows]
    runs.append({**runs[0], "comment_id": 30, "run_id": "e" * 64})
    show = SimpleNamespace(repo="example/repo", pr=7, engine="claude")
    with pytest.raises(handoff.HandoffError, match="no authenticated"):
        handoff._show_handoff(show)
    args.from_engine, args.to_engine = "codex", "claude"
    lose_next_post = lost_response
    handoff._post_handoff(args)
    posted = json.loads(capsys.readouterr().out)
    assert posted["comment_id"] == 31
    assert posted["replayed"] is lost_response
    handoff._post_handoff(args)
    assert json.loads(capsys.readouterr().out)["replayed"] is True
    handoff._show_handoff(show)
    shown = json.loads(capsys.readouterr().out)
    assert shown["comment_id"] == 31 and shown["verified"] is True
    assert rows[:2] == history
    assert len(rows) == 3

    # A delayed old-run write must never become verified current-run state --
    # and must not block reading this run's own handoff either. Selecting the
    # newest comment and refusing on it let one orphan wedge every later read,
    # so the reader now selects within the run.
    rows.append({**history[0], "id": 32})
    handoff._show_handoff(show)
    orphaned = json.loads(capsys.readouterr().out)
    assert orphaned["comment_id"] == 31 and orphaned["run_id"] == "e" * 64
    rows[-1]["body"] = str(rows[-1]["body"]).replace(
        "run=" + "d" * 64, "run=" + "e" * 64
    )
    with pytest.raises(handoff.HandoffError, match="digest"):
        handoff._show_handoff(show)

    # Even at the same Git head, a concurrent run transition invalidates a read.
    rows.pop()
    monkeypatch.setattr(
        handoff,
        "_verify_head",
        lambda *args: runs.append(
            {
                **runs[-1],
                "comment_id": 40,
                "run_id": "f" * 64,
            }
        ),
    )
    with pytest.raises(handoff.HandoffError, match="run changed"):
        handoff._show_handoff(show)
    # A run can also advance while a mutation response is in flight.
    monkeypatch.setattr(handoff, "_verify_head", lambda *args: None)
    runs[-1]["run_id"] = "e" * 64
    advance_run_on_post = True
    args.round = 2
    with pytest.raises(handoff.HandoffError, match="run changed"):
        handoff._post_handoff(args)


def test_legacy_handoff_without_run_still_reads(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Preserve the old digest/marker contract only when no run exists."""
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(handoff, "_run_records", lambda _: [])
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)

    def post(repo: str, pr: int, marker: str, body: str) -> tuple[int, bool]:
        assert " run=" not in marker
        rows.append({"id": 1, "body": body})
        return 1, False

    monkeypatch.setattr(handoff, "_post_issue_comment", post)
    handoff._post_handoff(
        SimpleNamespace(
            repo="example/repo",
            pr=7,
            base="b" * 40,
            head="a" * 40,
            from_engine="codex",
            to_engine="claude",
            round=1,
            outcome="clean",
            context_file=None,
        )
    )
    capsys.readouterr()
    handoff._show_handoff(SimpleNamespace(repo="example/repo", pr=7, engine="claude"))
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_ended_run_refuses_handoff_reads_and_writes(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: [])
    monkeypatch.setattr(handoff, "_run_end", lambda *args: {"outcome": "aborted"})
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base="b" * 40,
        head="a" * 40,
        from_engine="codex",
        to_engine="claude",
        round=1,
        outcome="clean",
        context_file=None,
    )
    with pytest.raises(handoff.HandoffError, match="ended"):
        handoff._post_handoff(args)
    with pytest.raises(handoff.HandoffError, match="ended"):
        handoff._show_handoff(
            SimpleNamespace(repo="example/repo", pr=7, engine="claude")
        )


def test_legacy_numbered_attestation_inside_run_is_refused(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run carrying pre-1.4 identities fails closed instead of regaining budget."""
    # Rounds 5 and 6 sit *after* the run marker (id 20), which is what the
    # previous controller produced when `_round_offset` pushed `first_round`
    # past 1. Run-local numbering would otherwise re-grant rounds 1-4 on a run
    # that has already spent two of them.
    rows = [
        {"id": 30, "body": marker("codex", 5)},
        {"id": 31, "body": marker("claude", 6)},
    ]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    for round_number in (1, 4):
        with pytest.raises(handoff.HandoffError, match="pre-1.4 round identities"):
            handoff._authorize_pass(
                SimpleNamespace(
                    repo="example/repo",
                    pr=7,
                    base="b" * 40,
                    head="a" * 40,
                    engine="codex",
                    round=round_number,
                )
            )
    # A mixed run is caught too: round 1 alone would satisfy a "starts at 1"
    # check while rounds 5 and 6 still overspend the cap.
    rows.insert(0, {"id": 29, "body": marker("codex", 1)})
    with pytest.raises(handoff.HandoffError, match="pre-1.4 round identities"):
        handoff._authorize_pass(
            SimpleNamespace(
                repo="example/repo",
                pr=7,
                base="b" * 40,
                head="a" * 40,
                engine="claude",
                round=2,
            )
        )


def test_round_equal_to_cap_authorizes(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The accepting side of the cap: `round == max_rounds` must be allowed."""
    # Seed rounds 1..cap-1 inside the run so the no-skip guard is satisfied and
    # the cap itself is the only thing under test. A `>` that became `>=` would
    # silently drop the deep cap to 3 and stays invisible to a reject-only test.
    rows: list[dict[str, object]] = [
        {"id": 30 + index, "body": marker("codex", index + 1)} for index in range(3)
    ]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base="b" * 40,
        head="a" * 40,
        engine="claude",
        round=4,
    )
    handoff._authorize_pass(args)
    assert json.loads(capsys.readouterr().out)["run_round"] == 4
    args.round = 5
    with pytest.raises(handoff.HandoffError, match="exceeds the deep cap"):
        handoff._authorize_pass(args)


@pytest.mark.parametrize(
    ("tier", "cap", "stances"),
    [
        ("lean", 2, ["adversarial", "convergence"]),
        ("deep", 4, ["adversarial", "adversarial", "convergence", "convergence"]),
    ],
)
def test_authorization_reports_the_round_stance(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tier: str,
    cap: int,
    stances: list[str],
) -> None:
    run = {
        "comment_id": 20,
        "run_id": "d" * 64,
        "base": "b" * 40,
        "tier": tier,
        "max_rounds": cap,
    }
    monkeypatch.setattr(handoff, "_run_records", lambda rows: [run])
    for round_number, stance in enumerate(stances, start=1):
        rows = [
            {"id": 30 + index, "body": marker("codex", index + 1)}
            for index in range(round_number - 1)
        ]
        monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
        handoff._authorize_pass(
            SimpleNamespace(
                repo="example/repo",
                pr=7,
                base="b" * 40,
                head="a" * 40,
                engine="claude",
                round=round_number,
            )
        )
        assert json.loads(capsys.readouterr().out)["stance"] == stance


def run_comment(
    handoff: ModuleType,
    *,
    comment_id: int,
    tier: str = "deep",
    base: str = "b" * 40,
    start_head: str = "a" * 40,
    supersedes: int | None = None,
    content: str = "Authorized run.",
    max_rounds: int | None = None,
) -> dict[str, object]:
    """Build a genuinely digest-valid run comment, as `_run_records` demands.

    The suite otherwise stubs `_run_records` everywhere, which leaves the run
    marker's own authentication unexecuted. Constructing a real marker is what
    lets a test reach those guards at all.
    """
    cap = handoff.TIER_CAPS[tier] if max_rounds is None else max_rounds
    digest = handoff._run_digest(
        tier=tier,
        max_rounds=cap,
        base=base,
        start_head=start_head,
        supersedes=supersedes,
        content=content,
    )
    supersedes_text = "none" if supersedes is None else str(supersedes)
    body = (
        f"<!-- local-review-run:v1 id={digest} tier={tier} max-rounds={cap} "
        f"base={base} start-head={start_head} supersedes={supersedes_text} "
        f"content-sha256={digest} -->\n{content}"
    )
    return {"id": comment_id, "body": body, "user": {"login": "reviewer"}}


def test_run_records_authenticates_the_marker(real_handoff: ModuleType) -> None:
    """The run marker is the root of trust; parse it for real, not from a stub."""
    valid = run_comment(real_handoff, comment_id=20)
    (record,) = real_handoff._run_records([valid])
    assert record["tier"] == "deep" and record["max_rounds"] == 4

    # A tampered digest must not open a run.
    tampered = dict(valid)
    tampered["body"] = str(valid["body"]).replace(
        "base=" + "b" * 40, "base=" + "c" * 40
    )
    with pytest.raises(
        real_handoff.HandoffError, match="run content digest is invalid"
    ):
        real_handoff._run_records([tampered])

    # A lean marker may not claim the deep cap, digest or no digest.
    mismatched = run_comment(real_handoff, comment_id=20, tier="lean", max_rounds=4)
    with pytest.raises(real_handoff.HandoffError, match="tier and cap disagree"):
        real_handoff._run_records([mismatched])

    # A body claiming to be a run marker must parse as one. (A body that merely
    # contains a marker without opening it is skipped by the prefix filter
    # rather than refused, so it is not this guard's case.)
    unparseable = dict(valid)
    unparseable["body"] = str(valid["body"]).replace("tier=deep", "tier=medium")
    with pytest.raises(real_handoff.HandoffError, match="run marker is malformed"):
        real_handoff._run_records([unparseable])


@pytest.mark.parametrize("operation", ["_next_pass", "_authorize_pass", "_authorize_fix", "_finish_run", "_post_handoff", "_show_handoff"])
def test_lifecycle_consumers_refuse_foreign_run_without_mutation(
    lifecycle_handoff: ModuleType, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], operation: str,
) -> None:
    h = lifecycle_handoff
    row = run_comment(h, comment_id=20,
                      content="<!-- local-review-sequence:v1 engines=codex,claude -->\n\nAuthorized run.")
    remote = ControllerRemote.for_controller(h)
    remote.rows = [dict(row)]  # The marker's reviewer differs from review-bot.
    before = json.dumps(remote.rows, sort_keys=True)
    def no_post(*args: Any, **kwargs: Any) -> None:
        pytest.fail("foreign lifecycle must not publish")
    monkeypatch.setattr(h, "_post_issue_comment", no_post)
    args = SimpleNamespace(repo="example/repo", pr=7, head=HEAD_SHA, base="b" * 40,
                           engine="codex", round=1, from_engine="codex", to_engine="claude",
                           outcome="aborted", context_file=None, run_id="d" * 64,
                           fingerprint="synthetic", occurrence=1)
    with pytest.raises(h.HandoffError, match="foreign-active"):
        getattr(h, operation)(args)
    assert json.dumps(remote.rows, sort_keys=True) == before
    assert capsys.readouterr().out == ""


def test_run_records_enforces_duplicate_and_supersession_rules(
    real_handoff: ModuleType,
) -> None:
    first = run_comment(real_handoff, comment_id=20)
    second = run_comment(real_handoff, comment_id=30, supersedes=20, content="Restart.")

    assert [
        record["comment_id"] for record in real_handoff._run_records([first, second])
    ] == [
        20,
        30,
    ]

    # A re-posted identical run id is an alias, not a second run.
    alias = dict(first, id=25)
    assert len(real_handoff._run_records([first, alias, second])) == 2

    # The conflicting-duplicate branch is deliberately not asserted here: the
    # digest covers the content, so a row sharing a run id with different
    # content fails the digest check first. It is defense in depth behind an
    # already-closed door, not a reachable state.

    # A run that supersedes nothing cannot follow another run.
    forked = run_comment(real_handoff, comment_id=30, content="Restart.")
    with pytest.raises(real_handoff.HandoffError, match="supersession chain"):
        real_handoff._run_records([first, forked])


@pytest.mark.parametrize(
    ("round_number", "base", "expected"),
    [
        (4, "b" * 40, None),
        (5, "b" * 40, "does not belong to the current run"),
        (1, "c" * 40, "does not belong to the current run"),
    ],
)
def test_handoff_guards_run_base_and_round_bounds(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    round_number: int,
    base: str,
    expected: str | None,
) -> None:
    """Both sides of the cap, and base agreement, for the publication guard."""
    rows: list[dict[str, object]] = []

    def post(repo: str, pr: int, marker_text: str, body: str) -> tuple[int, bool]:
        rows.append({"id": 21, "body": body, "user": {"login": "reviewer"}})
        return 21, False

    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    monkeypatch.setattr(handoff, "_post_issue_comment", post)
    monkeypatch.setattr(handoff, "_verify_handoff_run", lambda *args: None)
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base=base,
        head="a" * 40,
        from_engine="codex",
        to_engine="claude",
        round=round_number,
        outcome="clean",
        context_file=None,
    )
    if expected is None:
        handoff._post_handoff(args)
        assert json.loads(capsys.readouterr().out)["verified"] is True
        return
    with pytest.raises(handoff.HandoffError, match=expected):
        handoff._post_handoff(args)
    assert rows == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("base", "c" * 40), ("max_rounds", 2)],
)
def test_show_handoff_guards_run_base_and_round_bounds(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
) -> None:
    """The read side owns the same agreement checks as publication, not just the run id."""
    rows: list[dict[str, object]] = []
    runs: list[dict[str, object]] = [
        {
            "comment_id": 20,
            "run_id": "d" * 64,
            "base": "b" * 40,
            "tier": "deep",
            "max_rounds": 4,
        }
    ]

    def post(repo: str, pr: int, marker_text: str, body: str) -> tuple[int, bool]:
        rows.append({"id": 21, "body": body, "user": {"login": "reviewer"}})
        return 21, False

    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    monkeypatch.setattr(handoff, "_run_records", lambda _: runs)
    monkeypatch.setattr(handoff, "_post_issue_comment", post)
    monkeypatch.setattr(handoff, "_verify_handoff_run", lambda *args: None)
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base="b" * 40,
        head="a" * 40,
        from_engine="codex",
        to_engine="claude",
        round=4,
        outcome="clean",
        context_file=None,
    )
    handoff._post_handoff(args)
    capsys.readouterr()

    show = SimpleNamespace(repo="example/repo", pr=7, engine="claude")
    # The marker is digest-valid and belongs to this run id; only the field the
    # run pins has moved, so nothing but this guard can refuse it.
    handoff._show_handoff(show)
    assert json.loads(capsys.readouterr().out)["round"] == 4

    runs[0][field] = value
    with pytest.raises(
        handoff.HandoffError, match="does not belong to the current run"
    ):
        handoff._show_handoff(show)


HEAD_SHA = "a" * 40
EXPECTED_CALL = ("example/repo", 7, HEAD_SHA)


def test_authorize_pass_verifies_signed_history_at_the_reviewed_head(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verify_calls: VerifyCalls,
) -> None:
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: [])
    handoff._authorize_pass(
        SimpleNamespace(
            repo="example/repo",
            pr=7,
            base="b" * 40,
            head=HEAD_SHA,
            engine="codex",
            round=1,
        )
    )
    capsys.readouterr()
    assert ("history", EXPECTED_CALL) in verify_calls


def test_start_run_verifies_signed_history_before_and_after_publication(
    lifecycle_handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    verify_calls: VerifyCalls,
) -> None:
    handoff = lifecycle_handoff
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Explicit review authorization.\n")

    def post(*args: Any, lifecycle_write: bool = False) -> tuple[int, bool]:
        assert lifecycle_write
        verify_calls.append(("post", args))
        ControllerRemote.for_controller(handoff).rows.append({"id": 20, "body": args[-1]})
        return (20, False)

    monkeypatch.setattr(handoff, "_post_issue_comment", post)
    handoff._start_run(
        SimpleNamespace(
            repo="example/repo",
            pr=7,
            base="b" * 40,
            head=HEAD_SHA,
            tier="deep",
            restart=False,
            authorization_file=str(authorization),
        )
    )
    capsys.readouterr()
    kinds = [kind for kind, _ in verify_calls]
    posted = kinds.index("post")
    # The write window is what the surrounding checks exist to close, so the
    # ordering is the assertion: a signed-history check on each side of it.
    assert "history" in kinds[:posted]
    assert "history" in kinds[posted + 1 :]
    assert all(
        call == EXPECTED_CALL for kind, call in verify_calls if kind == "history"
    )


def test_post_handoff_verifies_signed_history_at_the_reviewed_head(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verify_calls: VerifyCalls,
) -> None:
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: [])
    monkeypatch.setattr(handoff, "_handoff_run", lambda rows: None)
    monkeypatch.setattr(handoff, "_post_issue_comment", lambda *args: (30, False))
    monkeypatch.setattr(handoff, "_verify_handoff_run", lambda *args: None)
    handoff._post_handoff(
        SimpleNamespace(
            repo="example/repo",
            pr=7,
            base="b" * 40,
            head=HEAD_SHA,
            from_engine="codex",
            to_engine="claude",
            round=1,
            outcome="clean",
            context_file=None,
        )
    )
    capsys.readouterr()
    assert ("history", EXPECTED_CALL) in verify_calls


@pytest.mark.parametrize(
    ("outcome", "expects_history"),
    [("converged", True), ("exhausted", False), ("aborted", False)],
)
def test_finish_run_verifies_signed_history_only_when_converged(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verify_calls: VerifyCalls,
    outcome: str,
    expects_history: bool,
) -> None:
    """Convergence is where an unsigned ancestor would be blessed as reviewed."""
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: [])
    monkeypatch.setattr(handoff, "_post_issue_comment", lambda *args: (40, False))
    ledger_checks: list[str] = []
    monkeypatch.setattr(
        handoff,
        "_verify_convergence_ledger",
        lambda args: ledger_checks.append(args.head),
    )
    handoff._finish_run(
        SimpleNamespace(repo="example/repo", pr=7, head=HEAD_SHA, outcome=outcome)
    )
    capsys.readouterr()
    kinds = [kind for kind, _ in verify_calls]
    # Two verification points: one before the run-end marker is posted and one
    # after. The replay branch's third is unreachable here because `_run_end`
    # reports no existing terminal marker.
    assert kinds.count("head") == 2
    assert kinds.count("history") == (2 if expects_history else 0)
    assert all(call == EXPECTED_CALL for _, call in verify_calls)
    assert ledger_checks == ([HEAD_SHA] if outcome == "converged" else [])


@pytest.mark.parametrize('conflict', [None, 'authorization', 'terminal', 'parent', 'unended', 'converged'])
def test_aborted_restart_replays_only_its_matching_successor(
    lifecycle_handoff: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str], conflict: str | None,
) -> None:
    h = lifecycle_handoff
    authorization = tmp_path / 'authorization.txt'
    authorization.write_text('Explicit new budget for synthetic review.')
    content = '<!-- local-review-plan:v1 mode=chain engines=codex,claude -->\n\n' + authorization.read_text()
    parent = run_comment(h, comment_id=1)
    successor = run_comment(h, comment_id=3,
                            supersedes=2 if conflict == 'parent' else 1,
                            content='Other authorization' if conflict == 'authorization' else content)
    parent['user'] = successor['user'] = {'login': 'review-bot'}
    parent_id = str(parent['body']).split('id=', 1)[1].split(' ', 1)[0]
    successor_id = str(successor['body']).split('id=', 1)[1].split(' ', 1)[0]
    remote = ControllerRemote.for_controller(h)
    remote.rows = [dict(parent)]
    if conflict != 'unended':
        outcome = 'converged' if conflict == 'converged' else 'aborted'
        remote.rows.append({'id': 2, 'body': f'<!-- local-review-run-end:v1 id={parent_id} outcome={outcome} head={HEAD_SHA} -->'})
    remote.rows.append(dict(successor))
    if conflict == 'terminal':
        remote.rows.append({'id': 4, 'body': f'<!-- local-review-run-end:v1 id={successor_id} outcome=exhausted head={HEAD_SHA} -->'})
    before = json.dumps(remote.rows, sort_keys=True)
    def unexpected_post(*args: Any, **kwargs: Any) -> None:
        pytest.fail('repeated restart must not post another run')
    monkeypatch.setattr(h, '_post_issue_comment', unexpected_post)
    args = SimpleNamespace(repo='example/repo', pr=1, head='a' * 40, base='b' * 40,
                           tier='deep', restart=True, restart_from_run=parent_id,
                           chain='codex,claude', authorization_file=str(authorization))
    if conflict:
        # Invalid chains now fail at the earlier shared lifecycle gate.
        message = ('lifecycle' if conflict in ('parent', 'unended') else
                   'aborted outcome' if conflict == 'converged' else 'successor')
        with pytest.raises(h.HandoffError, match=message):
            h._start_run(args)
    else:
        h._start_run(args)
        result = json.loads(capsys.readouterr().out)
        assert result['run_id'] == successor_id
        assert result['replayed'] is True
    assert json.dumps(remote.rows, sort_keys=True) == before
