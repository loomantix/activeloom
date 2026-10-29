"""Offline restart transaction recovery without touching a live approval or PR."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
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

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.usefixtures("offline_github")
spec = importlib.util.spec_from_file_location("transaction_herd", ROOT / "prompts/skills/hreview/scripts/herd-config.py")
assert spec and spec.loader
herd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(herd)
HEAD = "a" * 40
FINGERPRINT = "b" * 64
RUN = "c" * 64


@pytest.fixture
def approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return herd.approve_restart("acme/widgets", 7, HEAD, 4, False, None, "restart 7")  # type: ignore[no-any-return]


def start(approval: dict[str, Any], operation: Any, fingerprint: str = FINGERPRINT) -> Any:
    return herd.restart_transaction("acme/widgets", 7, HEAD, approval["id"], fingerprint, operation)


def test_prepublication_validation_failure_preserves_approval(approval: dict[str, Any]) -> None:
    def invalid(record: Any, reserve: Any) -> None:
        raise ValueError("invalid scope")

    with pytest.raises(ValueError):
        start(approval, invalid)
    assert herd.check_restart("acme/widgets", 7, HEAD)["used"] is None


def test_uncertain_publish_reconciles_one_run(approval: dict[str, Any]) -> None:
    remote: list[str] = []

    def publish(record: Any, reserve: Any) -> None:
        reserve(RUN)
        remote.append(RUN)
        raise OSError("response lost")

    with pytest.raises(OSError):
        start(approval, publish)
    with pytest.raises(herd.ApprovalError, match="pending"):
        herd.claim_restart("acme/widgets", 7, HEAD)
    assert start(approval, lambda record, reserve: remote[0]) == RUN
    assert start(approval, lambda record, reserve: remote[0]) == RUN
    assert remote == [RUN]
    assert json.loads(Path(approval["path"]).read_text())["used"]


def test_uncertain_publish_cannot_change_arguments(approval: dict[str, Any]) -> None:
    def publish(record: Any, reserve: Any) -> None:
        reserve(RUN)
        raise OSError("connection lost")

    with pytest.raises(OSError):
        start(approval, publish)
    with pytest.raises(herd.ApprovalError, match="arguments differ"):
        start(approval, lambda record, reserve: None, "d" * 64)


def test_consumed_transaction_cannot_publish_again(approval: dict[str, Any]) -> None:
    start(approval, lambda record, reserve: reserve(RUN))
    with pytest.raises(herd.ApprovalError, match="consumed"):
        start(approval, lambda record, reserve: reserve(RUN))


def test_expired_intent_can_only_reconcile(approval: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def publish(record: Any, reserve: Any) -> None:
        reserve(RUN)
        raise OSError("uncertain")

    with pytest.raises(OSError):
        start(approval, publish)
    later = herd._now() + timedelta(hours=5)
    monkeypatch.setattr(herd, "_now", lambda: later)
    with pytest.raises(herd.ApprovalError, match="expired"):
        start(approval, lambda record, reserve: reserve(RUN))
    assert start(approval, lambda record, reserve: RUN) == RUN


def test_different_run_is_refused(approval: dict[str, Any]) -> None:
    def publish(record: Any, reserve: Any) -> None:
        reserve(RUN)
        raise OSError("uncertain")

    with pytest.raises(OSError):
        start(approval, publish)
    with pytest.raises(herd.ApprovalError, match="different run"):
        start(approval, lambda record, reserve: reserve("e" * 64))


def test_concurrent_starters_publish_once(approval: dict[str, Any], tmp_path: Path) -> None:
    """Independent processes reconcile the same publication under the file lock."""
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    results = context.Queue()
    remote = tmp_path / "published-run"

    def worker() -> None:
        ready.wait(3)

        def publish(record: Any, reserve: Any) -> str:
            if remote.exists():
                return remote.read_text()
            reserve(RUN)
            # Exclusive creation makes an accidental duplicate observable.
            with remote.open("x") as stream:
                stream.write(RUN)
            return RUN

        try:
            results.put(start(approval, publish))
        except Exception as error:
            results.put(type(error).__name__)

    workers = [context.Process(target=worker) for _ in range(2)]
    try:
        for process in workers:
            process.start()
        ready.set()
        for process in workers:
            process.join(5)
            assert process.exitcode == 0
        assert [results.get(timeout=1) for _ in workers] == [RUN, RUN]
        assert remote.read_text() == RUN
        assert json.loads(Path(approval["path"]).read_text())["used"]
    finally:
        for process in workers:
            if process.is_alive():
                process.terminate()
                process.join(1)
        results.close()


def test_controller_recovers_uncertain_publication(
    approval: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise installed helper lookup and real controller replay with fake remote IO."""
    path = ROOT / ".codex/skills/critique/scripts/local-review-handoff.py"
    controller_spec = importlib.util.spec_from_file_location("transaction_controller", path)
    assert controller_spec and controller_spec.loader
    controller = importlib.util.module_from_spec(controller_spec)
    controller_spec.loader.exec_module(controller)
    remote = ControllerRemote(controller, monkeypatch, tmp_path)
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Explicit synthetic restart authorization.")
    rows: list[dict[str, Any]] = []
    publications: list[str] = []
    remote.rows = rows
    monkeypatch.setattr(controller, "_verify_signed_pr_history", lambda *_: None)
    monkeypatch.setattr(controller, "_scope_signals", lambda *_: {
        "behaviour_commits": 0, "missing_patch_files": [],
    })

    def publish(repo: str, pr: int, marker: str, body: str, *, lifecycle_write: bool = False) -> None:
        assert lifecycle_write
        record = json.loads(Path(approval["path"]).read_text())
        run_id = record["intent"]["run_id"]
        publications.append(run_id)
        rows.append({"id": 20, "body": body})
        raise OSError("response lost after publication")

    monkeypatch.setattr(controller, "_post_issue_comment", publish)

    def arguments() -> SimpleNamespace:
        return SimpleNamespace(repo="acme/widgets", pr=7, head=HEAD, base="f" * 40,
                               tier="lean", restart=True,
                               restart_approval_id=approval["id"],
                               authorization_file=str(authorization))

    with pytest.raises(OSError):
        controller._start_run(arguments())
    assert capsys.readouterr().out == ""
    assert json.loads(Path(approval["path"]).read_text())["used"] is None
    controller._start_run(arguments())
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] is True
    assert result["replayed"] is True
    assert publications == [result["run_id"]]
    assert json.loads(Path(approval["path"]).read_text())["used"]
