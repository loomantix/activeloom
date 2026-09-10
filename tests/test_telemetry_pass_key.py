"""Retries share a key; different reviewers and restarted runs do not."""

import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("harness", [".claude", ".codex", ".agents"])
def test_pass_identity(harness: str) -> None:
    script = ROOT / harness / "skills/critique/scripts/telemetry-pass-key.js"

    def run_key(
        repo: str = "owner/repo",
        pr: str = "123",
        run: str = "a" * 64,
        actor: str = "reviewer",
        engine: str = "codex",
        kind: str = "review",
        round_: str = "1",
        head: str = "b" * 40,
    ) -> dict[str, object]:
        result = subprocess.run(
            ["node", str(script), repo, pr, run, actor, engine, kind, round_, head],
            capture_output=True,
            text=True,
            check=True,
        )
        return dict(json.loads(result.stdout))

    def key(**kwargs: str) -> str:
        payload = run_key(**kwargs)
        assert payload["error"] is None
        return str(payload["idempotencyKey"])

    assert key() == key(actor="REVIEWER")
    assert key() != key(run="c" * 64)
    assert key() != key(actor="other")
    assert key() != key(kind="refactor")
    assert key(run="standalone") != key(run="standalone")

    # Every component of the identity must separate two passes. `head` is the
    # exact-head anchor the ledger turns on and `engine` separates reviewers in
    # one round: if either dropped out of the hash, a pass over a superseded
    # commit — or another engine's pass — would be reconciled away as a retry.
    assert key() != key(repo="owner/other")
    assert key() != key(pr="124")
    assert key() != key(engine="claude")
    assert key() != key(round_="2")
    assert key() != key(head="c" * 40)

    # A rejected argument yields no key. Without this the validation branch is
    # unobserved, and a loosened pattern would ship silently.
    for bad in (
        {"repo": "owner"},
        {"pr": "0"},
        {"run": "nope"},
        {"engine": "-bad"},
        {"kind": "cleanup"},
        {"round_": "0"},
        {"head": "b" * 39},
    ):
        rejected = run_key(**bad)
        assert rejected["idempotencyKey"] is None, bad
        assert rejected["error"], bad
