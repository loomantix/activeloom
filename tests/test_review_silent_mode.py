"""The run controller hands a silent PR's GitHub traffic to the ledger helper."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    path = ROOT / ".claude/skills/critique/scripts/local-review-handoff.py"
    spec = importlib.util.spec_from_file_location("silent_controller", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("ACTIVELOOM_SILENT_ROOT", str(tmp_path / "review"))
    monkeypatch.setenv("ACTIVELOOM_REVIEW_PROFILE", str(tmp_path / "profile.json"))
    monkeypatch.delenv("ACTIVELOOM_REVIEW_SILENT", raising=False)
    return module


def scope(repo: str = "client/app", pr: int = 7, silent: bool = False) -> argparse.Namespace:
    return argparse.Namespace(repo=repo, pr=pr, silent=silent)


def record_runs(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, "input": kwargs.get("input")})
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return calls


def test_loud_pr_calls_gh_directly(controller: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    controller._enter_silent_scope(scope())
    calls = record_runs(monkeypatch, controller)
    controller._run_gh(["api", "user"])
    assert calls[0]["command"] == ["gh", "api", "user"]


def test_profile_silent_pr_routes_through_the_helper(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "profile.json").write_text(json.dumps({"silent": {"repos": ["client/*"]}}))
    controller._enter_silent_scope(scope())
    calls = record_runs(monkeypatch, controller)
    controller._run_gh(["api", "-X", "POST", "repos/client/app/issues/7/comments"], {"body": "x"})
    command = calls[0]["command"]
    assert command[0] == "node" and command[1].endswith("review-ledger.js")
    assert command[2:8] == ["local-gh", "--repo", "client/app", "--pr", "7", "--"]
    assert command[8:] == ["api", "-X", "POST", "repos/client/app/issues/7/comments", "--input", "-"]
    assert json.loads(calls[0]["input"]) == {"body": "x"}


def test_a_pinned_pr_stays_silent_without_the_profile(
    controller: ModuleType, tmp_path: Path
) -> None:
    store = tmp_path / "review" / "client-app-7" / "silent"
    store.mkdir(parents=True)
    (store / "mode.json").write_text("{}")
    controller._enter_silent_scope(scope())
    assert controller.SILENT_SCOPE == ("client/app", 7)


def test_start_run_silent_pins_through_the_helper(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_runs(monkeypatch, controller)
    controller._enter_silent_scope(scope(silent=True))
    assert calls[0]["command"][2:] == ["silent-pin", "--repo", "client/app", "--pr", "7"]
    assert controller.SILENT_SCOPE == ("client/app", 7)


def test_unreadable_profile_fails_closed(controller: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "profile.json").write_text("{not json")
    with pytest.raises(controller.HandoffError, match="cannot decide silent mode"):
        controller._enter_silent_scope(scope())


def test_matcher_agrees_with_the_ledger_helper(controller: ModuleType) -> None:
    vectors = json.loads((ROOT / "tests/fixtures/silent-globs.json").read_text())
    for vector in vectors:
        assert controller._silent_pattern_matches(vector["pattern"], vector["repo"]) is vector["match"]
