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
    environment.pop("CODEX_SESSION_ID", None)
    environment.pop("CODEX_THREAD_ID", None)
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


def json_line(event: dict[str, Any]) -> str:
    return json.dumps(event) + "\n"


def meta(
    cwd: Path,
    version: str = "0.148.0",
    session_id: str = "01a0-session",
    parent_thread_id: str | None = None,
) -> str:
    """A session header in the shape the CLI actually writes.

    Measured across the rollout headers under a real sessions root: a root
    session carries `id == session_id`, while a child carries its own `id` and
    repeats its parent's id in **both** `session_id` and `parent_thread_id`.
    Writing `session_id == id` on a child would be a shape the CLI never emits,
    and it hides the case where a log's own identity has to be read from `id`.
    """
    payload = {
        "id": session_id,
        "session_id": parent_thread_id if parent_thread_id is not None else session_id,
        "cwd": str(cwd),
        "cli_version": version,
    }
    if parent_thread_id is not None:
        payload["parent_thread_id"] = parent_thread_id
    return json_line(
        {
            "timestamp": "2026-08-20T12:00:00.000Z",
            "type": "session_meta",
            "payload": payload,
        }
    )


def context(model: str = "gpt-5.6-sol", effort: str = "medium") -> str:
    return json_line(
        {
            "timestamp": "2026-08-20T12:00:01.000Z",
            "type": "turn_context",
            "payload": {"model": model, "effort": effort},
        }
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
    return json_line(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": totals},
            },
        }
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
    payload = run(
        "snapshot",
        "--out",
        str(out),
        "--session-log",
        str(session),
        "--sessions-dir",
        str(session.parents[3]),
    )
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


def test_gate_off_writes_nothing(tmp_path: Path) -> None:
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


@pytest.mark.parametrize("value", ["ON", " on ", "On"])
def test_gate_requires_exact_on(tmp_path: Path, value: str) -> None:
    payload = run(
        "snapshot",
        "--out",
        str(tmp_path / "start.json"),
        enabled=False,
        LOOM_REVIEW_TELEMETRY=value,
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


def test_discovery_uses_the_host_session_id_with_same_cwd_logs(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2026" / "08" / "20"
    day.mkdir(parents=True)
    selected = day / "rollout-selected.jsonl"
    selected.write_text(
        meta(tmp_path, session_id="selected")
        + context()
        + counted(input_tokens=10, output=2)
    )
    (day / "rollout-other.jsonl").write_text(
        meta(tmp_path, session_id="other")
        + context()
        + counted(input_tokens=999, output=999)
    )

    payload = delta(
        tmp_path,
        sessions_dir=str(tmp_path / "sessions"),
        cwd=str(tmp_path),
        session_id="selected",
    )

    assert payload["tokenSource"] == "unscoped-session"
    assert tokens_of(payload)[0]["output"] == 2


def test_discovery_abstains_when_same_cwd_logs_are_ambiguous(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2026" / "08" / "20"
    day.mkdir(parents=True)
    for session_id in ("first", "second"):
        (day / f"rollout-{session_id}.jsonl").write_text(
            meta(tmp_path, session_id=session_id)
            + context()
            + counted(input_tokens=10, output=2)
        )

    payload = delta(
        tmp_path, sessions_dir=str(tmp_path / "sessions"), cwd=str(tmp_path)
    )

    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None


def test_scoped_delta_counts_only_the_pass(tmp_path: Path, session: Path) -> None:
    """Work that predates the snapshot belongs to whatever ran before."""
    with session.open("a") as handle:
        handle.write(context())
        handle.write(
            counted(input_tokens=9_000, cached=8_000, output=400, reasoning=90)
        )
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(
            counted(input_tokens=9_150, cached=8_100, output=460, reasoning=100)
        )

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


def test_first_cumulative_event_after_snapshot_is_not_assumed_to_start_at_zero(
    tmp_path: Path, session: Path
) -> None:
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=500, output=20))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None


def test_canonical_buckets_reconcile_with_the_reported_input(
    tmp_path: Path, session: Path
) -> None:
    """The canonical buckets are disjoint so each can be priced separately.

    The CLI reports the whole prompt side as `input_tokens` with the cached and
    cache-write portions inside it, so the projection has to subtract. Keeping
    the reported figure alongside is what makes that arithmetic checkable later
    instead of lossy.
    """
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=0, cached=0, cache_write=0, output=0))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
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


def test_a_replaced_session_log_is_not_subtracted(
    tmp_path: Path, session: Path
) -> None:
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=100, output=10))
    start = snapshot(session, tmp_path)
    session.write_text(
        meta(tmp_path, session_id="replacement")
        + context()
        + counted(input_tokens=200, output=20)
    )

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None


def test_a_counter_reset_downgrades_to_unscoped(tmp_path: Path, session: Path) -> None:
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
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=10, output=1, reasoning=None))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=40, output=4, reasoning=5))

    bucket = tokens_of(delta(tmp_path, start))[0]
    assert bucket["reasoning"] is None
    assert bucket["output"] == 3


def test_a_bucket_first_reported_after_snapshot_stays_null(
    tmp_path: Path, session: Path
) -> None:
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=10, output=1, reasoning=None))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=20, output=2, reasoning=7))

    assert tokens_of(delta(tmp_path, start))[0]["reasoning"] is None


def test_a_reported_zero_stays_zero(tmp_path: Path, session: Path) -> None:
    """The converse: a measured zero must not be erased into `null`."""
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=10, output=1, reasoning=0))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
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


def test_unknown_model_interval_poisoning_is_preserved(
    tmp_path: Path, session: Path
) -> None:
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=100, output=10, reasoning=None))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=110, output=11, reasoning=3))
        handle.write(context(effort="high"))
        handle.write(counted(input_tokens=120, output=12, reasoning=5))
        handle.write(context(effort="medium"))
        handle.write(counted(input_tokens=130, output=13, reasoning=8))

    medium = next(
        bucket
        for bucket in tokens_of(delta(tmp_path, start))
        if bucket["effort"] == "medium"
    )
    assert medium["reasoning"] is None


def test_inconsistent_provider_subsets_make_usage_unavailable(
    tmp_path: Path, session: Path
) -> None:
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=0, cached=0, output=0))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=100, cached=120, output=5))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None


def test_snapshot_uses_one_complete_jsonl_boundary(
    tmp_path: Path, session: Path
) -> None:
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=100, output=10))
    straddling = counted(input_tokens=120, output=12)
    split = len(straddling) // 2
    with session.open("a") as handle:
        handle.write(straddling[:split])

    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(straddling[split:])
        handle.write(counted(input_tokens=130, output=13))

    bucket = tokens_of(delta(tmp_path, start))[0]
    assert bucket["providerBuckets"]["reported_input_tokens"] == 30
    assert bucket["output"] == 3


def test_new_descendant_sessions_are_included_in_the_pass(
    tmp_path: Path, session: Path
) -> None:
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=100, output=10))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=110, output=11))

    child = session.with_name("rollout-child.jsonl")
    child.write_text(
        meta(tmp_path, session_id="child", parent_thread_id="01a0-session")
        + context()
        + counted(input_tokens=20, output=2)
    )

    bucket = tokens_of(delta(tmp_path, start))[0]
    assert bucket["providerBuckets"]["reported_input_tokens"] == 30
    assert bucket["output"] == 3


def test_a_repeated_token_count_is_not_double_counted(
    tmp_path: Path, session: Path
) -> None:
    """The failure this guards against is silent and one-directional.

    A `token_count` event is also emitted for a rate-limit refresh, restating
    the previous turn's totals. Summing the per-turn figures those events carry
    inflates the pass; reading the cumulative total does not.
    """
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=0, output=0))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=1_000, output=100))
        handle.write(counted(input_tokens=1_000, output=100))
        handle.write(counted(input_tokens=1_000, output=100))

    bucket = tokens_of(delta(tmp_path, start))[0]
    assert bucket["output"] == 100
    assert bucket["providerBuckets"]["reported_input_tokens"] == 1_000


def test_lanes_are_absent_rather_than_empty(tmp_path: Path, session: Path) -> None:
    """This engine reports no per-lane attribution, and the record schema
    rejects an empty lane list."""
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=0, output=0))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=40, output=4))

    assert delta(tmp_path, start)["lanesFile"] is None


def test_an_incomplete_trailing_line_makes_usage_unavailable(
    tmp_path: Path, session: Path
) -> None:
    """A log still being appended to routinely ends mid-line."""
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=40, output=42))
        handle.write('{"type":"event_msg","payload":{"type":"token_c')

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None


def test_an_internal_error_reports_unavailable_and_exits_zero() -> None:
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


def test_a_log_own_identity_is_read_from_id_not_session_id(
    tmp_path: Path, session: Path
) -> None:
    """A child header repeats its parent's id in `session_id`.

    Reading identity from `session_id` first gives a child the parent's id,
    which makes the descendant walk reject every child as already-seen and
    makes discovery by host session id ambiguous. Both failures are silent:
    the record still claims `session-log-delta`.
    """
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=100, output=10))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=150, output=20))

    child = session.with_name("rollout-child.jsonl")
    child.write_text(
        meta(tmp_path, session_id="01a0-child", parent_thread_id="01a0-session")
        + context()
        + counted(input_tokens=9000, output=900)
    )

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "session-log-delta"
    bucket = tokens_of(payload)[0]
    assert bucket["providerBuckets"]["reported_input_tokens"] == 9050
    assert bucket["output"] == 910


def test_discovery_by_host_session_id_ignores_child_logs(
    tmp_path: Path, session: Path
) -> None:
    """A child shares the cwd but is not a second candidate for the root id."""
    session.with_name("rollout-child.jsonl").write_text(
        meta(tmp_path, session_id="01a0-child", parent_thread_id="01a0-session")
        + context()
        + counted(input_tokens=5, output=1)
    )
    payload = run(
        "snapshot",
        "--out",
        str(tmp_path / "start.json"),
        "--session-id",
        "01a0-session",
        "--sessions-dir",
        str(session.parents[3]),
        "--cwd",
        str(tmp_path),
    )
    assert payload["sessionLog"] == str(session)
    assert payload["scoped"] is True


def test_an_unparseable_event_in_the_window_makes_usage_unavailable(
    tmp_path: Path, session: Path
) -> None:
    """A complete-but-corrupt line may have been the closing `token_count`.

    The incomplete-trailing-line guard covers the newline boundary, not JSON
    validity, so without this the window silently under-reports and is still
    labelled exact.
    """
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=100, output=10))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=200, output=20))
        handle.write('{"type":"event_msg","payload":{"type":"token_c' + "\n")

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None
    assert payload["error"] == "the session log window contained an unparseable event"


def test_an_unmeasurable_interval_is_not_dropped_silently(
    tmp_path: Path, session: Path
) -> None:
    """Dropping an interval removes usage from the record.

    One surviving bucket would otherwise hide the loss behind a
    `session-log-delta` label, under-reporting the pass by whatever the dropped
    interval cost.
    """
    with session.open("a") as handle:
        handle.write(context(effort="medium"))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=5000, output=500))
        handle.write(context(effort="high"))
        handle.write(counted(input_tokens=5100, output=530))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None
    assert payload["error"] == "an interval had no measurable baseline"


def test_a_descendant_with_no_usage_yet_keeps_the_record(
    tmp_path: Path, session: Path
) -> None:
    """A spawned-but-idle child is an ordinary state, not corrupt data."""
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=100, output=10))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(counted(input_tokens=1100, output=510))

    session.with_name("rollout-idle.jsonl").write_text(
        meta(tmp_path, session_id="01a0-idle", parent_thread_id="01a0-session")
    )

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "session-log-delta"
    assert tokens_of(payload)[0]["output"] == 500


def test_snapshot_creates_its_own_output_directory(
    tmp_path: Path, session: Path
) -> None:
    """The documented invocation writes into a telemetry dir nothing creates."""
    out = tmp_path / "telemetry" / "usage-start.json"
    payload = run(
        "snapshot",
        "--out",
        str(out),
        "--session-log",
        str(session),
        "--sessions-dir",
        str(session.parents[3]),
    )
    assert payload["error"] is None
    assert payload["scoped"] is True
    assert out.exists()


def test_telemetry_files_are_owner_only_and_never_follow_a_symlink(
    tmp_path: Path, session: Path
) -> None:
    """`mode` on write applies only at creation, so it is enforced explicitly."""
    start = tmp_path / "start.json"
    start.write_text("{}")
    start.chmod(0o644)
    snapshot(session, tmp_path)
    assert start.stat().st_mode & 0o777 == 0o600

    outdir = tmp_path / "out"
    outdir.mkdir(mode=0o755)
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL")
    (outdir / "telemetry-tokens.json").symlink_to(victim)
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=10, output=1))

    delta(tmp_path, start)
    assert victim.read_text() == "ORIGINAL"
    assert outdir.stat().st_mode & 0o777 == 0o700


def test_a_tampered_snapshot_cannot_put_free_text_in_the_record(
    tmp_path: Path, session: Path
) -> None:
    """`model` and `effort` become the bucket identity in a public record."""
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=100, output=10))
    start = snapshot(session, tmp_path)
    payload = json.loads(start.read_text())
    payload["model"] = "MODEL /home/someone/private path prose"
    payload["effort"] = "<script>"
    start.write_text(json.dumps(payload))
    with session.open("a") as handle:
        handle.write(counted(input_tokens=150, output=15))

    result = delta(tmp_path, start)
    assert result["tokensFile"] is None or all(
        " " not in record["model"] for record in tokens_of(result)
    )


def test_a_rejected_start_snapshot_says_so_and_does_not_rediscover(
    tmp_path: Path, session: Path
) -> None:
    """A scoped pass whose baseline is gone must not fall back to discovery.

    Falling back is the silent retarget the discovery contract rules out, and
    it lands as `unscoped-session` with no sign the start file was refused.
    """
    with session.open("a") as handle:
        handle.write(context())
        handle.write(counted(input_tokens=100, output=10))
    start = snapshot(session, tmp_path)
    stale = json.loads(start.read_text())
    stale["version"] = 1
    start.write_text(json.dumps(stale))
    with session.open("a") as handle:
        handle.write(counted(input_tokens=110, output=11))

    payload = delta(
        tmp_path,
        start,
        sessions_dir=str(session.parents[3]),
        cwd=str(tmp_path),
    )
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None
    assert payload["error"] == "the start snapshot is version 1, not 2"
