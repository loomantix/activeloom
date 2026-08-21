"""Acceptance suite for the pass-scoped usage extractor.

The case names here are the shared contract: the sibling engine repository
runs the same scenarios against its own log format, so a behaviour that
diverges between engines shows up as a named case that only one side has.

What these assert is mostly about *not lying*. An extractor that guesses is
worse than one that abstains, because a plausible wrong number is indistinguishable
from a right one once it reaches an aggregate. So the cases below pin the
abstention paths — no log, no snapshot, a rewound log, a bucket the CLI never
reported — at least as hard as they pin the happy path.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".claude" / "skills" / "critique" / "scripts" / "usage-snapshot.js"


def run(*args: str, enabled: bool = True, **env: str) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.pop("LOOM_REVIEW_TELEMETRY", None)
    environment.pop("CLAUDE_SESSION_LOG", None)
    environment.pop("CLAUDE_PROJECTS_DIR", None)
    environment.pop("CLAUDE_CODE_SESSION_ID", None)
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


def turn(
    *,
    request_id: str,
    output: int | None,
    model: str = "claude-opus-5",
    effort: str = "high",
    input_tokens: int | None = 10,
    cache_read: int | None = 100,
    cache_write: int | None = 50,
    thinking: int | None = 7,
    sidechain: bool = False,
    lens: str | None = None,
    version: str = "2.1.238",
    timestamp: str = "2026-08-20T12:00:00.000Z",
) -> str:
    usage: dict[str, Any] = {}
    for key, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output),
        ("cache_read_input_tokens", cache_read),
        ("cache_creation_input_tokens", cache_write),
    ):
        if value is not None:
            usage[key] = value
    if thinking is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking}
    entry: dict[str, Any] = {
        "type": "assistant",
        "requestId": request_id,
        "timestamp": timestamp,
        "version": version,
        "effort": effort,
        "isSidechain": sidechain,
        "message": {"id": f"msg_{request_id}", "model": model, "usage": usage},
    }
    if lens is not None:
        entry["attributionAgent"] = lens
    return json.dumps(entry) + "\n"


@pytest.fixture
def session(tmp_path: Path) -> Path:
    """A projects tree holding one session log for `tmp_path` as the cwd."""
    slug = str(tmp_path.resolve()).replace(os.sep, "-")
    project = tmp_path / "projects" / slug
    project.mkdir(parents=True)
    log = project / "0000-session.jsonl"
    log.write_text("")
    return log


def snapshot(session: Path, tmp_path: Path) -> Path:
    out = tmp_path / "start.json"
    payload = run(
        "snapshot",
        "--out",
        str(out),
        "--session-log",
        str(session),
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
    empty = tmp_path / "projects"
    empty.mkdir()
    payload = delta(tmp_path, projects_dir=str(empty), cwd=str(tmp_path))
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None
    assert payload["lanesFile"] is None


def test_scoped_delta_counts_only_the_pass(tmp_path: Path, session: Path) -> None:
    """Work that predates the snapshot belongs to whatever ran before."""
    session.write_text(turn(request_id="before", output=9_999))
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(turn(request_id="during", output=120))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "session-log-delta"
    assert payload["engineVersion"] == "2.1.238"
    assert tokens_of(payload) == [
        {
            "model": "claude-opus-5",
            "effort": "high",
            "input": 10,
            "output": 120,
            "cacheRead": 100,
            "cacheWrite": 50,
            "reasoning": 7,
        }
    ]


def test_discovery_binds_to_the_explicit_session_identity(
    tmp_path: Path, session: Path
) -> None:
    """A newer same-worktree session must not retarget this pass."""
    session.write_text(turn(request_id="before", output=10))
    other = session.with_name("other-session.jsonl")
    other.write_text(turn(request_id="other-before", output=20))
    out = tmp_path / "identity" / "start.json"
    payload = run(
        "snapshot",
        "--out",
        str(out),
        "--projects-dir",
        str(tmp_path / "projects"),
        "--cwd",
        str(tmp_path),
        CLAUDE_CODE_SESSION_ID=session.stem,
    )
    assert payload["sessionLog"] == str(session)
    assert payload["scoped"] is True

    with session.open("a") as handle:
        handle.write(turn(request_id="during", output=30))
    with other.open("a") as handle:
        handle.write(turn(request_id="other-during", output=40))

    assert tokens_of(delta(tmp_path, out))[0]["output"] == 30


def test_heuristic_discovery_never_claims_scoped_provenance(
    tmp_path: Path, session: Path
) -> None:
    """Newest-by-mtime is usable only as an explicitly unscoped upper bound."""
    session.write_text(turn(request_id="a", output=5))
    out = tmp_path / "heuristic" / "start.json"
    payload = run(
        "snapshot",
        "--out",
        str(out),
        "--projects-dir",
        str(tmp_path / "projects"),
        "--cwd",
        str(tmp_path),
    )
    assert payload["scoped"] is False
    with session.open("a") as handle:
        handle.write(turn(request_id="b", output=6))

    measured = delta(tmp_path, out, session_log=str(session))
    assert measured["tokenSource"] == "unscoped-session"


def test_snapshot_creates_its_owner_only_parent(tmp_path: Path, session: Path) -> None:
    out = tmp_path / "new" / "telemetry" / "start.json"
    payload = run("snapshot", "--out", str(out), "--session-log", str(session))
    assert payload["scoped"] is True
    assert out.exists()
    assert out.parent.stat().st_mode & 0o077 == 0


def test_unscoped_without_a_snapshot_is_labelled_as_such(
    tmp_path: Path, session: Path
) -> None:
    """A standalone pass gets a truthful upper bound, not a wrong number."""
    session.write_text(turn(request_id="a", output=5))
    payload = delta(tmp_path, None, session_log=str(session))
    assert payload["tokenSource"] == "unscoped-session"
    assert tokens_of(payload)[0]["output"] == 5


def test_a_rewound_log_downgrades_to_unscoped(tmp_path: Path, session: Path) -> None:
    """A recorded offset past the end of the file no longer marks the start."""
    session.write_text(turn(request_id="a", output=100))
    start = snapshot(session, tmp_path)
    session.write_text(turn(request_id="b", output=7))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unscoped-session"
    assert tokens_of(payload)[0]["output"] == 7


def test_an_unreported_bucket_stays_null(tmp_path: Path, session: Path) -> None:
    """`null` and `0` are different answers all the way to the record."""
    session.write_text("")
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(turn(request_id="a", output=4, thinking=None))

    bucket = tokens_of(delta(tmp_path, start))[0]
    assert bucket["reasoning"] is None
    assert bucket["output"] == 4


def test_a_reported_zero_stays_zero(tmp_path: Path, session: Path) -> None:
    """The converse: a measured zero must not be erased into `null`."""
    session.write_text("")
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(turn(request_id="a", output=4, thinking=0))

    assert tokens_of(delta(tmp_path, start))[0]["reasoning"] == 0


@pytest.mark.parametrize(
    ("argument", "bucket"),
    [
        ("input_tokens", "input"),
        ("output", "output"),
        ("cache_read", "cacheRead"),
        ("cache_write", "cacheWrite"),
        ("thinking", "reasoning"),
    ],
)
@pytest.mark.parametrize(("value", "expected"), [(None, None), (0, 0), (-1, None)])
def test_each_bucket_preserves_missing_zero_and_negative_semantics(
    tmp_path: Path,
    session: Path,
    argument: str,
    bucket: str,
    value: int | None,
    expected: int | None,
) -> None:
    """Missing/invalid is null; a provider-reported zero remains measured."""
    start = snapshot(session, tmp_path)
    values: dict[str, Any] = {
        "request_id": "a",
        "output": 4,
        "sidechain": True,
        "lens": "test-lens",
    }
    values[argument] = value
    with session.open("a") as handle:
        handle.write(turn(**values))

    payload = delta(tmp_path, start)
    assert tokens_of(payload)[0][bucket] == expected
    lanes = json.loads(Path(payload["lanesFile"]).read_text())
    assert lanes[0][bucket] == expected


def test_no_post_snapshot_measurement_is_unavailable(
    tmp_path: Path, session: Path
) -> None:
    """An empty scoped range carries no plausible all-null token bucket."""
    start = snapshot(session, tmp_path)
    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None
    assert payload["lanesFile"] is None


def test_multiple_models_get_one_bucket_each(tmp_path: Path, session: Path) -> None:
    """A pass may span models, and a single scalar would be unpriceable."""
    session.write_text("")
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(turn(request_id="a", output=10, model="claude-opus-5"))
        handle.write(turn(request_id="b", output=20, model="claude-haiku-4-5"))
        handle.write(
            turn(request_id="c", output=30, model="claude-opus-5", effort="low")
        )

    buckets = {
        (bucket["model"], bucket["effort"]): bucket["output"]
        for bucket in tokens_of(delta(tmp_path, start))
    }
    assert buckets == {
        ("claude-opus-5", "high"): 10,
        ("claude-haiku-4-5", "high"): 20,
        ("claude-opus-5", "low"): 30,
    }


def test_a_streaming_turn_is_counted_once_at_its_final_usage(
    tmp_path: Path, session: Path
) -> None:
    """The failure this guards against is silent and one-directional.

    A turn is appended repeatedly while it streams, with the input and cache
    buckets fixed and `output_tokens` climbing to its final value. Counting
    every occurrence multiplies the input side; keeping the first records a
    fraction of the output. Both produce a plausible number.
    """
    session.write_text("")
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(turn(request_id="a", output=1))
        handle.write(turn(request_id="a", output=180))
        handle.write(turn(request_id="a", output=4_122))

    bucket = tokens_of(delta(tmp_path, start))[0]
    assert bucket["output"] == 4_122
    assert bucket["input"] == 10
    assert bucket["cacheRead"] == 100


def test_subagent_lanes_are_attributed_and_included(
    tmp_path: Path, session: Path
) -> None:
    """Lane turns live beside the session log, so a sum over it alone would
    omit the bulk of a fanned-out pass."""
    session.write_text("")
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(turn(request_id="main", output=11))
    subagents = session.parent / session.stem / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-1.jsonl").write_text(
        turn(
            request_id="lane-1",
            output=500,
            sidechain=True,
            lens="pr-review-toolkit:code-reviewer",
        )
    )
    (subagents / "agent-2.jsonl").write_text(
        turn(
            request_id="lane-2",
            output=700,
            sidechain=True,
            lens="pr-review-toolkit:security-review",
        )
    )

    payload = delta(tmp_path, start)
    assert tokens_of(payload)[0]["output"] == 11 + 500 + 700

    lanes = json.loads(Path(payload["lanesFile"]).read_text())
    assert {lane["lens"]: lane["output"] for lane in lanes} == {
        "pr-review-toolkit:code-reviewer": 500,
        "pr-review-toolkit:security-review": 700,
    }


def test_lanes_are_absent_rather_than_empty(tmp_path: Path, session: Path) -> None:
    """The record schema rejects an empty lane list, so unattributable means
    no file at all."""
    session.write_text("")
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(turn(request_id="main", output=11))

    assert delta(tmp_path, start)["lanesFile"] is None


def test_a_malformed_trailing_line_does_not_lose_the_record(
    tmp_path: Path, session: Path
) -> None:
    """A log still being appended to routinely ends mid-line."""
    session.write_text("")
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(turn(request_id="a", output=42))
        handle.write('{"type":"assistant","message":{"usa')

    assert tokens_of(delta(tmp_path, start))[0]["output"] == 42


def test_mid_log_corruption_downgrades_instead_of_claiming_scoped(
    tmp_path: Path, session: Path
) -> None:
    start = snapshot(session, tmp_path)
    with session.open("a") as handle:
        handle.write(turn(request_id="a", output=42))
        handle.write("not-json\n")
        handle.write(turn(request_id="b", output=43))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None


def test_an_internal_error_reports_unavailable_and_exits_zero(
    tmp_path: Path,
) -> None:
    """A telemetry defect must never block a review that found real defects."""
    payload = run("delta")
    assert payload["tokenSource"] == "unavailable"
    assert payload["error"]


def test_no_bucket_is_ever_negative(tmp_path: Path, session: Path) -> None:
    """An invalid-only usage object is unavailable, never an all-null bucket."""
    start = snapshot(session, tmp_path)
    session.write_text(
        turn(
            request_id="a",
            output=-1,
            input_tokens=-1,
            cache_read=-1,
            cache_write=-1,
            thinking=-1,
        )
    )

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["tokensFile"] is None
