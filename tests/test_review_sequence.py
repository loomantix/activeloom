"""Alternating chains require actual ordered, run-local engine participation."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

HEAD = "a" * 40
BASE = "b" * 40


@pytest.fixture
def controller() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / ".codex/skills/critique/scripts/local-review-handoff.py"
    )
    spec = importlib.util.spec_from_file_location("sequence_controller", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run() -> dict[str, Any]:
    return {
        "comment_id": 20,
        "run_id": "d" * 64,
        "base": BASE,
        "start_head": HEAD,
        "tier": "deep",
        "max_rounds": 4,
        "sequence": ["codex", "claude"],
    }


def event(
    index: int,
    *,
    engine: str | None = None,
    head: str = HEAD,
    classification: str = "clean",
) -> dict[str, Any]:
    engine = engine or ("codex" if index % 2 == 0 else "claude")
    fields = f"engine={engine} round={index // 2 + 1} base={BASE} "
    if classification == "clean":
        marker = f"<!-- local-review-pass:v3 {fields}head={head} result-sha256={'c' * 64} -->"
    else:
        marker = (
            f"<!-- local-review-complete:v3 {fields}before={HEAD} head={head} "
            f"classification={classification} fingerprints=x result-sha256={'c' * 64} -->"
        )
    return {"id": 21 + index, "body": marker}


def wire(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
) -> list[str]:
    posted: list[str] = []
    monkeypatch.setattr(controller, "_issue_comments", lambda *_: rows)
    monkeypatch.setattr(controller, "_run_records", lambda _: [run()])
    monkeypatch.setattr(controller, "_verify_head", lambda *_: None)
    def post(*args: Any) -> tuple[int, bool]:
        posted.append(args[-1])
        return 99, False

    monkeypatch.setattr(controller, "_post_issue_comment", post)
    return posted


@pytest.mark.parametrize(
    "count, status, engine, number",
    [
        (0, "next", "codex", 1),
        (1, "next", "claude", 1),
        (2, "next", "codex", 2),
        (3, "converged", None, None),
    ],
)
def test_clean_chain_returns_to_initiator(
    controller: ModuleType,
    count: int,
    status: str,
    engine: str | None,
    number: int | None,
) -> None:
    decision = controller._sequence_decision(
        [event(i) for i in range(count)], run(), HEAD
    )
    assert decision["status"] == status
    assert decision.get("engine") == engine
    assert decision.get("round") == number
    assert len(decision["passes"]) == count


def test_solo_pass_cannot_replace_other_engine(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire(controller, monkeypatch, [event(0)])
    args = SimpleNamespace(
        repo="example/repo", pr=1, base=BASE, head=HEAD, engine="codex", round=2
    )
    with pytest.raises(controller.HandoffError, match="claude round 1"):
        controller._authorize_pass(args)
    args.engine, args.round = "claude", 1
    controller._authorize_pass(args)


def test_material_fix_requires_clean_followup(controller: ModuleType) -> None:
    rows = [event(0), event(1, classification="material"), event(2)]
    assert controller._sequence_decision(rows, run(), HEAD)["engine"] == "claude"
    rows.extend([event(3), event(4)])
    assert controller._sequence_decision(rows, run(), HEAD)["status"] == "converged"


def test_stale_heads_and_cap(controller: ModuleType) -> None:
    rows = [event(i) for i in range(8)]
    decision = controller._sequence_decision(rows, run(), "e" * 40)
    assert decision["status"] == "exhausted"
    assert len(decision["passes"]) == 8


def test_historical_and_duplicate_passes_do_not_count(controller: ModuleType) -> None:
    historical = {**event(7), "id": 19}
    rows = [historical, event(0), {**event(0), "id": 25}]
    decision = controller._sequence_decision(rows, run(), HEAD)
    assert len(decision["passes"]) == 1
    assert decision["engine"] == "claude"


@pytest.mark.parametrize(
    "rows, message",
    [
        ([event(0, engine="claude")], "violate"),
        ([event(0), event(2)], "violate"),
        ([event(0), {**event(0, head="e" * 40), "id": 25}], "conflicting"),
        ([{"id": 21, "body": "<!-- local-review-pass:v2 malformed -->"}], "malformed"),
        (
            [{"id": 21, "body": event(0)["body"] + "\n" + event(1)["body"]}],
            "exactly one",
        ),
        (
            [{"id": 21, "body": event(0)["body"].replace(BASE, "f" * 40)}],
            "different base",
        ),
    ],
)
def test_invalid_evidence_fails_closed(
    controller: ModuleType, rows: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(controller.HandoffError, match=message):
        controller._sequence_decision(rows, run(), HEAD)


def test_finish_checks_sequence_then_ledger_before_posting(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [event(0), event(1)]
    posted = wire(controller, monkeypatch, rows)
    args = SimpleNamespace(repo="example/repo", pr=1, head=HEAD, outcome="converged")
    with pytest.raises(controller.HandoffError, match="has not converged"):
        controller._finish_run(args)
    assert not posted
    rows.append(event(2))
    commands: list[str] = []

    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command[2])
        return subprocess.CompletedProcess(command, 1, "", "unresolved thread")

    monkeypatch.setattr(controller.subprocess, "run", execute)
    with pytest.raises(controller.HandoffError, match="verify-ledger refused"):
        controller._finish_run(args)
    assert not posted
    monkeypatch.setattr(
        controller.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    controller._finish_run(args)
    assert len(posted) == 1


def test_next_pass_checks_live_head(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wire(controller, monkeypatch, [event(0)])
    args = SimpleNamespace(repo="example/repo", pr=1, head=HEAD)
    controller._next_pass(args)
    assert json.loads(capsys.readouterr().out)["engine"] == "claude"
    monkeypatch.setattr(
        controller, "_verify_head", lambda *_: controller._fail("head moved")
    )
    with pytest.raises(controller.HandoffError, match="head moved"):
        controller._next_pass(args)
    assert not capsys.readouterr().out


def test_start_binds_sequence_to_run_digest(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Review the scoped change.")
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(controller, "_issue_comments", lambda *_: [])
    monkeypatch.setattr(controller, "_verify_head", lambda *_: None)
    def post(*args: Any) -> tuple[int, bool]:
        posted.append({"id": 20, "body": args[-1]})
        return 20, False

    monkeypatch.setattr(controller, "_post_issue_comment", post)
    args = controller._parser().parse_args(
        [
            "start-run",
            "--repo",
            "example/repo",
            "--pr",
            "1",
            "--head",
            HEAD,
            "--base",
            BASE,
            "--tier",
            "deep",
            "--authorization-file",
            str(authorization),
            "--sequence",
            "codex,claude",
        ]
    )
    args.handler(args)
    assert controller._run_records(posted)[0]["sequence"] == ["codex", "claude"]
    posted[0]["body"] = posted[0]["body"].replace("codex,claude", "claude,codex")
    with pytest.raises(controller.HandoffError, match="digest"):
        controller._run_records(posted)


@pytest.mark.parametrize(
    "value", ["codex", "codex,codex", "codex,unknown", "codex, claude"]
)
def test_invalid_sequences(controller: ModuleType, value: str) -> None:
    with pytest.raises(controller.HandoffError):
        controller._parse_sequence(value)


def test_legacy_run_needs_explicit_restart_for_sequence(controller: ModuleType) -> None:
    legacy = {**run(), "sequence": None}
    with pytest.raises(controller.HandoffError, match="no engine sequence"):
        controller._sequence_decision([], legacy, HEAD)
