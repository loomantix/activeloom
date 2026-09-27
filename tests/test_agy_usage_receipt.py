"""Only a completed, fresh Agy invocation may supply aggregate usage."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate the launcher's numeric-only receipt from real review sessions."""
    spec = importlib.util.spec_from_file_location(
        "launch_usage", ROOT / ".codex/skills/critique/scripts/review-launch-state.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "result.json"
    target = tmp_path / "usage.json"
    monkeypatch.setenv("ACTIVELOOM_AGY_USAGE_FILE", str(target))
    monkeypatch.setenv("ACTIVELOOM_ATTEMPT_ID", "attempt-1")
    payload = {
        "status": "SUCCESS",
        "num_turns": 1,
        "response": "PRIVATE",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "thinking_tokens": 15,
            "cache_read_tokens": 50,
            "total_tokens": 120,
        },
    }
    return module, source, target, payload


def test_receipt_preserves_only_numeric_usage(receipt: Any) -> None:
    """Never retain response text, conversation identifiers or requested models."""
    module, source, target, payload = receipt
    source.write_text(json.dumps(payload))
    module.record_usage(str(source))
    result = json.loads(target.read_text())
    assert result == {
        "version": 1,
        "attempt_id": "attempt-1",
        "usage": payload["usage"],
    }
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "change",
    [
        {"num_turns": 2},
        {"num_turns": True},
        {"status": "ERROR"},
        {"usage": {}},
        {"usage": {"input_tokens": -1}},
        {"usage": {"input_tokens": True}},
        {"usage": {"output_tokens": 1.5}},
        {"usage": {"input_tokens": 2**53}},
    ],
)
def test_receipt_rejects_unmeasured_or_resumed_usage(
    receipt: Any, change: dict[str, Any]
) -> None:
    """Absent, malformed and cumulative usage cannot become a measured pass."""
    module, source, target, payload = receipt
    payload.update(change)
    source.write_text(json.dumps(payload))
    module.record_usage(str(source))
    assert not target.exists()


@pytest.mark.parametrize("preseed", ["file", "symlink"])
def test_receipt_replaces_an_untrusted_destination(receipt: Any, preseed: str) -> None:
    """Child-created files and symlinks cannot win the post-exit receipt race."""
    module, source, target, payload = receipt
    source.write_text(json.dumps(payload))
    if preseed == "file":
        target.write_text(
            json.dumps(
                {
                    "version": 1,
                    "attempt_id": "attempt-1",
                    "usage": {"input_tokens": 999_999},
                }
            )
        )
    else:
        target.symlink_to(source)
    module.record_usage(str(source))
    assert not target.is_symlink()
    assert json.loads(target.read_text()) == {
        "version": 1,
        "attempt_id": "attempt-1",
        "usage": payload["usage"],
    }
    assert json.loads(source.read_text()) == payload


def test_receipt_opt_out_does_not_read_the_result(
    receipt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner supplies no destination when extraction is disabled."""
    module, source, target, _ = receipt
    monkeypatch.delenv("ACTIVELOOM_AGY_USAGE_FILE")
    module.record_usage(str(source))
    assert not target.exists()
