"""Regression tests for deterministic cross-engine review handoffs."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest


HEAD = "a" * 40
BASE = "b" * 40
OTHER_HEAD = "d" * 40
REPO = "example/repository"


def _row(comment_id: int, body: str, *, login: str = "reviewer") -> dict[str, Any]:
    return {"id": comment_id, "body": body, "user": {"login": login}}


@pytest.fixture(scope="session")
def handoff() -> ModuleType:
    path = (
        Path(__file__).resolve().parent.parent
        / ".codex/skills/critique/scripts/local-review-handoff.py"
    )
    spec = importlib.util.spec_from_file_location("local_review_handoff", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = ModuleType("local_review_handoff")
    sys.modules["local_review_handoff"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def authenticated_actor(handoff: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handoff, "_current_actor", lambda: "reviewer")


def _body(handoff: ModuleType, from_engine: str, to_engine: str) -> str:
    args = SimpleNamespace(
        base=BASE,
        from_engine=from_engine,
        head=HEAD,
        outcome="clean",
        pr=7,
        repo=REPO,
        round=1,
        to_engine=to_engine,
    )
    content = handoff._handoff_content(args, "")
    digest = handoff._handoff_digest(
        from_engine=from_engine,
        to_engine=to_engine,
        round_number=1,
        base=BASE,
        head=HEAD,
        outcome="clean",
        content=content,
    )
    marker = (
        f"<!-- local-review-handoff:v1 from={from_engine} to={to_engine} "
        f"round=1 base={BASE} head={HEAD} outcome=clean content-sha256={digest} -->"
    )
    return f"{marker}\n{content}"


def _run_body(
    handoff: ModuleType,
    *,
    tier: str = "deep",
    supersedes: int | None = None,
    content: str = "Review explicitly authorized.",
) -> str:
    max_rounds = handoff.TIER_CAPS[tier]
    digest = handoff._run_digest(
        tier=tier,
        max_rounds=max_rounds,
        base=BASE,
        start_head=HEAD,
        supersedes=supersedes,
        content=content,
    )
    marker = (
        f"<!-- local-review-run:v1 id={digest} tier={tier} "
        f"max-rounds={max_rounds} base={BASE} start-head={HEAD} "
        f"supersedes={supersedes if supersedes is not None else 'none'} "
        f"content-sha256={digest} -->"
    )
    return f"{marker}\n{content}"


def test_post_handoff_builds_prompt_and_replays(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    posted: list[dict[str, Any]] = []
    stored: list[dict[str, Any]] = []

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if args[-1] == f"repos/{REPO}/issues/7/comments?per_page=100":
            return json.dumps([stored])
        if args[-1] == f"repos/{REPO}/issues/7/comments":
            assert payload is not None
            posted.append(payload)
            stored.append(_row(77, cast(str, payload["body"])))
            return json.dumps({"id": 77})
        if args[-1] == f"repos/{REPO}/issues/comments/77":
            return json.dumps(stored[0])
        raise AssertionError(args)

    monkeypatch.setattr(handoff, "_run_gh", fake_gh)
    command = [
        "post-handoff",
        "--repo",
        REPO,
        "--pr",
        "7",
        "--head",
        HEAD,
        "--base",
        BASE,
        "--from-engine",
        "codex",
        "--to-engine",
        "claude",
        "--round",
        "2",
        "--outcome",
        "material",
    ]
    assert handoff.main(command) == 0
    assert json.loads(capsys.readouterr().out)["replayed"] is False
    body = cast(str, posted[0]["body"])
    assert "Continue review on PR #7." in body
    assert "Do not invoke the other review" in body
    assert "If they satisfy the repository's convergence" in body
    assert f"base={BASE} head={HEAD}" in body

    assert handoff.main(command) == 0
    assert json.loads(capsys.readouterr().out)["replayed"] is True
    assert len(posted) == 1


def test_authorize_pass_enforces_run_cap_and_duplicate_passes(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        _row(20, _run_body(handoff)),
        _row(
            21,
            f"<!-- local-review-pass:v3 engine=codex round=1 base={BASE} "
            f"head={HEAD} result-sha256={'c' * 64} -->",
        ),
    ]
    monkeypatch.setattr(handoff, "_issue_comments", lambda repo, pr: rows)
    monkeypatch.setattr(handoff, "_verify_head", lambda repo, pr, head: None)

    assert (
        handoff.main(
            [
                "authorize-pass",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--base",
                BASE,
                "--head",
                HEAD,
                "--engine",
                "claude",
                "--round",
                "1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["max_rounds"] == 4

    with pytest.raises(handoff.HandoffError, match="already completed"):
        handoff.main(
            [
                "authorize-pass",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--base",
                BASE,
                "--head",
                HEAD,
                "--engine",
                "codex",
                "--round",
                "1",
            ]
        )
    with pytest.raises(handoff.HandoffError, match="exceeds the deep cap"):
        handoff.main(
            [
                "authorize-pass",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--base",
                BASE,
                "--head",
                HEAD,
                "--engine",
                "codex",
                "--round",
                "5",
            ]
        )


def test_restart_requires_terminal_prior_run(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Explicit restart authorization.\n", encoding="utf-8")
    rows = [_row(20, _run_body(handoff))]
    monkeypatch.setattr(handoff, "_issue_comments", lambda repo, pr: rows)
    monkeypatch.setattr(handoff, "_verify_head", lambda repo, pr, head: None)

    with pytest.raises(handoff.HandoffError, match="must be ended"):
        handoff.main(
            [
                "start-run",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--base",
                BASE,
                "--head",
                HEAD,
                "--tier",
                "deep",
                "--authorization-file",
                str(authorization),
                "--restart",
            ]
        )


def test_ended_run_rejects_another_pass(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_body = _run_body(handoff, tier="lean")
    run_id = handoff.RUN_V1_RE.search(run_body).group("run_id")
    rows = [
        _row(20, run_body),
        _row(
            21,
            f"<!-- local-review-run-end:v1 id={run_id} outcome=exhausted "
            f"head={HEAD} -->",
        ),
    ]
    monkeypatch.setattr(handoff, "_issue_comments", lambda repo, pr: rows)
    with pytest.raises(handoff.HandoffError, match="has ended"):
        handoff.main(
            [
                "authorize-pass",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--base",
                BASE,
                "--head",
                HEAD,
                "--engine",
                "codex",
                "--round",
                "1",
            ]
        )


def test_show_handoff_uses_latest_authenticated_comment(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        _row(10, _body(handoff, "claude", "codex")),
        _row(11, _body(handoff, "codex", "claude")),
        _row(12, _body(handoff, "claude", "codex"), login="other"),
    ]

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        assert payload is None
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        return json.dumps([rows])

    monkeypatch.setattr(handoff, "_run_gh", fake_gh)
    command = ["show-handoff", "--repo", REPO, "--pr", "7", "--engine", "claude"]
    assert handoff.main(command) == 0
    assert json.loads(capsys.readouterr().out)["comment_id"] == 11
    with pytest.raises(handoff.HandoffError, match="targets claude, not codex"):
        handoff.main(["show-handoff", "--repo", REPO, "--pr", "7", "--engine", "codex"])


def test_show_handoff_rejects_stale_head(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row(11, _body(handoff, "codex", "claude"))

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        assert payload is None
        if args[:2] == ["pr", "view"]:
            return OTHER_HEAD + "\n"
        return json.dumps([[row]])

    monkeypatch.setattr(handoff, "_run_gh", fake_gh)
    with pytest.raises(handoff.HandoffError, match="PR head mismatch"):
        handoff.main(
            ["show-handoff", "--repo", REPO, "--pr", "7", "--engine", "claude"]
        )


def test_show_handoff_rejects_tampered_marker_metadata(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    tampered = _body(handoff, "codex", "claude").replace(
        f"head={HEAD}", f"head={OTHER_HEAD}", 1
    )

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        assert payload is None
        if args[:2] == ["pr", "view"]:
            return OTHER_HEAD + "\n"
        return json.dumps([[_row(11, tampered)]])

    monkeypatch.setattr(handoff, "_run_gh", fake_gh)
    with pytest.raises(handoff.HandoffError, match="content digest is invalid"):
        handoff.main(
            ["show-handoff", "--repo", REPO, "--pr", "7", "--engine", "claude"]
        )


def test_show_handoff_rejects_malformed_newest_marker(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        _row(10, _body(handoff, "claude", "codex")),
        _row(11, "<!-- local-review-handoff:v1 malformed -->\nnewer"),
    ]
    monkeypatch.setattr(handoff, "_issue_comments", lambda repo, pr: rows)
    with pytest.raises(handoff.HandoffError, match="marker is malformed"):
        handoff.main(["show-handoff", "--repo", REPO, "--pr", "7", "--engine", "codex"])


def test_post_handoff_rejects_concurrent_duplicate(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored: list[dict[str, Any]] = []
    comment_lists = 0

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        nonlocal comment_lists
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if args[-1] == f"repos/{REPO}/issues/7/comments?per_page=100":
            comment_lists += 1
            if comment_lists == 1:
                return json.dumps([[]])
            duplicate = _row(78, cast(str, stored[0]["body"]))
            return json.dumps([[stored[0], duplicate]])
        if args[-1] == f"repos/{REPO}/issues/7/comments":
            assert payload is not None
            stored.append(_row(77, cast(str, payload["body"])))
            return json.dumps({"id": 77})
        if args[-1] == f"repos/{REPO}/issues/comments/77":
            return json.dumps(stored[0])
        raise AssertionError(args)

    monkeypatch.setattr(handoff, "_run_gh", fake_gh)
    with pytest.raises(handoff.HandoffError, match="idempotency key is duplicated"):
        handoff.main(
            [
                "post-handoff",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                HEAD,
                "--base",
                BASE,
                "--from-engine",
                "codex",
                "--to-engine",
                "claude",
                "--round",
                "1",
                "--outcome",
                "clean",
            ]
        )


def test_context_rejects_marker_injection(handoff: ModuleType, tmp_path: Path) -> None:
    context = tmp_path / "context.md"
    context.write_text("<!-- local-review-handoff:v1 injected -->", encoding="utf-8")
    with pytest.raises(handoff.HandoffError, match="must not contain"):
        handoff._read_context(str(context))
