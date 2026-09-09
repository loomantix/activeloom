"""Exact session identity must survive moving a shell into a linked worktree."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any, cast

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".codex/skills/critique/scripts/usage-snapshot.js"


def event(kind: str, payload: dict[str, Any]) -> str:
    return (
        json.dumps(
            {"type": kind, "timestamp": "2026-08-20T12:00:00.000Z", "payload": payload}
        )
        + "\n"
    )


def log(path: Path, cwd: Path, identity: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        event("session_meta", {"id": identity, "cwd": str(cwd), "cli_version": "1.0.0"})
    )


def invoke(
    tmp_path: Path,
    *args: str,
    identity: str | None = "selected",
    id_env: str = "CODEX_THREAD_ID",
) -> dict[str, Any]:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    env = dict(os.environ)
    for name in (
        "CODEX_SESSION_LOG",
        "CODEX_SESSIONS_DIR",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
    ):
        env.pop(name, None)
    env.update(LOOM_REVIEW_TELEMETRY_EXTRACT="on", LOOM_REVIEW_TELEMETRY="off")
    if identity is not None:
        env[id_env] = identity
    result = subprocess.run(
        ["node", str(SCRIPT), *args, "--sessions-dir", str(tmp_path / "sessions")],
        env=env,
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    )
    return cast(dict[str, Any], json.loads(result.stdout))


def snapshot(
    tmp_path: Path,
    *args: str,
    identity: str | None = "selected",
    id_env: str = "CODEX_THREAD_ID",
) -> dict[str, Any]:
    return invoke(
        tmp_path,
        "snapshot",
        "--out",
        str(tmp_path / "start.json"),
        *args,
        identity=identity,
        id_env=id_env,
    )


def test_exact_host_identity_survives_worktree_change(tmp_path: Path) -> None:
    selected = tmp_path / "sessions/2026/08/20/rollout-selected.jsonl"
    log(selected, tmp_path / "primary", "selected")
    # Another session happens to use the worktree; it must never win by cwd.
    log(selected.with_name("rollout-other.jsonl"), tmp_path / "worktree", "other")
    result = snapshot(tmp_path)
    assert result["scoped"] is True
    assert result["sessionLog"] == str(selected)
    assert result["emit"] is False
    stored = json.loads((tmp_path / "start.json").read_text())
    assert stored["cwd"] == str(tmp_path / "primary")


def test_explicit_session_id_survives_worktree_change(tmp_path: Path) -> None:
    log(tmp_path / "sessions/rollout-selected.jsonl", tmp_path / "primary", "selected")
    assert (
        snapshot(tmp_path, "--session-id", "selected", identity=None)["scoped"] is True
    )


def test_worktree_snapshot_produces_scoped_model_token_delta(tmp_path: Path) -> None:
    selected = tmp_path / "sessions/rollout-selected.jsonl"
    log(selected, tmp_path / "primary", "selected")

    def counted(input_tokens: int, output_tokens: int) -> str:
        return event(
            "event_msg",
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": 0,
                    }
                },
            },
        )

    with selected.open("a") as stream:
        stream.write(event("turn_context", {"model": "test-model", "effort": "low"}))
        stream.write(counted(10, 2))
    snapshot(tmp_path)
    with selected.open("a") as stream:
        stream.write(counted(35, 7))
    result = invoke(
        tmp_path,
        "delta",
        "--start",
        str(tmp_path / "start.json"),
        "--out-dir",
        str(tmp_path / "delta"),
    )
    assert result["tokenSource"] == "session-log-delta"
    tokens = json.loads(Path(result["tokensFile"]).read_text())
    assert len(tokens) == 1
    assert tokens[0]["model"] == "test-model"
    assert tokens[0]["effort"] == "low"
    assert tokens[0]["input"] == 25
    assert tokens[0]["output"] == 5


def test_explicit_cwd_still_restricts_an_exact_identity(tmp_path: Path) -> None:
    log(tmp_path / "sessions/rollout-selected.jsonl", tmp_path / "primary", "selected")
    assert snapshot(tmp_path, "--cwd", str(tmp_path / "worktree"))["scoped"] is False


def test_without_identity_discovery_still_requires_matching_cwd(tmp_path: Path) -> None:
    log(tmp_path / "sessions/rollout-selected.jsonl", tmp_path / "primary", "selected")
    assert snapshot(tmp_path, identity=None)["scoped"] is False


def test_missing_exact_identity_never_falls_back_to_another_session(
    tmp_path: Path,
) -> None:
    log(tmp_path / "sessions/rollout-other.jsonl", tmp_path / "worktree", "other")
    assert snapshot(tmp_path)["scoped"] is False


def test_duplicate_identity_across_directories_abstains(tmp_path: Path) -> None:
    log(tmp_path / "sessions/rollout-one.jsonl", tmp_path / "primary", "selected")
    log(tmp_path / "sessions/rollout-two.jsonl", tmp_path / "worktree", "selected")
    assert snapshot(tmp_path)["scoped"] is False


@pytest.mark.parametrize("name", ["CODEX_THREAD_ID", "CODEX_SESSION_ID"])
def test_both_host_identity_variables_are_supported(
    tmp_path: Path, name: str
) -> None:
    log(tmp_path / "sessions/rollout-selected.jsonl", tmp_path / "primary", "selected")
    assert snapshot(tmp_path, id_env=name)["scoped"] is True
