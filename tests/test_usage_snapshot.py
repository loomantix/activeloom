"""Acceptance suite for the pass-scoped usage extractor.

The case names here are the shared contract: the sibling engine repository
runs the same scenarios against its own log format, so a behaviour that
diverges between engines shows up as a named case that only one side has.

What these assert is mostly about *not lying*. An extractor that guesses is
worse than one that abstains, because a plausible wrong number is
indistinguishable from a right one once it reaches an aggregate. So the cases
below pin the abstention paths — no log, no snapshot, a rewound log, a counter
reset, a bucket the CLI never reported — at least as hard as they pin the happy
path.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".codex" / "skills" / "critique" / "scripts" / "usage-snapshot.js"


def run(*args: str, enabled: bool = True, **env: str) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.pop("LOOM_REVIEW_TELEMETRY", None)
    environment.pop("CODEX_SESSION_LOG", None)
    environment.pop("CODEX_SESSIONS_DIR", None)
    if enabled:
        environment["LOOM_REVIEW_TELEMETRY"] = "on"
    environment.update(env)
    result = subprocess.run(
        ["node", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    # Emission never fails the pass that produced the record, so a non-zero
    # exit is itself a defect regardless of what went wrong inside.
    assert result.returncode == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def meta(cwd: Path, version: str = "0.148.0") -> str:
    return (
        json.dumps(
            {
                "timestamp": "2026-08-20T12:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "session_id": "01a0-session",
                    "cwd": str(cwd),
                    "cli_version": version,
                },
            }
        )
        + "\n"
    )


def context(model: str = "gpt-5.6-sol", effort: str = "medium") -> str:
    return (
        json.dumps(
            {
                "timestamp": "2026-08-20T12:00:01.000Z",
                "type": "turn_context",
                "payload": {"model": model, "effort": effort},
            }
        )
        + "\n"
    )


def counted(
    *,
    input_tokens: int,
    cached: int = 0,
    cache_write: int = 0,
    output: int = 0,
    reasoning: int | None = 0,
    timestamp: str = "2026-08-20T12:00:02.000Z",
) -> str:
    """One cumulative `token_count` event.

    The CLI reports session-to-date totals, so a fixture builds a timeline by
    handing successive calls increasing numbers.
    """
    totals: dict[str, Any] = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
    }
    if reasoning is not None:
        totals["reasoning_output_tokens"] = reasoning
    return (
        json.dumps(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": totals},
                },
            }
        )
        + "\n"
    )


@pytest.fixture
def session(tmp_path: Path) -> Path:
    """A sessions tree holding one rollout log whose header names `tmp_path`."""
    day = tmp_path / "sessions" / "2026" / "08" / "20"
    day.mkdir(parents=True)
    log = day / "rollout-2026-08-20T12-00-00-01a0-session.jsonl"
    log.write_text(meta(tmp_path))
    return log


def snapshot(session: Path, tmp_path: Path) -> Path:
    out = tmp_path / "start.json"
    payload = run("snapshot", "--out", str(out), "--session-log", str(session))
    assert payload["enabled"] is True
    assert payload["scoped"] is True
    return out


def delta(tmp_path: Path, start: Path | None = None, **extra: str) -> dict[str, Any]:
    args = ["delta", "--out-dir", str(tmp_path / "out")]
    if start is not None:
        args += ["--start", str(start)]
    for key, value in extra.items():
        args += [f"--{key.replace('_', '-')}", value]
    return run(*args)


def tokens_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    assert payload["tokensFile"] is not None
    records: list[dict[str, Any]] = json.loads(Path(payload["tokensFile"]).read_text())
    return records


def test_gate_off_writes_nothing(tmp_path: Path, session: Path) -> None:
    """Emission is opt-in, and a gated-off run must not touch the filesystem."""
    out = tmp_path / "start.json"
    payload = run("snapshot", "--out", str(out), enabled=False)
    assert payload["enabled"] is False
    assert payload["reason"] == "LOOM_REVIEW_TELEMETRY is unset"
    assert not out.exists()


def test_gate_rejects_an_unrecognised_value(tmp_path: Path) -> None:
    """A typo must read as off, never as on."""
    payload = run(
        "snapshot",
        "--out",
        str(tmp_path / "start.json"),
        enabled=False,
        LOOM_REVIEW_TELEMETRY="true",
    )
    assert payload["enabled"] is False


def test_no_session_log_reports_unavailable(tmp_path: Path) -> None:
    """No data must never serialise as zero tokens."""
    empty = tmp_path / "sessions"
    empty.mkdir()
    payload = delta(tmp_path, sessions_dir=str(empty), cwd=str(tmp_path))
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None
    assert payload["lanesFile"] is None


def test_discovery_requires_the_working_directory_to_match(tmp_path: Path) -> None:
    """The most recent session on the machine is not necessarily this one."""
    day = tmp_path / "sessions" / "2026" / "08" / "20"
    day.mkdir(parents=True)
    (day / "rollout-elsewhere.jsonl").write_text(
        meta(tmp_path / "somewhere-else") + context() + counted(input_tokens=5)
    )
    payload = delta(
        tmp_path, sessions_dir=str(tmp_path / "sessions"), cwd=str(tmp_path)
    )
    assert payload["tokenSource"] == "unavailable"


def test_scoped_delta_counts_only_the_pass(tmp_path: Path, session: Path) -> None:
    """Work that predates the snapshot belongs to whatever ran before."""
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=9_000, cached=8_000, output=400, reasoning=90))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=9_150, cached=8_100, output=460, reasoning=100))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "session-log-delta"
    assert payload["engineVersion"] == "0.148.0"
    assert tokens_of(payload) == [
        {
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "input": 50,
            "output": 60,
            "cacheRead": 100,
            "cacheWrite": 0,
            "reasoning": 10,
            "providerBuckets": {"reported_input_tokens": 150},
        }
    ]


def test_canonical_buckets_reconcile_with_the_reported_input(
    tmp_path: Path, session: Path
) -> None:
    """The canonical buckets are disjoint so each can be priced separately.

    The CLI reports the whole prompt side as `input_tokens` with the cached and
    cache-write portions inside it, so the projection has to subtract. Keeping
    the reported figure alongside is what makes that arithmetic checkable later
    instead of lossy.
    """
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(context())
        handle.write(
            counted(input_tokens=1_000, cached=700, cache_write=200, output=50)
        )

    bucket = tokens_of(delta(tmp_path, start))[0]
    assert bucket["input"] == 100
    assert (
        bucket["input"] + bucket["cacheRead"] + bucket["cacheWrite"]
        == bucket["providerBuckets"]["reported_input_tokens"]
    )


def test_unscoped_without_a_snapshot_is_labelled_as_such(
    tmp_path: Path, session: Path
) -> None:
    """A standalone pass gets a truthful upper bound, not a wrong number."""
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=500, output=5))

    payload = delta(tmp_path, None, session_log=str(session))
    assert payload["tokenSource"] == "unscoped-session"
    assert tokens_of(payload)[0]["output"] == 5


def test_a_rewound_log_downgrades_to_unscoped(tmp_path: Path, session: Path) -> None:
    """A recorded offset past the end of the file no longer marks the start."""
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=5_000, output=300))
    start = snapshot(session, tmp_path)
    session.write_text(meta(tmp_path) + context() + counted(input_tokens=40, output=7))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unscoped-session"
    assert tokens_of(payload)[0]["output"] == 7


def test_a_counter_reset_downgrades_to_unscoped(
    tmp_path: Path, session: Path
) -> None:
    """A cumulative counter that goes backwards invalidates the subtraction.

    Reporting the difference anyway would emit a negative or wildly wrong
    count; reporting the end total under an honest label keeps the record
    usable as a bound.
    """
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=9_000, output=400))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=120, output=9))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unscoped-session"


def test_an_unreported_bucket_stays_null(tmp_path: Path, session: Path) -> None:
    """`null` and `0` are different answers all the way to the record."""
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=40, output=4, reasoning=None))

    bucket = tokens_of(delta(tmp_path, start))[0]
    assert bucket["reasoning"] is None
    assert bucket["output"] == 4


def test_a_reported_zero_stays_zero(tmp_path: Path, session: Path) -> None:
    """The converse: a measured zero must not be erased into `null`."""
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=40, output=4, reasoning=0))

    assert tokens_of(delta(tmp_path, start))[0]["reasoning"] == 0


def test_multiple_models_get_one_bucket_each(tmp_path: Path, session: Path) -> None:
    """A pass may span models, and a single scalar would be unpriceable."""
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=1_000, output=100))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=1_030, output=110))
        handle.write(context(model="gpt-5.6-sol", effort="high"))
        handle.write(counted(input_tokens=1_070, output=135))

    buckets = {
        (bucket["model"], bucket["effort"]): bucket["output"]
        for bucket in tokens_of(delta(tmp_path, start))
    }
    assert buckets == {
        ("gpt-5.6-sol", "medium"): 10,
        ("gpt-5.6-sol", "high"): 25,
    }


def test_a_repeated_token_count_is_not_double_counted(
    tmp_path: Path, session: Path
) -> None:
    """The failure this guards against is silent and one-directional.

    A `token_count` event is also emitted for a rate-limit refresh, restating
    the previous turn's totals. Summing the per-turn figures those events carry
    inflates the pass; reading the cumulative total does not.
    """
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=1_000, output=100))
        handle.write(counted(input_tokens=1_000, output=100))
        handle.write(counted(input_tokens=1_000, output=100))

    bucket = tokens_of(delta(tmp_path, start))[0]
    assert bucket["output"] == 100
    assert bucket["providerBuckets"]["reported_input_tokens"] == 1_000


def test_lanes_are_absent_rather_than_empty(tmp_path: Path, session: Path) -> None:
    """This engine reports no per-lane attribution, and the record schema
    rejects an empty lane list."""
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=40, output=4))

    assert delta(tmp_path, start)["lanesFile"] is None


def test_a_malformed_trailing_line_does_not_lose_the_record(
    tmp_path: Path, session: Path
) -> None:
    """A log still being appended to routinely ends mid-line."""
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=40, output=42))
        handle.write('{"type":"event_msg","payload":{"type":"token_c')

    assert tokens_of(delta(tmp_path, start))[0]["output"] == 42


def test_an_internal_error_reports_unavailable_and_exits_zero(
    tmp_path: Path,
) -> None:
    """A telemetry defect must never block a review that found real defects."""
    payload = run("delta")
    assert payload["tokenSource"] == "unavailable"
    assert payload["error"]


def test_no_bucket_is_ever_negative(tmp_path: Path, session: Path) -> None:
    """The record rejects a negative count, so a degraded read must degrade
    into an over-count rather than into an unemittable one.

    This is the invariant that keeps the non-fatal rule from quietly becoming a
    never-emits rule: every abstention path still has to produce something the
    validator will accept.
    """
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=9_000, cached=8_000, output=400))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=120, cached=90, output=9))

    for bucket in tokens_of(delta(tmp_path, start)):
        for key, value in bucket.items():
            if isinstance(value, int):
                assert value >= 0, key
