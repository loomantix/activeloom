"""Contract for horch's completion-event inbox (`horch/scripts/completion-events.py`)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "prompts/skills/horch/scripts/completion-events.py"
ROOTS = [ROOT / root / "skills/horch/scripts/completion-events.py" for root in (".claude", ".codex", ".agents")]
REPO = "example-org/example-repo"
HEAD = "a" * 40


def load(path: Path = SOURCE) -> ModuleType:
    spec = importlib.util.spec_from_file_location("horch_completion_events", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ce = load()


def run(script: Path, inbox: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(script), "--inbox", str(inbox), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def build_args(pr: int = 7, outcome: str = "pr-opened") -> list[str]:
    return ["record", "--kind", "build", "--repo", REPO, "--issue", "3", "--pr", str(pr),
            "--head", HEAD, "--outcome", outcome, "--source", "i3-build"]


@pytest.mark.parametrize("script", ROOTS, ids=lambda p: p.parts[-5])
def test_every_harness_root_ships_the_source_and_round_trips(script: Path, tmp_path: Path) -> None:
    assert script.read_bytes() == SOURCE.read_bytes()
    assert os.access(script, os.X_OK)
    inbox = tmp_path / "events"
    recorded = run(script, inbox, *build_args())
    assert recorded.returncode == 0, recorded.stdout
    eid = json.loads(recorded.stdout)["id"]
    assert json.loads(recorded.stdout)["status"] == "recorded"

    pending = json.loads(run(script, inbox, "pending").stdout)
    assert [event["id"] for event in pending["events"]] == [eid]
    assert pending["events"][0]["pr"] == 7 and pending["events"][0]["head"] == HEAD

    woke = run(script, inbox, "wait", "--timeout", "5", "--interval", "0.1")
    assert woke.returncode == 0 and json.loads(woke.stdout)["events"][0]["id"] == eid

    acked = run(script, inbox, "ack", "--id", eid)
    assert json.loads(acked.stdout) == {"id": eid, "status": "acknowledged"}
    assert json.loads(run(script, inbox, "pending").stdout) == {"events": [], "invalid": 0}


def test_event_is_durable_before_it_is_visible_to_waiters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = ce.Inbox(tmp_path / "events")
    order: list[str] = []
    real_fsync, real_link = os.fsync, os.link

    def fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    def link(src: str, dst: os.PathLike[str] | str) -> None:
        # The source is complete and parseable at the moment it becomes visible.
        assert ce.validate(json.loads(Path(src).read_text()))
        order.append("link")
        real_link(src, dst)

    monkeypatch.setattr(ce.os, "fsync", fsync)
    monkeypatch.setattr(ce.os, "link", link)
    event = ce.build_event(kind="build", repo=REPO, outcome="pr-opened", issue=3, pr=7, head=HEAD)
    assert inbox.record(event) == "recorded"
    assert order.index("fsync") < order.index("link")


def test_failed_persistence_never_wakes_a_waiter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = ce.Inbox(tmp_path / "events")

    def broken_fsync(fd: int) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(ce.os, "fsync", broken_fsync)
    event = ce.build_event(kind="build", repo=REPO, outcome="pr-opened", issue=3, pr=7, head=HEAD)
    with pytest.raises(OSError):
        inbox.record(event)
    assert inbox.wait(timeout=0.3, interval=0.05) is None
    assert list(inbox.pending_dir.iterdir()) == [] and list(inbox.tmp_dir.iterdir()) == []


def test_waiter_wakes_promptly_on_a_new_event(tmp_path: Path) -> None:
    inbox = ce.Inbox(tmp_path / "events")
    result: dict[str, object] = {}

    def waiter() -> None:
        started = time.monotonic()
        result["found"] = inbox.wait(timeout=30, interval=0.05)
        result["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.2)
    event = ce.build_event(kind="review-run", repo=REPO, outcome="converged", pr=7, head=HEAD, run_id="run-1")
    inbox.record(event)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["found"]["events"] == [event]  # type: ignore[index]
    assert result["elapsed"] < 5  # type: ignore[operator]


def test_delivery_requires_a_durable_pending_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = ce.Inbox(tmp_path / "events")
    event = ce.build_event(kind="build", repo=REPO, outcome="question", issue=3)
    with monkeypatch.context() as patch:
        def fail_sync(path: Path) -> None:
            assert path == inbox.pending_dir
            raise OSError("directory sync failed")

        patch.setattr(ce, "_fsync_dir", fail_sync)
        with pytest.raises(OSError):
            inbox.record(event)
        with pytest.raises(OSError):
            inbox.wait(timeout=1, interval=0.1)
        with pytest.raises(OSError):
            inbox.record(event)
    assert inbox.pending()["events"] == [event]


def test_ack_retry_syncs_destination_before_removing_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = ce.Inbox(tmp_path / "events")
    event = ce.build_event(kind="build", repo=REPO, outcome="question", issue=3)
    inbox.record(event)
    with monkeypatch.context() as patch:
        def fail_sync(path: Path) -> None:
            assert path == inbox.acked_dir
            raise OSError("directory sync failed")

        patch.setattr(ce, "_fsync_dir", fail_sync)
        for _ in range(2):
            with pytest.raises(OSError):
                inbox.ack(event["id"])
            assert (inbox.pending_dir / (event["id"] + ".json")).exists()
    assert inbox.ack(event["id"]) == "already-acknowledged"
    assert inbox.pending()["events"] == []


def test_duplicate_and_replayed_events_change_nothing(tmp_path: Path) -> None:
    inbox = ce.Inbox(tmp_path / "events")
    first = ce.build_event(kind="build", repo=REPO, outcome="pr-opened", issue=3, pr=7, head=HEAD, at="2026-01-01T00:00:00Z")
    replay = ce.build_event(kind="build", repo=REPO, outcome="pr-opened", issue=3, pr=7, head=HEAD,
                            source="i3-build", at="2026-01-01T00:05:00Z")
    assert first["id"] == replay["id"]
    assert inbox.record(first) == "recorded"
    assert inbox.record(replay) == "duplicate"
    assert inbox.pending()["events"] == [first]

    assert inbox.ack(first["id"]) == "acknowledged"
    snapshot = sorted(p.name for p in inbox.acked_dir.iterdir())
    assert inbox.ack(first["id"]) == "already-acknowledged"
    # A replay after acknowledgement does not re-open the event or wake a waiter.
    assert inbox.record(replay) == "duplicate"
    assert inbox.pending() == {"events": [], "invalid": 0}
    assert inbox.wait(timeout=0.2, interval=0.05) is None
    assert sorted(p.name for p in inbox.acked_dir.iterdir()) == snapshot

    moved = ce.build_event(kind="build", repo=REPO, outcome="pr-opened", issue=3, pr=7, head="b" * 40)
    assert moved["id"] != first["id"]
    assert inbox.record(moved) == "recorded"


def test_concurrent_recorders_persist_one_event(tmp_path: Path) -> None:
    inbox_path = tmp_path / "events"
    procs = [subprocess.Popen([sys.executable, "-I", str(SOURCE), "--inbox", str(inbox_path), *build_args()],
                              stdout=subprocess.PIPE, text=True) for _ in range(6)]
    statuses = sorted(json.loads(proc.communicate(timeout=30)[0])["status"] for proc in procs)
    assert statuses == ["duplicate"] * 5 + ["recorded"]
    assert len(ce.Inbox(inbox_path).pending()["events"]) == 1


def test_wait_times_out_to_the_watchdog(tmp_path: Path) -> None:
    inbox = tmp_path / "events"
    started = time.monotonic()
    result = run(SOURCE, inbox, "wait", "--timeout", "0.5", "--interval", "0.1")
    assert result.returncode == 124
    assert json.loads(result.stdout) == {"status": "watchdog"}
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("args", [["--timeout", "301"], ["--timeout", "0"], ["--timeout", "5", "--interval", "6"]])
def test_wait_never_extends_past_the_watchdog_interval(tmp_path: Path, args: list[str]) -> None:
    result = run(SOURCE, tmp_path / "events", "wait", *args)
    assert result.returncode == 2


def test_wait_defaults_to_the_300_second_watchdog() -> None:
    assert ce.WATCHDOG_SECONDS == 300
    assert ce._parser().parse_args(["--inbox", "/x", "wait"]).timeout == 300


@pytest.mark.parametrize(
    "overrides",
    [
        {"repo": "not a repo"},
        {"head": "abc"},
        {"head": None},
        {"pr": 0},
        {"issue": None, "pr": None, "head": None},
        {"source": "free text with spaces"},
        {"run_id": "x" * 200},
        {"kind": "review-run", "run_id": None},
        {"kind": "ready-flip", "pr": None, "head": None},
    ],
)
def test_events_carry_identifiers_only(overrides: dict[str, object]) -> None:
    fields = {"kind": "build", "repo": REPO, "outcome": "pr-opened", "issue": 3, "pr": 7, "head": HEAD}
    fields.update(overrides)
    with pytest.raises(ce.EventError):
        ce.build_event(**fields)


def test_unknown_or_extra_fields_are_rejected(tmp_path: Path) -> None:
    event = ce.build_event(kind="build", repo=REPO, outcome="pr-opened", issue=3, pr=7, head=HEAD)
    with pytest.raises(ce.EventError):
        ce.validate({**event, "message": "anything"})
    with pytest.raises(ce.EventError):
        ce.validate({**event, "pr": 8})  # id no longer matches identity
    result = run(SOURCE, tmp_path / "events", "record", "--kind", "build", "--repo", REPO,
                 "--outcome", "summary", "--issue", "3")
    assert result.returncode != 0


def test_ack_of_unknown_event_is_an_error_and_writes_nothing(tmp_path: Path) -> None:
    inbox = tmp_path / "events"
    result = run(SOURCE, inbox, "ack", "--id", "0" * 32)
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "invalid"
    assert list((inbox / "acked").iterdir()) == []
    assert run(SOURCE, inbox, "ack", "--id", "../escape").returncode == 2


def test_a_malformed_pending_file_still_wakes_but_is_not_an_event(tmp_path: Path) -> None:
    inbox = ce.Inbox(tmp_path / "events")
    (inbox.pending_dir / ("1" * 32 + ".json")).write_text('{"schema": "other"}')
    assert inbox.wait(timeout=1, interval=0.05) == {"events": [], "invalid": 1}


def test_inbox_must_be_private_and_absolute(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    assert run(SOURCE, tmp_path / "link", "pending").returncode == 2
    assert run(SOURCE, Path("relative/events"), "pending").returncode == 2
    ce.Inbox(tmp_path / "events")
    assert (tmp_path / "events").stat().st_mode & 0o077 == 0
    (tmp_path / "events" / "pending").chmod(0o770)
    assert run(SOURCE, tmp_path / "events", "pending").returncode == 2


def test_fifo_is_reported_without_blocking_wait(tmp_path: Path) -> None:
    inbox = ce.Inbox(tmp_path / "events")
    os.mkfifo(inbox.pending_dir / ("1" * 32 + ".json"))
    result = run(SOURCE, inbox.root, "wait", "--timeout", "1", "--interval", "0.1", timeout=3)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"events": [], "invalid": 1}
