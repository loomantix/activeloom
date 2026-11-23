"""Offline tests for the ship-staging notification outbox; no real webhook is contacted."""

from __future__ import annotations

import http.server
import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError
from urllib.request import build_opener

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "ship-notification-outbox.py"
RELATIVE = "skills/ship-staging/scripts/notification-outbox.py"
REPO = "example/project"
HEAD = "a" * 40
MERGE = "b" * 40
SECRET_KEY = "SENSITIVE-KEY"
SECRET_TOKEN = "SENSITIVE-TOKEN"
WEBHOOK = f"https://chat.googleapis.com/v1/spaces/AAAA/messages?key={SECRET_KEY}&token={SECRET_TOKEN}"
OTHER_WEBHOOK = f"https://chat.googleapis.com/v1/spaces/BBBB/messages?key={SECRET_KEY}&token={SECRET_TOKEN}"
MESSAGE = "*Merged to staging:* PR #7 - Example\n\nSummary.\n\nhttps://github.com/example/project/pull/7"


@pytest.fixture
def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ship_outbox", SOURCE)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def box(module: ModuleType, tmp_path: Path):
    outbox = module.Outbox(tmp_path / "state" / "outbox.sqlite3")
    yield outbox
    outbox.close()


class Recorder:
    def __init__(self, receipt: str = "spaces/AAAA/messages/m1", error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.receipt = receipt
        self.error = error

    def __call__(self, webhook: str, request_id: str, message: str) -> str:
        self.calls.append((webhook, request_id, message))
        if self.error is not None:
            raise self.error
        return self.receipt


def test_intent_is_recorded_before_merge_and_not_delivered_until_confirmed(module: ModuleType, box) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    assert box.status(identity) == module.AWAITING_MERGE
    sender = Recorder()
    assert box.deliver(identity, WEBHOOK, sender) == module.AWAITING_MERGE
    assert box.flush(WEBHOOK, sender) == {}
    assert sender.calls == []
    assert box.pending() == {identity: module.AWAITING_MERGE}
    assert box.confirm(identity, MERGE) == module.PENDING_DELIVERY
    assert box.flush(WEBHOOK, sender) == {identity: module.DELIVERED}


def test_record_and_confirm_are_idempotent(module: ModuleType, box) -> None:
    identity = box.record(REPO, 7, HEAD, "first draft")
    assert box.record(REPO, 7, HEAD, MESSAGE) == identity
    request_id = box._row(identity)["request_id"]
    assert box.confirm(identity, MERGE) == module.PENDING_DELIVERY
    assert box.confirm(identity, MERGE) == module.PENDING_DELIVERY
    with pytest.raises(module.OutboxError):
        box.confirm(identity, "c" * 40)
    box.record(REPO, 7, HEAD, "changed after merge")
    row = box._row(identity)
    assert (row["message"], row["request_id"]) == (MESSAGE, request_id)


def test_flush_is_safe_to_repeat_and_never_reposts(module: ModuleType, box) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    sender = Recorder()
    for _ in range(3):
        box.flush(WEBHOOK, sender)
        assert box.deliver(identity, WEBHOOK, sender) == module.DELIVERED
    assert len(sender.calls) == 1
    assert box.pending() == {}


def test_failed_delivery_stays_pending_and_retries_with_the_same_request_id(module: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "outbox.sqlite3"
    box = module.Outbox(path)
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    failing = Recorder(error=OSError("SENSITIVE-TRANSPORT-DETAIL"))
    assert box.deliver(identity, WEBHOOK, failing) == module.PENDING_DELIVERY
    assert box.deliver(identity, WEBHOOK, Recorder(receipt="not a receipt")) == module.PENDING_DELIVERY
    box.close()

    box = module.Outbox(path)
    success = Recorder()
    assert box.flush(WEBHOOK, success) == {identity: module.DELIVERED}
    assert failing.calls[0][1] == success.calls[0][1]
    box.close()
    stored = path.read_bytes()
    for secret in (SECRET_KEY, SECRET_TOKEN, "SENSITIVE-TRANSPORT-DETAIL", "chat.googleapis.com"):
        assert secret.encode() not in stored


def test_missing_or_invalid_webhook_is_pending_not_an_error(module: ModuleType, box) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    sender = Recorder()
    assert box.deliver(identity, None, sender) == module.PENDING_CONFIGURATION
    assert box.deliver(identity, "https://evil.example/v1/spaces/AAAA/messages", sender) == module.PENDING_CONFIGURATION
    assert sender.calls == []
    assert box.pending() == {identity: module.PENDING_CONFIGURATION}


def test_destination_change_does_not_resend_an_uncertain_delivery(module: ModuleType, box) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    box.deliver(identity, WEBHOOK, Recorder(error=TimeoutError()))
    sender = Recorder()
    assert box.deliver(identity, OTHER_WEBHOOK, sender) == module.PENDING_DESTINATION_CHANGE
    assert sender.calls == []
    assert box.discard(identity)
    assert box.pending() == {}


def test_discard_never_removes_a_delivered_intent(module: ModuleType, box) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    box.deliver(identity, WEBHOOK, Recorder())
    assert not box.discard(identity)
    assert box.status(identity) == module.DELIVERED


@pytest.mark.parametrize(
    "webhook",
    [
        "http://chat.googleapis.com/v1/spaces/AAAA/messages?key=k",
        "https://chat.googleapis.com.evil.example/v1/spaces/AAAA/messages",
        "https://user@chat.googleapis.com/v1/spaces/AAAA/messages",
        "https://chat.googleapis.com:8443/v1/spaces/AAAA/messages",
        "https://chat.googleapis.com/v1/spaces/AAAA/members",
        "https://chat.googleapis.com/v1/spaces/AAAA/messages#frag",
    ],
)
def test_webhook_is_pinned_to_the_chat_host(module: ModuleType, webhook: str) -> None:
    with pytest.raises(module.InvalidWebhook):
        module.validate_webhook(webhook)


def test_endpoint_pins_the_request_id(module: ModuleType) -> None:
    endpoint = module.build_endpoint(WEBHOOK + "&requestId=attacker", "req-1")
    assert endpoint.count("requestId=") == 1
    assert endpoint.endswith("requestId=req-1")
    assert endpoint.startswith("https://chat.googleapis.com/v1/spaces/AAAA/messages?")


class _Redirector(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        type(self).hits.append(self.path)
        if self.path.startswith("/redirect"):
            self.send_response(307)
            self.send_header("Location", "/landed")
            self.end_headers()
            return
        body = json.dumps({"name": "spaces/AAAA/messages/m1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


def test_redirects_are_refused(module: ModuleType) -> None:
    _Redirector.hits = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(module.NoRedirect())
        assert module.post(f"{base}/ok", "hi", opener) == "spaces/AAAA/messages/m1"
        with pytest.raises(HTTPError):
            module.post(f"{base}/redirect", "hi", opener)
        assert "/landed" not in _Redirector.hits
    finally:
        server.shutdown()
        server.server_close()


def run_cli(script: Path, database: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(script), "--database", str(database), *args],
        capture_output=True, text=True, timeout=10, env=env or {"PATH": "/usr/bin:/bin"},
    )


def test_cli_reports_pending_without_a_webhook(tmp_path: Path) -> None:
    database = tmp_path / "outbox.sqlite3"
    message = tmp_path / "message.txt"
    message.write_text(MESSAGE)
    recorded = run_cli(SOURCE, database, "record", "--repo", REPO, "--pr", "7", "--head", HEAD, "--message-file", str(message))
    assert recorded.returncode == 0, recorded.stdout
    identity = json.loads(recorded.stdout)["id"]
    assert run_cli(SOURCE, database, "confirm", "--id", identity, "--merge", MERGE).returncode == 0
    delivered = run_cli(SOURCE, database, "deliver", "--id", identity)
    assert delivered.returncode == 3
    assert json.loads(delivered.stdout)["notification"] == "pending-configuration"
    flushed = run_cli(SOURCE, database, "flush")
    assert flushed.returncode == 3
    assert run_cli(SOURCE, database, "discard", "--id", identity).returncode == 0
    assert run_cli(SOURCE, database, "pending").returncode == 0


@pytest.mark.parametrize(("engine", "harness"), [("claude", ".claude"), ("codex", ".codex"), ("gemini", ".agents")])
def test_outbox_ships_to_all_three_harness_roots(engine: str, harness: str, tmp_path: Path) -> None:
    destination = f"{harness}/{RELATIVE}"
    assert (ROOT / destination).read_bytes() == SOURCE.read_bytes()
    targets = yaml.safe_load((ROOT / "scripts/sync-targets.yml").read_text())["harnesses"][engine]["targets"]
    entry = next(target for target in targets if target["destination"] == destination)
    assert entry["source"] == destination
    assert entry.get("mode") == "0755"
    result = run_cli(ROOT / destination, tmp_path / "outbox.sqlite3", "pending")
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout) == {"pending": {}}
