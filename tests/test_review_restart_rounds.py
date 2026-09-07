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
    tampered["body"] = str(valid["body"]).replace("base=" + "b" * 40, "base=" + "c" * 40)
    with pytest.raises(real_handoff.HandoffError, match="run content digest is invalid"):
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


def test_run_records_enforces_duplicate_and_supersession_rules(
    real_handoff: ModuleType,
) -> None:
    first = run_comment(real_handoff, comment_id=20)
    second = run_comment(real_handoff, comment_id=30, supersedes=20, content="Restart.")

    assert [record["comment_id"] for record in real_handoff._run_records([first, second])] == [
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
    with pytest.raises(handoff.HandoffError, match="does not belong to the current run"):
        handoff._show_handoff(show)
