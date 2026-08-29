"""Shared `local-review-run:v1` marker builder for the launcher contract tests.

The digest is recomputed here by hand rather than by calling
`local-review-handoff.py`'s `_run_digest`. That is deliberate: these tests exist
to prove the launchers accept a marker built to the published grammar, so the
expected digest has to come from an oracle independent of the implementation
under test. Sharing the *oracle* between the launcher suites removes a genuine
copy without collapsing it into the production code path.
"""

from __future__ import annotations

import hashlib
import json


HEAD = "a" * 40


def run_comment(head: str = HEAD) -> str:
    """Return an authorizing run-marker comment body for `head`."""
    content = "Review explicitly authorized."
    payload = {
        "base": head,
        "content": content,
        "max_rounds": 4,
        "start_head": head,
        "supersedes": None,
        "tier": "deep",
    }
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return (
        f"<!-- local-review-run:v1 id={digest} tier=deep max-rounds=4 "
        f"base={head} start-head={head} supersedes=none "
        f"content-sha256={digest} -->\n{content}"
    )
