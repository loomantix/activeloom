"""Offline GitHub transport for the real controller and vendored lifecycle CLI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from weakref import WeakKeyDictionary

import pytest


class ControllerRemote:
    """Expose mutable remote records without replacing lifecycle/auth parsers."""

    _controllers: WeakKeyDictionary[ModuleType, ControllerRemote] = WeakKeyDictionary()

    @classmethod
    def for_controller(cls, controller: ModuleType) -> ControllerRemote:
        """Require an explicitly attached transport for this imported controller."""
        return cls._controllers[controller]

    def __init__(self, controller: ModuleType, monkeypatch: pytest.MonkeyPatch, directory: Path, *, actor: str = "review-bot") -> None:
        self.rows: list[dict[str, Any]] = []
        self.actor = actor
        self.original_actor = actor
        self.head = "a" * 40
        self.path = directory / "remote.json"
        binary = directory / "bin"
        binary.mkdir(parents=True)
        fake = binary / "gh"
        fake.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
state = json.loads(Path(os.environ["TEST_CONTROLLER_REMOTE"]).read_text())
args = sys.argv[1:]
if args[:2] == ["api", "user"]:
    print(state["actor"] if "--jq" in args else json.dumps({"login": state["actor"]}))
elif args[:2] == ["pr", "view"] and "headRefOid" in args:
    print(state["head"])
elif any("/comments?per_page=100" in arg for arg in args):
    print(json.dumps([state["rows"]]))
elif "graphql" in args and any("issueOrPullRequest" in arg for arg in args):
    nodes = [{"databaseId": row["id"], "body": row["body"],
              "author": row["user"], "createdAt": row["created_at"],
              "lastEditedAt": row["lastEditedAt"]} for row in state["rows"]]
    print(json.dumps([{"data": {"repository": {"issueOrPullRequest": {"comments": {
        "totalCount": len(nodes), "nodes": nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}]))
else:
    sys.stderr.write("Unexpected offline GitHub operation; network fallback prohibited\\n")
    sys.exit(97)
''')
        fake.chmod(0o700)
        monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ.get("PATH", ""))
        monkeypatch.setenv("TEST_CONTROLLER_REMOTE", str(self.path))
        monkeypatch.setenv("AGENT_LOOP_REVIEW_ACTOR", actor)
        gh = controller._run_gh
        lifecycle = controller._lifecycle

        def run_gh(*args: Any, **kwargs: Any) -> str:
            self.sync()
            return str(gh(*args, **kwargs))

        def read_lifecycle(*args: Any) -> dict[str, Any]:
            self.sync()
            result: dict[str, Any] = lifecycle(*args)
            return result

        monkeypatch.setattr(controller, "_run_gh", run_gh)
        monkeypatch.setattr(controller, "_lifecycle", read_lifecycle)
        self._controllers[controller] = self

    def sync(self) -> None:
        """Publish synthetic API state for the next real parser invocation."""
        rows = [{"user": {"login": self.original_actor},
                 "created_at": "2026-01-01T00:00:00Z", "lastEditedAt": None,
                 **row} for row in self.rows]
        self.path.write_text(json.dumps({"actor": self.actor, "head": self.head, "rows": rows}))
