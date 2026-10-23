"""Blocked-round recovery in the Codex-root agent-loop controller, end to end."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_agent_loop import _blocked_slip_hook, consumer as consumer_fixture
from tests.test_agent_loop_prepublication import harness, run_loop
from tests.test_codex_agent_loop import LINUX_ONLY

pytestmark = [
    pytest.mark.regression,
    pytest.mark.parametrize(
        "harness", [pytest.param(".codex", marks=LINUX_ONLY)], indirect=True
    ),
]

consumer = consumer_fixture
__all__ = ["consumer", "harness"]


def test_blocked_slip_after_publication_is_reviewed_next_round(
    consumer: tuple[Path, Path, Path, Path], harness: str, tmp_path: Path
) -> None:
    result = run_loop(
        consumer,
        harness,
        tmp_path,
        ["--issues", "71"],
        codex_review_hook=str(_blocked_slip_hook(tmp_path)),
        review_max_rounds="2",
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "recovered: blocked-round (Codex, round 1)" in result.stdout
    assert "slip: cleanup occurrence 1 was posted at its fix head" in result.stdout
    assert "Local review convergence round 2/2" in result.stdout
    assert (consumer[3] / "pr-ready").exists()


def test_blocked_slip_with_an_incomplete_ledger_still_stops(
    consumer: tuple[Path, Path, Path, Path], harness: str, tmp_path: Path
) -> None:
    result = run_loop(
        consumer,
        harness,
        tmp_path,
        ["--issues", "71"],
        codex_review_hook=str(_blocked_slip_hook(tmp_path, resolved=False)),
        review_max_rounds="2",
    )
    assert result.returncode != 0
    assert "Codex review blocked in round 1" in result.stderr
    assert "recovered: blocked-round" not in result.stdout
    assert not (consumer[3] / "pr-ready").exists()


def test_blocked_slip_does_not_hide_an_unrelated_blocker(
    consumer: tuple[Path, Path, Path, Path], harness: str, tmp_path: Path
) -> None:
    result = run_loop(
        consumer,
        harness,
        tmp_path,
        ["--issues", "71"],
        codex_review_hook=str(
            _blocked_slip_hook(tmp_path, blocker="review lane did not finish")
        ),
        review_max_rounds="2",
    )
    assert result.returncode != 0
    assert "Codex review blocked in round 1: review lane did not finish" in result.stderr
    assert "recovered: blocked-round" not in result.stdout
    assert not (consumer[3] / "pr-ready").exists()
