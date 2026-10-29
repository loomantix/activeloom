"""Offline owner-transition authority and uncertain-publication contracts."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40
RUN = "b" * 64
SCOPE = dict(repo="example/project", pr=7, head=HEAD, run_id=RUN,
             run_comment_id=10, from_actor="old-owner", to_actor="new-owner")


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    scripts = tmp_path / "skills/critique/scripts"
    scripts.mkdir(parents=True)
    other = tmp_path / "skills/hreview/scripts"
    other.mkdir(parents=True)
    shutil.copyfile(ROOT / "packages/review-ledger/protocol/local-review-handoff.py", scripts / "local-review-handoff.py")
    shutil.copyfile(ROOT / "prompts/skills/hreview/scripts/herd-config.py", other / "herd-config.py")
    controller = load("transition_controller_test", scripts / "local-review-handoff.py")
    approval = load("transition_approval_test", other / "herd-config.py")
    return controller, approval


def approve(helper: ModuleType) -> dict[str, Any]:
    phrase = f"transition 7 10 old-owner to new-owner at {HEAD}"
    return helper.approve_transition(**SCOPE, ttl_hours=4, confirm=phrase)  # type: ignore[no-any-return]


def test_transition_requires_human_confirmation_and_cannot_replace_approval(helpers: tuple[ModuleType, ModuleType]) -> None:
    _, helper = helpers
    with pytest.raises(helper.UsageError):
        helper.approve_transition(**SCOPE, ttl_hours=4, confirm="take over")
    record = approve(helper)
    with pytest.raises(helper.ApprovalError, match="already exists"):
        approve(helper)
    assert json.loads(Path(record["path"]).read_text())["used"] is None


def test_invalid_scope_does_not_consume_and_expired_intent_only_reconciles(
    helpers: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, helper = helpers
    record = approve(helper)
    with pytest.raises(helper.ApprovalError, match="scope"):
        helper.transition_transaction({**SCOPE, "to_actor": "third-party"}, record["id"], lambda *_: None)
    assert json.loads(Path(record["path"]).read_text())["intent"] is None

    def uncertain(binding: dict[str, Any], reserve: Any) -> None:
        reserve("c" * 64)
        raise OSError("synthetic lost response")

    with pytest.raises(OSError):
        helper.transition_transaction(SCOPE, record["id"], uncertain)
    later = helper._now() + timedelta(hours=5)
    monkeypatch.setattr(helper, "_now", lambda: later)
    with pytest.raises(helper.ApprovalError, match="expired"):
        helper.transition_transaction(SCOPE, record["id"], lambda binding, reserve: reserve("c" * 64))
    assert helper.transition_transaction(SCOPE, record["id"], lambda *_: "existing") == "existing"


def test_human_can_renew_expired_unused_transition(
    helpers: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, helper = helpers
    first = approve(helper)
    later = helper._now() + timedelta(hours=5)
    monkeypatch.setattr(helper, "_now", lambda: later)
    renewed = approve(helper)
    assert renewed["id"] != first["id"]
    assert renewed["binding"]["created"] == helper._iso(later)
    assert renewed["intent"] is None and renewed["used"] is None
    with pytest.raises(helper.ApprovalError, match="ID"):
        helper.transition_transaction(SCOPE, first["id"], lambda *_: None)
    helper.transition_transaction(SCOPE, renewed["id"], lambda binding, reserve: reserve("c" * 64))
    assert json.loads(Path(renewed["path"]).read_text())["used"]


@pytest.mark.parametrize("consumed", [False, True])
def test_renewal_preserves_expired_pending_or_consumed_transition(
    helpers: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch, consumed: bool,
) -> None:
    _, helper = helpers
    first = approve(helper)

    def publish(binding: dict[str, Any], reserve: Any) -> None:
        reserve("c" * 64)
        if not consumed:
            raise OSError("synthetic uncertain publication")

    if consumed:
        helper.transition_transaction(SCOPE, first["id"], publish)
    else:
        with pytest.raises(OSError):
            helper.transition_transaction(SCOPE, first["id"], publish)
    before = Path(first["path"]).read_bytes()
    later = helper._now() + timedelta(hours=5)
    monkeypatch.setattr(helper, "_now", lambda: later)
    with pytest.raises(helper.ApprovalError, match="already exists"):
        approve(helper)
    assert Path(first["path"]).read_bytes() == before
    assert helper.transition_transaction(SCOPE, first["id"], lambda *_: "existing") == "existing"


@pytest.mark.parametrize("state", ["foreign-active", "foreign-ended", "foreign-transition"])
def test_start_cannot_hide_foreign_history(helpers: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    controller, _ = helpers
    monkeypatch.setattr(controller, "_verify_live_head", lambda *_: None)
    monkeypatch.setattr(controller, "_lifecycle", lambda *_: {"state": state})
    with pytest.raises(controller.HandoffError, match="foreign"):
        controller._start_lifecycle(argparse.Namespace(repo=SCOPE["repo"], pr=7, head=HEAD, restart=True))


def test_transition_is_not_restart_or_scope_approval(helpers: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch) -> None:
    controller, _ = helpers
    state = {"state": "transitioned", "transition": {"id": "c" * 64, "binding": {"head": HEAD}}}
    monkeypatch.setattr(controller, "_verify_live_head", lambda *_: None)
    monkeypatch.setattr(controller, "_lifecycle", lambda *_: state)
    fields = dict(repo=SCOPE["repo"], pr=7, head=HEAD, restart=True, scope_decision="keep", owner_transition_id="c" * 64)
    for changed in ({"restart": False}, {"scope_decision": None}, {"owner_transition_id": "d" * 64}, {"head": "e" * 40}):
        with pytest.raises(controller.HandoffError):
            controller._start_lifecycle(argparse.Namespace(**{**fields, **changed}))
    assert controller._start_lifecycle(argparse.Namespace(**fields)) == (state, "c" * 64)


def test_controller_reconciles_transition_after_uncertain_write(
    helpers: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    controller, helper = helpers
    record = approve(helper)
    args = argparse.Namespace(**SCOPE, approval_id=record["id"])
    state: dict[str, Any] = {"state": "foreign-active", "actor": "new-owner",
                             "run": {"id": RUN, "commentId": 10, "actor": "old-owner"}, "transitions": []}
    monkeypatch.setattr(controller, "_current_actor", lambda: "new-owner")
    monkeypatch.setattr(controller, "_verify_live_head", lambda *_: None)
    monkeypatch.setattr(controller, "_verify_issue_comment", lambda *_: None)
    monkeypatch.setattr(controller, "_lifecycle", lambda *_: json.loads(json.dumps(state)))
    writes: list[str] = []

    def publish(repo: str, pr: int, marker: str, body: str, **kwargs: Any) -> None:
        identifier = marker.split("id=", 1)[1].split(" ", 1)[0]
        transition = {"id": identifier, "commentId": 20, "binding": json.loads(body.split("\n", 1)[1])}
        state.update(state="transitioned", transition=transition, transitions=[transition])
        writes.append(body)
        raise controller.HandoffError("synthetic uncertain publication")

    monkeypatch.setattr(controller, "_post_issue_comment", publish)
    with pytest.raises(controller.HandoffError):
        controller._transition_run(args)
    assert json.loads(Path(record["path"]).read_text())["used"] is None
    controller._transition_run(args)
    result = json.loads(capsys.readouterr().out)
    assert result["replayed"] is True and result["restart_authorized"] is False
    assert len(writes) == 1
    assert json.loads(Path(record["path"]).read_text())["used"]
