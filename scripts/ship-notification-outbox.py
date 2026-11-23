#!/usr/bin/env python3
"""Durable outbox for ship-staging chat notifications.

`ship-staging` records the notification intent before it merges, confirms it
with the merge commit after the merge is verified, and delivers it afterwards.
Delivery is decoupled from the merge: a missing webhook or a failed post leaves
the intent pending, and `flush` retries the selected repository's confirmed intents until the chat
provider returns a message receipt. Each intent carries one request ID for its
whole life, sent as the provider's idempotency key, and a delivered intent is
never posted again.

The outbox stores identifiers, the composed summary message, an enum status and
a hash of the non-secret space path. It never stores the webhook URL, its key or
token, or a raw transport error.

Exit codes: 0 done, 2 outbox or usage error, 3 one or more intents pending.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

CHAT_HOST = "chat.googleapis.com"
SPACE_PATH_RE = re.compile(r"/v1/spaces/[A-Za-z0-9_-]+/messages")
RECEIPT_RE = re.compile(r"spaces/[A-Za-z0-9_-]+/messages/[A-Za-z0-9_.-]+")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA_RE = re.compile(r"[0-9a-f]{40}")
MAX_MESSAGE_CHARS = 1500
TIMEOUT_SECONDS = 10

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_PENDING = 3

# Statuses an undelivered intent can report. All are enums, never free text.
AWAITING_MERGE = "awaiting-merge"
PENDING_CONFIGURATION = "pending-configuration"
PENDING_DELIVERY = "pending-delivery"
PENDING_DESTINATION_CHANGE = "pending-destination-change"
DELIVERED = "delivered"

Sender = Callable[[str, str, str], str]


class OutboxError(Exception):
    """A usage or storage problem; the message is safe to print."""


class InvalidWebhook(ValueError):
    """The configured webhook is not a pinned chat endpoint."""


class NoRedirect(HTTPRedirectHandler):
    """Refuse every redirect so the webhook credential never leaves the pinned host."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


def validate_webhook(webhook: str) -> str:
    """Return the non-secret space path of a pinned chat webhook, or raise."""
    try:
        parts = urlsplit(webhook.strip())
        port = parts.port
    except ValueError:
        raise InvalidWebhook("webhook is not a pinned chat endpoint") from None
    if (
        parts.scheme != "https"
        or parts.hostname != CHAT_HOST
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or not SPACE_PATH_RE.fullmatch(parts.path)
    ):
        raise InvalidWebhook("webhook is not a pinned chat endpoint")
    return parts.path


def build_endpoint(webhook: str, request_id: str) -> str:
    """Pin the request ID into the webhook query, replacing any supplied one."""
    parts = urlsplit(webhook.strip())
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "requestId"]
    query.append(("requestId", request_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def post(endpoint: str, message: str, opener: OpenerDirector | None = None) -> str:
    """POST one message without following redirects; return the provider receipt."""
    opener = opener or build_opener(NoRedirect())
    request = Request(
        endpoint,
        data=json.dumps({"text": message}).encode(),
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise ValueError("unexpected status")
        result = json.load(response)
    name = result.get("name", "") if isinstance(result, dict) else ""
    if not isinstance(name, str) or not RECEIPT_RE.fullmatch(name):
        raise ValueError("missing message receipt")
    return name


def send(webhook: str, request_id: str, message: str) -> str:
    """Deliver to the pinned chat host with the intent's idempotency key."""
    validate_webhook(webhook)
    return post(build_endpoint(webhook, request_id), message)


def default_database() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "activeloom" / "ship-staging" / "outbox.sqlite3"


def read_webhook() -> str | None:
    """Resolve the webhook from the runtime environment only; never print it."""
    value = os.environ.get("GCHAT_DEV_WEBHOOK_URL", "")
    if not value.strip():
        path = os.environ.get("GCHAT_DEV_WEBHOOK_FILE", "")
        if not path:
            return None
        try:
            if not stat.S_ISREG(os.stat(path).st_mode):
                return None
            value = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    value = value.strip()
    return value or None


class Outbox:
    """SQLite-backed notification intents keyed by `<repo>#<pr>`."""

    def __init__(self, path: Path) -> None:
        if path.is_symlink():
            raise OutboxError("outbox path must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS intents ("
            " id TEXT PRIMARY KEY,"
            " repo TEXT NOT NULL,"
            " pr INTEGER NOT NULL,"
            " head_sha TEXT NOT NULL,"
            " merge_sha TEXT,"
            " message TEXT NOT NULL,"
            " request_id TEXT NOT NULL,"
            " destination TEXT,"
            " status TEXT NOT NULL,"
            " receipt TEXT,"
            " attempts INTEGER NOT NULL DEFAULT 0)"
        )

    def close(self) -> None:
        self.connection.close()

    def _row(self, identity: str) -> sqlite3.Row | None:
        self.connection.row_factory = sqlite3.Row
        row: sqlite3.Row | None = self.connection.execute(
            "SELECT * FROM intents WHERE id = ?", (identity,)
        ).fetchone()
        return row

    def record(self, repo: str, pr: int, head: str, message: str) -> str:
        """Persist the intent before the merge. Re-recording an unmerged intent updates it."""
        if not REPO_RE.fullmatch(repo) or pr < 1 or not SHA_RE.fullmatch(head):
            raise OutboxError("invalid repository, PR number or head SHA")
        message = message.strip()
        if not message or len(message) > MAX_MESSAGE_CHARS:
            raise OutboxError(f"message must be 1-{MAX_MESSAGE_CHARS} characters")
        identity = f"{repo}#{pr}"
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(identity)
            if row is None:
                self.connection.execute(
                    "INSERT INTO intents(id, repo, pr, head_sha, message, request_id, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (identity, repo, pr, head, message, str(uuid.uuid4()), AWAITING_MERGE),
                )
            elif row["merge_sha"] is None:
                self.connection.execute(
                    "UPDATE intents SET head_sha = ?, message = ? WHERE id = ?", (head, message, identity)
                )
            # A confirmed intent is immutable: its message may already be posted.
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return identity

    def confirm(self, identity: str, merge: str) -> str:
        """Mark the intent deliverable once the merge is verified on GitHub."""
        if not SHA_RE.fullmatch(merge):
            raise OutboxError("invalid merge SHA")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(identity)
            if row is None:
                raise OutboxError("unknown notification")
            if row["merge_sha"] not in (None, merge):
                raise OutboxError("notification already confirmed with a different merge")
            if row["merge_sha"] is None:
                self.connection.execute(
                    "UPDATE intents SET merge_sha = ?, status = ? WHERE id = ?",
                    (merge, PENDING_DELIVERY, identity),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return self.status(identity)

    def discard(self, identity: str) -> bool:
        """Drop an undelivered intent (a cancelled merge, or a reconciled one)."""
        cursor = self.connection.execute(
            "DELETE FROM intents WHERE id = ? AND status != ?", (identity, DELIVERED)
        )
        return cursor.rowcount > 0

    def status(self, identity: str) -> str:
        row = self._row(identity)
        if row is None:
            raise OutboxError("unknown notification")
        return str(row["status"])

    def deliver(self, identity: str, webhook: str | None, sender: Sender = send) -> str:
        """Attempt one delivery. Holding the write lock serializes concurrent flushes."""
        destination = None
        if webhook:
            try:
                space = validate_webhook(webhook)
            except InvalidWebhook:
                pass
            else:
                destination = hashlib.sha256(space.encode()).hexdigest()
                # Autocommit before sending: a crash can lose the receipt but
                # must preserve the space in which the request ID was used.
                self.connection.execute(
                    "UPDATE intents SET destination = ? WHERE id = ? AND destination IS NULL"
                    " AND merge_sha IS NOT NULL AND receipt IS NULL",
                    (destination, identity),
                )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            outcome = self._deliver_locked(identity, webhook, destination, sender)
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return outcome

    def _deliver_locked(self, identity: str, webhook: str | None, destination: str | None, sender: Sender) -> str:
        row = self._row(identity)
        if row is None:
            raise OutboxError("unknown notification")
        if row["status"] in (DELIVERED, AWAITING_MERGE):
            return str(row["status"])

        def settle(status: str) -> str:
            self.connection.execute("UPDATE intents SET status = ? WHERE id = ?", (status, identity))
            return status

        if not webhook or destination is None:
            return settle(PENDING_CONFIGURATION)
        if row["destination"] is None:
            # A confirm or re-record raced with the pin. Retry from the start.
            return settle(PENDING_DELIVERY)
        if row["destination"] != destination:
            # An earlier attempt may have landed in the other space; the request
            # ID only deduplicates within one space, so do not resend blindly.
            return settle(PENDING_DESTINATION_CHANGE)
        self.connection.execute(
            "UPDATE intents SET attempts = attempts + 1 WHERE id = ?", (identity,)
        )
        try:
            receipt = sender(webhook, str(row["request_id"]), str(row["message"]))
        except Exception:
            return settle(PENDING_DELIVERY)
        if not isinstance(receipt, str) or not RECEIPT_RE.fullmatch(receipt):
            return settle(PENDING_DELIVERY)
        self.connection.execute(
            "UPDATE intents SET status = ?, receipt = ? WHERE id = ?", (DELIVERED, receipt, identity)
        )
        return DELIVERED

    def flush(self, repo: str, webhook: str | None, sender: Sender = send) -> dict[str, str]:
        """Deliver this repository's confirmed, undelivered intents."""
        identities = [
            row[0]
            for row in self.connection.execute(
                "SELECT id FROM intents WHERE repo = ? AND status NOT IN (?, ?) ORDER BY rowid",
                (repo, DELIVERED, AWAITING_MERGE),
            )
        ]
        return {identity: self.deliver(identity, webhook, sender) for identity in identities}

    def pending(self) -> dict[str, str]:
        return {
            row[0]: row[1]
            for row in self.connection.execute(
                "SELECT id, status FROM intents WHERE status != ? ORDER BY rowid", (DELIVERED,)
            )
        }


def _read_message(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise OutboxError("could not read message file") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Durable ship-staging notification outbox.")
    parser.add_argument("--database", type=Path, default=None, help="outbox path (default: private state dir)")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="persist the intent before merging")
    record.add_argument("--repo", required=True)
    record.add_argument("--pr", required=True, type=int)
    record.add_argument("--head", required=True)
    record.add_argument("--message-file", required=True)
    confirm = commands.add_parser("confirm", help="attach the verified merge commit")
    confirm.add_argument("--id", required=True)
    confirm.add_argument("--merge", required=True)
    deliver = commands.add_parser("deliver", help="attempt delivery of one intent")
    deliver.add_argument("--id", required=True)
    flush = commands.add_parser("flush", help="attempt delivery of confirmed pending intents for one repository")
    flush.add_argument("--repo", required=True)
    status = commands.add_parser("status", help="read one notification's stored status")
    status.add_argument("--id", required=True)
    commands.add_parser("pending", help="list undelivered intents and their status")
    discard = commands.add_parser("discard", help="drop an undelivered intent")
    discard.add_argument("--id", required=True)
    args = parser.parse_args(argv)

    outbox: Outbox | None = None
    try:
        outbox = Outbox(args.database or default_database())
        if args.command == "record":
            identity = outbox.record(args.repo, args.pr, args.head, _read_message(args.message_file))
            print(json.dumps({"id": identity, "notification": outbox.status(identity)}))
            return EXIT_OK
        if args.command == "confirm":
            print(json.dumps({"id": args.id, "notification": outbox.confirm(args.id, args.merge)}))
            return EXIT_OK
        if args.command == "discard":
            print(json.dumps({"id": args.id, "discarded": outbox.discard(args.id)}))
            return EXIT_OK
        if args.command == "status":
            print(json.dumps({"id": args.id, "notification": outbox.status(args.id)}))
            return EXIT_OK
        if args.command == "pending":
            pending = outbox.pending()
            print(json.dumps({"pending": pending}))
            return EXIT_PENDING if pending else EXIT_OK
        webhook = read_webhook()
        if args.command == "deliver":
            outcome = outbox.deliver(args.id, webhook)
            print(json.dumps({"id": args.id, "notification": outcome}))
            return EXIT_OK if outcome == DELIVERED else EXIT_PENDING
        results = outbox.flush(args.repo, webhook)
        print(json.dumps({"flushed": results}))
        return EXIT_OK if all(value == DELIVERED for value in results.values()) else EXIT_PENDING
    except OutboxError as error:
        print(json.dumps({"notification": "outbox-error", "reason": str(error)}))
        return EXIT_ERROR
    except (OSError, sqlite3.Error):
        print(json.dumps({"notification": "outbox-error", "reason": "storage"}))
        return EXIT_ERROR
    finally:
        if outbox is not None:
            outbox.close()


if __name__ == "__main__":
    sys.exit(main())
