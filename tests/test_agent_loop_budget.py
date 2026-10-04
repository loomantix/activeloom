"""Active execution accounting and explicit recovery in every shipped engine."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["claude", "codex", "agents"])
def helper(request: pytest.FixtureRequest) -> Path:
    return ROOT / f".{request.param}/skills/agent-loop/scripts/agent-loop-state.py"


def run(helper: Path, *args: str, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["python3", "-I", str(helper), *args], capture_output=True,
                          text=True, pass_fds=pass_fds, check=False)


def state(tmp_path: Path, helper: Path, *, legacy: bool = False, deadline: int | None = None) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir(mode=0o700)
    path = directory / "run-state.json"
    value: dict[str, object] = {
        "version": (3 if ".agents" in str(helper) else 2) if legacy else 4, "runId": "original-run", "repo": "example/project",
        "issue": 7, "prNumber": 9, "prUrl": "https://example.invalid/pull/9",
        "issueTitleSha256": "a" * 64, "issueBodySha256": "b" * 64,
        "baseBranch": "main", "branch": "agent-loop/issue-7-original",
        "worktree": str(tmp_path / "worktree"), "logDir": str(directory),
        "baseSha": "c" * 40, "headSha": "d" * 40, "phase": "reviewing",
        "round": 3, "reviewEngine": "claude", "claudeResultSha256": None,
        "geminiResultSha256" if ".agents" in str(helper) else "codexResultSha256": None,
    }
    if not legacy:
        value.update(reviewMaxRounds=4, reviewBudget={"limit": 7200, "remaining": 7200,
                                                    "attempts": [], "migration": None})
    elif deadline is not None:
        value.update(reviewMaxRounds=4, reviewDeadlineEpoch=deadline)
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def load_module(helper: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("budget_state_test", helper)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_outage_and_repeated_resumes_do_not_spend_or_replenish(helper: Path, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    path = state(tmp_path, helper)
    module = load_module(helper)
    parser = module._parser()
    now = 1000000000000
    monkeypatch.setattr(time, "monotonic_ns", lambda: now)
    before = json.loads(path.read_text())
    for attempt in range(1, 4):
        args = parser.parse_args(["budget-begin", "--file", str(path), "--seconds", "1000",
                                  "--owner", str(os.getpid())])
        module._budget_command(args)
        now += 1500000000
        args = parser.parse_args(["budget-finish", "--file", str(path), "--attempt", str(attempt),
                                  "--owner", str(os.getpid())])
        module._budget_command(args)
        # A month stopped, and a wall-clock rollback, cannot affect saved time.
        now += 30 * 86400 * 1000000000
        monkeypatch.setattr(time, "time", lambda: -1000)
        for _ in range(2):
            shown = run(helper, "budget-show", "--file", str(path))
            assert shown.returncode == 0, shown.stderr
            assert int(shown.stdout) == 7200 - attempt * 2
    after = json.loads(path.read_text())
    assert {k: v for k, v in before.items() if k != "reviewBudget"} == {
        k: v for k, v in after.items() if k != "reviewBudget"}
    assert len(after["reviewBudget"]["attempts"]) == 3


@pytest.mark.parametrize("deadline", [None, 1, 4102444800])
def test_legacy_requires_explicit_bounded_once_only_migration(helper: Path, tmp_path: Path,
                                                            deadline: int | None) -> None:
    path = state(tmp_path, helper, legacy=True, deadline=deadline)
    original = path.read_bytes()
    denied = run(helper, "budget-show", "--file", str(path))
    assert denied.returncode != 0 and "budget-migrate" in denied.stderr
    assert path.read_bytes() == original
    digest = hashlib.sha256(original).hexdigest()
    args = ("budget-migrate", "--file", str(path), "--expected-sha256", digest,
            "--remaining-seconds", "1800", "--limit-seconds", "7200", "--max-rounds", "4",
            "--confirm-stopped", "--reason", "Operator bounded recovery after evidence inspection")
    migrated = run(helper, *args)
    assert migrated.returncode == 0, migrated.stderr
    saved = path.read_bytes()
    assert run(helper, *args).returncode != 0
    assert path.read_bytes() == saved
    value = json.loads(saved)
    for key, prior in json.loads(original).items():
        if key not in {"version", "reviewDeadlineEpoch"}:
            assert value[key] == prior
    assert value["reviewBudget"]["remaining"] == 1800
    assert value["reviewBudget"]["migration"]["deadline"] == deadline
    assert path.with_name(path.name + ".legacy-" + digest + ".json").read_bytes() == original


def test_crashed_execution_is_charged_and_needs_explicit_reconciliation(helper: Path, tmp_path: Path) -> None:
    path = state(tmp_path, helper)
    begin = run(helper, "budget-begin", "--file", str(path), "--seconds", "7200", "--owner", "2147483647")
    assert begin.returncode == 0, begin.stderr
    saved = path.read_bytes()
    assert json.loads(saved)["reviewBudget"]["remaining"] == 0
    for _ in range(2):
        blocked = run(helper, "budget-show", "--file", str(path))
        assert blocked.returncode != 0 and "budget-reconcile" in blocked.stderr
        assert path.read_bytes() == saved
    recovered = run(helper, "budget-reconcile", "--file", str(path), "--expected-sha256",
                    hashlib.sha256(saved).hexdigest(), "--confirm-stopped", "--reason", "Confirmed no workers remain")
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip() == "0"
    assert run(helper, "budget-begin", "--file", str(path), "--seconds", "1", "--owner", str(os.getpid())).returncode != 0
    assert run(helper, "budget-migrate", "--file", str(path), "--expected-sha256",
               hashlib.sha256(path.read_bytes()).hexdigest(), "--confirm-stopped", "--reason", "no reset",
               "--remaining-seconds", "7200", "--limit-seconds", "7200").returncode != 0


def test_live_controller_and_concurrent_recovery_are_refused(helper: Path, tmp_path: Path) -> None:
    path = state(tmp_path, helper)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(helper, "budget-show", "--file", str(path)).returncode != 0
        begin = run(helper, "budget-begin", "--file", str(path), "--seconds", "100",
                    "--owner", str(os.getpid()), "--lock-fd", str(fd), pass_fds=(fd,))
        assert begin.returncode == 0, begin.stderr
    finally:
        os.close(fd)
    before = path.read_bytes()
    result = run(helper, "budget-reconcile", "--file", str(path), "--expected-sha256",
                 hashlib.sha256(before).hexdigest(), "--confirm-stopped", "--reason", "still alive")
    assert result.returncode != 0 and "controller still exists" in result.stderr
    assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["remaining", "version", "symlink", "permissions"])
def test_invalid_accounting_or_unsafe_state_fails_closed(helper: Path, tmp_path: Path, mutation: str) -> None:
    path = state(tmp_path, helper)
    if mutation == "symlink":
        target = path.with_name("actual.json")
        path.rename(target)
        path.symlink_to(target)
    elif mutation == "permissions":
        path.chmod(0o644)
    else:
        value = json.loads(path.read_text())
        if mutation == "remaining":
            value["reviewBudget"]["remaining"] = 7199
        else:
            value["version"] = True
        path.write_text(json.dumps(value))
    assert run(helper, "budget-show", "--file", str(path)).returncode != 0


def test_monotonic_rollback_or_reboot_never_refunds(helper: Path, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    path = state(tmp_path, helper)
    module = load_module(helper)
    parser = module._parser()
    monkeypatch.setattr(time, "monotonic_ns", lambda: 2000000000)
    module._budget_command(parser.parse_args(["budget-begin", "--file", str(path), "--seconds", "10", "--owner", str(os.getpid())]))
    before = path.read_bytes()
    finish = parser.parse_args(["budget-finish", "--file", str(path), "--attempt", "1", "--owner", str(os.getpid())])
    monkeypatch.setattr(time, "monotonic_ns", lambda: 1000000000)
    with pytest.raises(module.StateError, match="clock or execution owner"):
        module._budget_command(finish)
    monkeypatch.setattr(module, "_budget_boot", lambda: "new-boot")
    with pytest.raises(module.StateError, match="clock or execution owner"):
        module._budget_command(finish)
    assert path.read_bytes() == before


def test_generated_budget_regions_match_the_canonical_sources() -> None:
    for suffix, name in (("py", "agent-loop-state.py"), ("sh", "agent-loop.sh")):
        expected = (ROOT / "scripts" / f"agent-loop-budget.{suffix}.inc").read_text()
        for engine in ("claude", "codex", "agents"):
            text = (ROOT / f".{engine}/skills/agent-loop/scripts" / name).read_text()
            assert text.split("# agent-loop-budget:begin\n")[1].split("# agent-loop-budget:end")[0] == expected


@pytest.mark.parametrize("remaining,limit", [(7201, 7200), (-1, 7200), (1, 86401), (1, 0)])
def test_legacy_migration_rejects_invalid_operator_bounds(helper: Path, tmp_path: Path,
                                                        remaining: int, limit: int) -> None:
    path = state(tmp_path, helper, legacy=True, deadline=1)
    before = path.read_bytes()
    result = run(helper, "budget-migrate", "--file", str(path), "--expected-sha256",
                 hashlib.sha256(before).hexdigest(), "--confirm-stopped", "--reason", "invalid bound",
                 "--remaining-seconds", str(remaining), "--limit-seconds", str(limit))
    assert result.returncode != 0
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.legacy-*"))


def test_fully_spent_observed_execution_cannot_be_replenished(helper: Path, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    path = state(tmp_path, helper)
    module = load_module(helper)
    parser = module._parser()
    monkeypatch.setattr(time, "monotonic_ns", lambda: 1000000000)
    module._budget_command(parser.parse_args(["budget-begin", "--file", str(path), "--seconds", "7200", "--owner", str(os.getpid())]))
    monkeypatch.setattr(time, "monotonic_ns", lambda: 7201000000000)
    module._budget_command(parser.parse_args(["budget-finish", "--file", str(path), "--attempt", "1", "--owner", str(os.getpid())]))
    before = path.read_bytes()
    for _ in range(2):
        assert run(helper, "budget-show", "--file", str(path)).stdout.strip() == "0"
        assert run(helper, "budget-begin", "--file", str(path), "--seconds", "1", "--owner", str(os.getpid())).returncode != 0
        assert path.read_bytes() == before
