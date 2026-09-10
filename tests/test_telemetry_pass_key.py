"""Retries share a key; different reviewers and restarted runs do not."""

import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("harness", [".claude", ".codex", ".agents"])
def test_pass_identity(harness: str) -> None:
    script = ROOT / harness / "skills/critique/scripts/telemetry-pass-key.js"

    def key(run: str = "a" * 64, actor: str = "reviewer", kind: str = "review") -> str:
        result = subprocess.run(
            [
                "node",
                str(script),
                "owner/repo",
                "123",
                run,
                actor,
                "codex",
                kind,
                "1",
                "b" * 40,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        assert payload["error"] is None
        return str(payload["idempotencyKey"])

    assert key() == key(actor="REVIEWER")
    assert key() != key(run="c" * 64)
    assert key() != key(actor="other")
    assert key() != key(kind="refactor")
    assert key(run="standalone") != key(run="standalone")
