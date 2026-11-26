"""Offline tests for the ship-staging notification outbox; no real webhook is contacted."""

from __future__ import annotations

import http.server
import io
import importlib.util
import json
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener

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
def box(module: ModuleType, tmp_path: Path) -> Iterator[Any]:
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


def test_intent_is_recorded_before_merge_and_not_delivered_until_confirmed(module: ModuleType, box: Any) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    assert box.status(identity) == module.AWAITING_MERGE
    sender = Recorder()
    assert box.deliver(identity, WEBHOOK, sender) == module.AWAITING_MERGE
    assert box.flush(REPO, WEBHOOK, sender) == {}
    assert sender.calls == []
    assert box.pending() == {identity: module.AWAITING_MERGE}
    assert box.confirm(identity, MERGE) == module.PENDING_DELIVERY
    assert box.flush(REPO, WEBHOOK, sender) == {identity: module.DELIVERED}


def test_reopened_outbox_restricts_access_and_preserves_intents(module: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "outbox.sqlite3"
    original = module.Outbox(path)
    try:
        identity = original.record(REPO, 7, HEAD, MESSAGE)
    finally:
        original.close()
    path.chmod(0o644)

    reopened = module.Outbox(path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert reopened.status(identity) == module.AWAITING_MERGE
    finally:
        reopened.close()


def test_record_and_confirm_are_idempotent(module: ModuleType, box: Any) -> None:
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


def test_flush_is_safe_to_repeat_and_never_reposts(module: ModuleType, box: Any) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    sender = Recorder()
    for _ in range(3):
        box.flush(REPO, WEBHOOK, sender)
        assert box.deliver(identity, WEBHOOK, sender) == module.DELIVERED
    assert len(sender.calls) == 1
    assert box.pending() == {}


def test_flush_only_delivers_the_selected_repository(module: ModuleType, box: Any) -> None:
    other = box.record("other/project", 7, HEAD, "other summary")
    box.confirm(other, MERGE)
    current = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(current, MERGE)
    sender = Recorder()
    assert box.flush(REPO, WEBHOOK, sender) == {current: module.DELIVERED}
    assert [message for _, _, message in sender.calls] == [MESSAGE]
    assert box.pending() == {other: module.PENDING_DELIVERY}
    assert box.pending(REPO) == {}
    assert box.pending("other/project") == {other: module.PENDING_DELIVERY}


def test_interrupted_send_preserves_the_destination(module: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "outbox.sqlite3"
    box = module.Outbox(path)
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)

    def interrupted(*args: object) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        box.deliver(identity, WEBHOOK, interrupted)
    box.close()
    box = module.Outbox(path)
    sender = Recorder()
    assert box.deliver(identity, OTHER_WEBHOOK, sender) == module.PENDING_DESTINATION_CHANGE
    assert sender.calls == []
    assert box.deliver(identity, WEBHOOK, sender) == module.DELIVERED
    box.close()


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
    assert box.flush(REPO, WEBHOOK, success) == {identity: module.DELIVERED}
    assert failing.calls[0][1] == success.calls[0][1]
    box.close()
    stored = path.read_bytes()
    for secret in (SECRET_KEY, SECRET_TOKEN, "SENSITIVE-TRANSPORT-DETAIL", "chat.googleapis.com"):
        assert secret.encode() not in stored


def test_missing_or_invalid_webhook_is_pending_not_an_error(module: ModuleType, box: Any) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    sender = Recorder()
    assert box.deliver(identity, None, sender) == module.PENDING_CONFIGURATION
    assert box.deliver(identity, "https://chat.googleapis.com:invalid/", sender) == module.PENDING_CONFIGURATION
    assert box.deliver(identity, "https://[", sender) == module.PENDING_CONFIGURATION
    assert box.deliver(identity, "https://evil.example/v1/spaces/AAAA/messages", sender) == module.PENDING_CONFIGURATION
    assert sender.calls == []
    assert box.pending() == {identity: module.PENDING_CONFIGURATION}


def test_destination_change_does_not_resend_an_uncertain_delivery(module: ModuleType, box: Any) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    box.deliver(identity, WEBHOOK, Recorder(error=TimeoutError()))
    sender = Recorder()
    assert box.deliver(identity, OTHER_WEBHOOK, sender) == module.PENDING_DESTINATION_CHANGE
    assert sender.calls == []
    assert box.discard(identity)
    assert box.pending() == {}


def test_discard_never_removes_a_delivered_intent(module: ModuleType, box: Any) -> None:
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


def test_message_id_is_a_stable_chat_client_id(module: ModuleType) -> None:
    first = module.message_id_for("0b8f8a52-2f7e-4f43-9d0e-3f2f3d9b1c11")
    assert first == module.message_id_for("0b8f8a52-2f7e-4f43-9d0e-3f2f3d9b1c11")
    assert first != module.message_id_for("another-request")
    for request_id in ("0b8f8a52-2f7e-4f43-9d0e-3f2f3d9b1c11", "UPPER_case/odd chars", "x" * 500):
        message_id = module.message_id_for(request_id)
        assert message_id.startswith("client-") and len(message_id) <= 63
        assert all(character in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in message_id)
        assert module.RECEIPT_RE.fullmatch(f"spaces/AAAA/messages/{message_id}")


def test_endpoint_pins_the_message_id_and_drops_request_id(module: ModuleType) -> None:
    message_id = module.message_id_for("req-1")
    endpoint = module.build_endpoint(WEBHOOK + "&requestId=attacker&messageId=client-attacker", message_id)
    query = parse_qs(urlsplit(endpoint).query)
    assert query == {"key": [SECRET_KEY], "token": [SECRET_TOKEN], "messageId": [message_id]}
    assert endpoint.startswith("https://chat.googleapis.com/v1/spaces/AAAA/messages?")
    with pytest.raises(ValueError):
        module.build_endpoint(WEBHOOK, "req-1")


class FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status = 200
        self.body = json.dumps(body).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, *args: object) -> bytes:
        return self.body


class FakeOpener:
    """Answers each POST with the next scripted outcome; never touches the network."""

    def __init__(self, *outcomes: tuple[int, dict[str, Any]]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float) -> FakeResponse:
        self.requests.append(request)
        code, body = self.outcomes.pop(0)
        if code == 200:
            return FakeResponse(body)
        raise HTTPError(request.full_url, code, "scripted", {}, io.BytesIO(json.dumps(body).encode()))  # type: ignore[arg-type]


def chat_error(code: int, status: str) -> tuple[int, dict[str, Any]]:
    return code, {"error": {"code": code, "message": "scripted provider text", "status": status}}


def test_send_posts_with_the_client_message_id(module: ModuleType) -> None:
    opener = FakeOpener((200, {"name": "spaces/AAAA/messages/m1"}))
    assert module.send(WEBHOOK, "req-1", MESSAGE, opener) == "spaces/AAAA/messages/m1"
    (request,) = opener.requests
    parts = urlsplit(request.full_url)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "chat.googleapis.com", "/v1/spaces/AAAA/messages")
    assert parse_qs(parts.query) == {
        "key": [SECRET_KEY], "token": [SECRET_TOKEN], "messageId": [module.message_id_for("req-1")],
    }
    assert isinstance(request.data, bytes) and json.loads(request.data) == {"text": MESSAGE}
    with pytest.raises(module.InvalidWebhook):
        module.send("https://evil.example/v1/spaces/AAAA/messages", "req-1", MESSAGE, opener)
    assert len(opener.requests) == 1


def test_send_treats_already_exists_as_delivered(module: ModuleType) -> None:
    opener = FakeOpener(chat_error(409, "ALREADY_EXISTS"))
    receipt = module.send(WEBHOOK, "req-1", MESSAGE, opener)
    assert receipt == f"spaces/AAAA/messages/{module.message_id_for('req-1')}"
    assert module.RECEIPT_RE.fullmatch(receipt)


@pytest.mark.parametrize(
    "outcome",
    [chat_error(400, "INVALID_ARGUMENT"), chat_error(409, "ABORTED"), (409, {}), chat_error(500, "INTERNAL")],
)
def test_send_raises_on_other_http_errors(module: ModuleType, outcome: tuple[int, dict[str, Any]]) -> None:
    with pytest.raises(HTTPError):
        module.send(WEBHOOK, "req-1", MESSAGE, FakeOpener(outcome))


def test_delivery_through_chat_outcomes(module: ModuleType, box: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    opener = FakeOpener(
        chat_error(400, "INVALID_ARGUMENT"),
        chat_error(409, "ALREADY_EXISTS"),
    )

    def sender(webhook: str, request_id: str, message: str) -> str:
        return str(module.send(webhook, request_id, message, opener))

    assert box.deliver(identity, WEBHOOK, sender) == module.PENDING_DELIVERY
    assert box.last_failure(identity) == "http-400"
    assert box.deliver(identity, WEBHOOK, sender) == module.DELIVERED
    assert box.last_failure(identity) is None
    message_id = module.message_id_for(box._row(identity)["request_id"])
    assert box._row(identity)["receipt"] == f"spaces/AAAA/messages/{message_id}"
    # Both attempts carried the same client message ID.
    assert {parse_qs(urlsplit(r.full_url).query)["messageId"][0] for r in opener.requests} == {message_id}
    assert box.deliver(identity, WEBHOOK, sender) == module.DELIVERED
    assert len(opener.requests) == 2


def test_failure_reasons_are_enums(module: ModuleType, box: Any) -> None:
    identity = box.record(REPO, 7, HEAD, MESSAGE)
    box.confirm(identity, MERGE)
    for error, expected in [
        (TimeoutError(), "transport"),
        (OSError("SENSITIVE-TRANSPORT-DETAIL"), "transport"),
        (ValueError("missing message receipt"), "no-receipt"),
        (RuntimeError("SENSITIVE"), "error"),
    ]:
        assert box.deliver(identity, WEBHOOK, Recorder(error=error)) == module.PENDING_DELIVERY
        assert box.last_failure(identity) == expected
    assert box.deliver(identity, WEBHOOK, Recorder(receipt="not a receipt")) == module.PENDING_DELIVERY
    assert box.last_failure(identity) == "no-receipt"


@pytest.mark.parametrize("concurrent_upgrade", [False, True])
def test_an_existing_outbox_gains_the_failure_column(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent_upgrade: bool
) -> None:
    path = tmp_path / "outbox.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE intents (id TEXT PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER NOT NULL,"
        " head_sha TEXT NOT NULL, merge_sha TEXT, message TEXT NOT NULL, request_id TEXT NOT NULL,"
        " destination TEXT, status TEXT NOT NULL, receipt TEXT, attempts INTEGER NOT NULL DEFAULT 0)"
    )
    legacy.execute(
        "INSERT INTO intents(id, repo, pr, head_sha, merge_sha, message, request_id, status)"
        " VALUES (?, ?, 7, ?, ?, ?, 'legacy-request', 'pending-delivery')",
        (f"{REPO}#7", REPO, HEAD, MERGE, MESSAGE),
    )
    legacy.commit()
    competing_upgrade_finished = False
    if concurrent_upgrade:
        connect = sqlite3.connect

        def open_with_competitor(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = connect(*args, **kwargs)

            def finish_other_upgrade(statement: str) -> None:
                nonlocal competing_upgrade_finished
                if statement.startswith(("ALTER TABLE", "BEGIN IMMEDIATE")):
                    connection.set_trace_callback(None)
                    legacy.execute("ALTER TABLE intents ADD COLUMN last_failure TEXT")
                    legacy.commit()
                    competing_upgrade_finished = True

            connection.set_trace_callback(finish_other_upgrade)
            return connection

        monkeypatch.setattr(sqlite3, "connect", open_with_competitor)
    box = module.Outbox(path)
    try:
        assert competing_upgrade_finished == concurrent_upgrade
        identity = f"{REPO}#7"
        assert box.last_failure(identity) is None
        assert box.deliver(identity, WEBHOOK, Recorder(error=TimeoutError())) == module.PENDING_DELIVERY
        assert box.last_failure(identity) == "transport"
    finally:
        box.close()
        legacy.close()


def test_status_reads_one_snapshot(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "outbox.sqlite3"
    writer = module.Outbox(path)
    try:
        identity = writer.record(REPO, 7, HEAD, MESSAGE)
        read_row = module.Outbox._row

        def read_during_delivery(reader: Any, key: str) -> Any:
            row = read_row(reader, key)
            if reader is not writer and row["status"] == module.AWAITING_MERGE:
                writer.confirm(key, MERGE)
                writer.deliver(key, WEBHOOK, Recorder(error=TimeoutError()))
            return row

        monkeypatch.setattr(module.Outbox, "_row", read_during_delivery)
        assert module.main(["--database", str(path), "status", "--id", identity]) == 0
        assert json.loads(capsys.readouterr().out) == {"id": identity, "notification": module.AWAITING_MERGE}
    finally:
        writer.close()


def test_post_sends_the_message_as_json(module: ModuleType) -> None:
    seen: list[Request] = []

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, *args: object) -> bytes:
            return json.dumps({"name": "spaces/AAAA/messages/m1"}).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            seen.append(request)
            return Response()

    assert module.post("https://chat.googleapis.com/v1/spaces/AAAA/messages?messageId=client-r", MESSAGE, Opener()) == "spaces/AAAA/messages/m1"
    (request,) = seen
    assert request.get_method() == "POST"
    assert isinstance(request.data, bytes) and json.loads(request.data) == {"text": MESSAGE}


class _Redirector(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        # A stock opener follows a 302/303 for a POST as a GET; record it.
        type(self).hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        type(self).hits.append(self.path)
        if self.path.startswith("/redirect"):
            # 302/303 are the codes the stock handler follows for a POST.
            self.send_response(int(self.path.rsplit("/", 1)[1]))
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
        assert module.post(f"{base}/ok", "hi") == "spaces/AAAA/messages/m1"
        for code in (302, 303):
            # The default opener, as production uses it.
            with pytest.raises(HTTPError):
                module.post(f"{base}/redirect/{code}", "hi")
            with pytest.raises(HTTPError):
                module.post(f"{base}/redirect/{code}", "hi", build_opener(module.NoRedirect()))
        assert "/landed" not in _Redirector.hits
        # Control: without NoRedirect the same response is followed.
        build_opener().open(f"{base}/redirect/302", data=b"{}")
        assert "/landed" in _Redirector.hits
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
    for repo, expected in [(REPO, {identity: "pending-configuration"}), ("other/project", {})]:
        listed = run_cli(SOURCE, database, "pending", "--repo", repo)
        assert listed.returncode == (3 if expected else 0)
        assert json.loads(listed.stdout) == {"pending": expected}
    flushed = run_cli(SOURCE, database, "flush", "--repo", REPO)
    assert flushed.returncode == 3
    status = run_cli(SOURCE, database, "status", "--id", identity)
    assert status.returncode == 0
    assert json.loads(status.stdout) == {"id": identity, "notification": "pending-configuration"}
    assert run_cli(SOURCE, database, "discard", "--id", identity).returncode == 0
    assert run_cli(SOURCE, database, "status", "--id", identity).returncode == 2
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
