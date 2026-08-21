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
import re
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


def test_the_project_slug_substitutes_every_non_alphanumeric(
    tmp_path: Path,
) -> None:
    """A dotted working directory must still resolve its project directory.

    The harness replaces every non-alphanumeric character with a dash, not only
    the separator, so a hostname-style repository name resolves to a directory
    a separator-only substitution never finds.
    """
    cwd = tmp_path / "www.example.com"
    cwd.mkdir()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
    project = tmp_path / "projects" / slug
    project.mkdir(parents=True)
    log = project / "0000-session.jsonl"
    log.write_text(turn(request_id="a", output=11))

    payload = run(
        "snapshot",
        "--out",
        str(tmp_path / "start.json"),
        "--projects-dir",
        str(tmp_path / "projects"),
        "--cwd",
        str(cwd),
    )
    assert payload["sessionLog"] == str(log)


def test_a_snapshot_from_another_session_is_not_scoped(
    tmp_path: Path, session: Path
) -> None:
    """A stale start file must not scope this pass to its predecessor's log.

    The start file sits at a fixed path across an autonomous run, so a pass
    whose snapshot step failed would otherwise measure from the previous pass's
    baseline and still call the result scoped.
    """
    start = snapshot(session, tmp_path)
    other = session.parent / "1111-other.jsonl"
    other.write_text(turn(request_id="other", output=99))

    payload = run(
        "delta",
        "--out-dir",
        str(tmp_path / "out"),
        "--start",
        str(start),
        "--projects-dir",
        str(tmp_path / "projects"),
        "--cwd",
        str(tmp_path),
        CLAUDE_CODE_SESSION_ID="1111-other",
    )
    assert payload["tokenSource"] == "unscoped-session"
    assert payload["reason"] == "snapshot-not-this-session"


def test_a_log_recorded_at_snapshot_and_gone_at_delta_downgrades(
    tmp_path: Path, session: Path
) -> None:
    """A vanished lane log must not leave a record still claiming completeness."""
    subagents = session.parent / session.stem / "subagents"
    subagents.mkdir(parents=True)
    lane = subagents / "agent-1.jsonl"
    lane.write_text(turn(request_id="lane", output=500, sidechain=True, lens="x"))

    start = snapshot(session, tmp_path)
    lane.unlink()
    with session.open("a") as handle:
        handle.write(turn(request_id="after", output=20))

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["reason"] == "log-missing-since-snapshot"


def test_a_replaced_log_is_not_reported_as_a_scoped_delta(
    tmp_path: Path, session: Path
) -> None:
    """A different file at the same path is not the file the offset described."""
    session.write_text(turn(request_id="first", output=5))
    start = snapshot(session, tmp_path)

    replacement = session.parent / "replacement.jsonl"
    replacement.write_text(
        turn(request_id="first", output=5) + turn(request_id="second", output=900)
    )
    session.unlink()
    replacement.rename(session)

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["reason"] == "log-replaced-since-snapshot"


def test_an_ambiguous_session_id_is_not_identity_bound(tmp_path: Path) -> None:
    """Two projects holding the same session id must not resolve to either."""
    projects = tmp_path / "projects"
    for name in ("alpha", "beta"):
        directory = projects / name
        directory.mkdir(parents=True)
        (directory / "shared.jsonl").write_text(turn(request_id=name, output=1))

    payload = run(
        "snapshot",
        "--out",
        str(tmp_path / "start.json"),
        "--projects-dir",
        str(projects),
        "--cwd",
        str(tmp_path / "nowhere"),
        CLAUDE_CODE_SESSION_ID="shared",
    )
    assert payload["sessionLog"] is None
    assert payload["scoped"] is False


def test_a_traversal_shaped_session_id_is_rejected(tmp_path: Path) -> None:
    """The session id is joined into a path, so its shape is a real guard."""
    outside = tmp_path / "outside.jsonl"
    outside.write_text(turn(request_id="outside", output=1))
    projects = tmp_path / "projects"
    (projects / "empty").mkdir(parents=True)

    payload = run(
        "snapshot",
        "--out",
        str(tmp_path / "start.json"),
        "--projects-dir",
        str(projects),
        "--cwd",
        str(tmp_path / "nowhere"),
        CLAUDE_CODE_SESSION_ID="../../outside",
    )
    assert payload["sessionLog"] is None
    assert payload["scoped"] is False


def test_a_malformed_start_file_downgrades_instead_of_scoping(
    tmp_path: Path, session: Path
) -> None:
    """Only a snapshot this helper can validate may scope a measurement."""
    session.write_text(turn(request_id="a", output=12))
    start = tmp_path / "start.json"
    start.write_text("{ not json")

    payload = delta(tmp_path, start, session_log=str(session))
    assert payload["tokenSource"] == "unscoped-session"
    assert payload["reason"] == "no-start-snapshot"


def test_a_snapshot_version_bump_is_not_read_as_current(
    tmp_path: Path, session: Path
) -> None:
    """A start file from a different snapshot format must not scope this pass."""
    session.write_text(turn(request_id="a", output=12))
    start = snapshot(session, tmp_path)
    stored = json.loads(start.read_text())
    stored["version"] = stored["version"] + 1
    start.write_text(json.dumps(stored))

    payload = delta(tmp_path, start, session_log=str(session))
    assert payload["tokenSource"] == "unscoped-session"


def test_an_unrecognised_model_id_downgrades_rather_than_dropping_its_tokens(
    tmp_path: Path, session: Path
) -> None:
    """A rejected model id must not silently leave the per-model total short."""
    start = snapshot(session, tmp_path)
    session.write_text(
        turn(request_id="ok", output=10)
        + turn(request_id="odd", output=4000, model="<synthetic>")
    )

    payload = delta(tmp_path, start)
    assert payload["tokenSource"] == "unavailable"
    assert payload["reason"] == "unrecognized-model-id"


def test_an_unavailable_record_reports_no_measured_fields(
    tmp_path: Path, session: Path
) -> None:
    """A record that declared its inputs unusable must not report values from them."""
    payload = delta(tmp_path)
    assert payload["tokenSource"] == "unavailable"
    assert payload["engineVersion"] is None
    assert payload["durationSeconds"] is None
    # Never zero: a missing count that serialises as zero makes the pass look
    # free, which is the same defect as a zero-filled token bucket.
    assert payload["turns"] is None


def test_written_files_are_owner_only(tmp_path: Path, session: Path) -> None:
    """These files embed absolute session paths, so the mode is part of the contract."""
    start = snapshot(session, tmp_path)
    session.write_text(turn(request_id="a", output=10, sidechain=True, lens="x"))
    payload = delta(tmp_path, start)

    written = [start, Path(payload["tokensFile"]), Path(payload["lanesFile"])]
    for path in written:
        assert path.stat().st_mode & 0o077 == 0, path


def test_an_existing_output_directory_keeps_its_own_mode(
    tmp_path: Path, session: Path
) -> None:
    """The helper hardens what it creates, not a directory the caller named."""
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o755)

    snapshot(session, tmp_path)
    run("delta", "--out-dir", str(out))
    assert out.stat().st_mode & 0o777 == 0o755


def test_only_a_scoped_delta_reports_a_duration(tmp_path: Path, session: Path) -> None:
    """An upper-bound record must not carry a duration from a snapshot that does not bound it."""
    session.write_text(turn(request_id="a", output=12))

    payload = delta(tmp_path, session_log=str(session))
    assert payload["tokenSource"] == "unscoped-session"
    assert payload["durationSeconds"] is None


def test_a_lane_spanning_models_reports_no_single_model(
    tmp_path: Path, session: Path
) -> None:
    """Naming one of several models would be a guess presented as a measurement."""
    start = snapshot(session, tmp_path)
    session.write_text(
        turn(request_id="a", output=1, model="claude-opus-5", sidechain=True, lens="w")
        + turn(
            request_id="b", output=1, model="claude-sonnet-5", sidechain=True, lens="w"
        )
        + turn(request_id="c", output=1, model="claude-opus-5", sidechain=True, lens="s")
    )

    payload = delta(tmp_path, start)
    lanes = {
        lane["lens"]: lane for lane in json.loads(Path(payload["lanesFile"]).read_text())
    }
    assert lanes["w"]["model"] is None
    assert lanes["s"]["model"] == "claude-opus-5"
