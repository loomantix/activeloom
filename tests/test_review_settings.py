"""Contract for the shared resolve-and-pin helper for reviewer and worker settings."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "prompts/skills/review-setup/scripts"
ENV_KEYS = {
    f"AGENT_LOOP_{engine}{infix}_{field}"
    for engine in ("CLAUDE", "CODEX", "GEMINI")
    for infix in ("", "_WORKER")
    for field in ("MODEL", "EFFORT", "SOURCE")
}


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "review_settings", SOURCE / "review-settings.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profile_document(**changes: Any) -> dict[str, Any]:
    defaults = json.loads((SOURCE / "review-profile.defaults.json").read_text())
    engines = defaults["engines"]
    engines["codex"].update(
        model="gpt-6-astra",
        effort="max",
        fallback={"model": "gpt-5.6-sol", "effort": "medium"},
    )
    engines["codex"]["worker"] = {
        "model": "gpt-6-astra",
        "effort": "high",
        "fallback": {"model": "gpt-5.6-sol", "effort": "medium"},
    }
    engines["claude"]["model"] = "opus[1m]"
    document = {
        "schema_version": 2,
        "defaults_version": defaults["defaults_version"],
        "confirmed_at": "2026-01-01T00:00:00Z",
        "engines": engines,
        "order": defaults["order"],
    }
    document.update(changes)
    return document


@pytest.fixture
def scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the helper beside the profile helper, as every root ships it."""
    directory = tmp_path / "scripts"
    directory.mkdir()
    for name in ("review-settings.py", "review-profile.py", "review-profile.defaults.json"):
        shutil.copyfile(SOURCE / name, directory / name)
    profile = tmp_path / "review-profile.json"
    profile.write_text(json.dumps(profile_document()))
    monkeypatch.setenv("ACTIVELOOM_REVIEW_PROFILE", str(profile))
    # An inherited run pin must not leak into a fresh resolution.
    monkeypatch.setenv("ACTIVELOOM_REVIEW_MODEL", "stale-model")
    monkeypatch.setenv("ACTIVELOOM_REVIEW_EFFORT", "low")
    return directory


def run(scripts: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(scripts / "review-settings.py"), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ),
    )


def evaluated(output: str) -> dict[str, str]:
    """Evaluate the env output in bash exactly as a wrapper would."""
    script = 'eval "$1"; for k in "${@:2}"; do printf "%s=%s\\0" "$k" "${!k-}"; done'
    result = subprocess.run(
        ["bash", "-c", script, "bash", output, *sorted(ENV_KEYS)],
        capture_output=True,
        text=True,
        check=True,
        cwd="/",
    )
    pairs = [item.split("=", 1) for item in result.stdout.split("\0") if item]
    return {key: value for key, value in pairs if value}


def test_pin_once_keeps_settings_when_the_profile_changes(
    scripts: Path, tmp_path: Path
) -> None:
    pin_file = tmp_path / "run" / "settings.json"
    pin_file.parent.mkdir()
    args = ("pin", "--pin-file", str(pin_file), "--repo", "example/repo")
    first = run(scripts, *args, "--reviewer", "claude", "codex", "--worker", "codex")
    assert first.returncode == 0, first.stderr
    assert evaluated(first.stdout) == {
        "AGENT_LOOP_CLAUDE_MODEL": "opus[1m]",
        "AGENT_LOOP_CLAUDE_EFFORT": "medium",
        "AGENT_LOOP_CLAUDE_SOURCE": "user profile",
        "AGENT_LOOP_CODEX_MODEL": "gpt-6-astra",
        "AGENT_LOOP_CODEX_EFFORT": "max",
        "AGENT_LOOP_CODEX_SOURCE": "user profile",
        "AGENT_LOOP_CODEX_WORKER_MODEL": "gpt-6-astra",
        "AGENT_LOOP_CODEX_WORKER_EFFORT": "high",
        "AGENT_LOOP_CODEX_WORKER_SOURCE": "user profile",
    }
    assert first.stderr.count("Pinned ") == 3
    assert oct(pin_file.stat().st_mode & 0o777) == "0o600"
    stored = pin_file.read_bytes()

    changed = profile_document()
    changed["engines"]["claude"]["effort"] = "max"
    changed["engines"]["codex"]["worker"]["effort"] = "low"
    Path(os.environ["ACTIVELOOM_REVIEW_PROFILE"]).write_text(json.dumps(changed))
    resumed = run(scripts, *args, "--reviewer", "claude", "--worker", "codex")
    assert resumed.returncode == 0, resumed.stderr
    assert resumed.stdout == first.stdout
    assert "Pinned" not in resumed.stderr
    assert pin_file.read_bytes() == stored

    # Resume needs no profile at all once everything is pinned.
    Path(os.environ["ACTIVELOOM_REVIEW_PROFILE"]).unlink()
    offline = run(scripts, *args, "--reviewer", "codex", "--format", "json")
    assert offline.returncode == 0, offline.stderr
    report = json.loads(offline.stdout)
    assert report["reviewer"]["claude"]["effort"] == "medium"
    assert report["worker"]["codex"] == {
        "engine": "codex",
        "role": "worker",
        "model": "gpt-6-astra",
        "effort": "high",
        "fallback": {"model": "gpt-5.6-sol", "effort": "medium"},
        "source": "user profile",
    }


def test_a_later_pair_is_pinned_without_changing_earlier_ones(
    scripts: Path, tmp_path: Path
) -> None:
    pin_file = tmp_path / "settings.json"
    base = ("pin", "--pin-file", str(pin_file), "--repo", "example/repo")
    assert run(scripts, *base, "--reviewer", "claude").returncode == 0
    changed = profile_document()
    changed["engines"]["claude"]["effort"] = "max"
    Path(os.environ["ACTIVELOOM_REVIEW_PROFILE"]).write_text(json.dumps(changed))
    later = run(scripts, *base, "--worker", "gemini")
    assert later.returncode == 0, later.stderr
    values = evaluated(later.stdout)
    assert values["AGENT_LOOP_CLAUDE_EFFORT"] == "medium"
    assert values["AGENT_LOOP_GEMINI_WORKER_MODEL"] == "gemini-3.7-flash-high"
    assert "Pinned gemini worker settings" in later.stderr


def test_fallback_switches_once_and_reports_the_actual_settings(
    scripts: Path, tmp_path: Path
) -> None:
    pin_file = tmp_path / "settings.json"
    base = ("--pin-file", str(pin_file))
    pinned = run(scripts, "pin", *base, "--reviewer", "codex", "--worker", "codex")
    assert pinned.returncode == 0, pinned.stderr

    switched = run(scripts, "fallback", *base, "--engine", "codex", "--role", "worker")
    assert switched.returncode == 0, switched.stderr
    values = evaluated(switched.stdout)
    assert values["AGENT_LOOP_CODEX_WORKER_MODEL"] == "gpt-5.6-sol"
    assert values["AGENT_LOOP_CODEX_WORKER_EFFORT"] == "medium"
    assert values["AGENT_LOOP_CODEX_WORKER_SOURCE"] == "capacity fallback"
    # The reviewer pair switches independently.
    assert values["AGENT_LOOP_CODEX_MODEL"] == "gpt-6-astra"
    assert "Capacity fallback: codex worker model gpt-5.6-sol" in switched.stderr

    # Resume keeps the fallback in use.
    resumed = run(scripts, "pin", *base, "--worker", "codex")
    assert resumed.stdout == switched.stdout

    again = run(scripts, "fallback", *base, "--engine", "codex", "--role", "worker")
    assert again.returncode == 1
    assert again.stdout == ""
    assert "also at capacity" in again.stderr
    assert run(scripts, "pin", *base).stdout == switched.stdout


def test_fallback_without_a_pinned_pair_is_refused(
    scripts: Path, tmp_path: Path
) -> None:
    pin_file = tmp_path / "settings.json"
    base = ("--pin-file", str(pin_file))
    assert run(scripts, "fallback", *base, "--engine", "claude").returncode == 2
    assert not pin_file.exists()
    assert run(scripts, "pin", *base, "--reviewer", "claude").returncode == 0
    stored = pin_file.read_bytes()
    refused = run(scripts, "fallback", *base, "--engine", "claude")
    assert refused.returncode == 1
    assert "no Claude fallback was pinned" in refused.stderr
    assert pin_file.read_bytes() == stored


def test_missing_profile_fails_closed_with_the_missing_keys(
    scripts: Path, tmp_path: Path
) -> None:
    Path(os.environ["ACTIVELOOM_REVIEW_PROFILE"]).unlink()
    pin_file = tmp_path / "settings.json"
    result = run(
        scripts, "pin", "--pin-file", str(pin_file), "--reviewer", "codex",
        "--worker", "claude",
    )
    assert result.returncode == 3
    assert json.loads(result.stdout) == {
        "missing": ["codex.model", "codex.effort", "claude.worker.model", "claude.worker.effort"],
        "path": os.environ["ACTIVELOOM_REVIEW_PROFILE"],
    }
    assert len(result.stderr.strip().splitlines()) == 1
    assert "review-setup" in result.stderr
    assert not pin_file.exists()


def test_missing_worker_keys_fail_closed_before_any_pin(
    scripts: Path, tmp_path: Path
) -> None:
    document = profile_document(schema_version=1)
    for settings in document["engines"].values():
        settings.pop("worker")
    Path(os.environ["ACTIVELOOM_REVIEW_PROFILE"]).write_text(json.dumps(document))
    pin_file = tmp_path / "settings.json"
    result = run(
        scripts, "pin", "--pin-file", str(pin_file), "--reviewer", "claude",
        "--worker", "claude", "gemini",
    )
    assert result.returncode == 3
    assert json.loads(result.stdout)["missing"] == [
        "claude.worker.model",
        "claude.worker.effort",
        "gemini.worker.model",
        "gemini.worker.effort",
    ]
    assert not pin_file.exists()


def test_unavailable_engine_is_refused_before_any_pin(
    scripts: Path, tmp_path: Path
) -> None:
    document = profile_document()
    document["engines"]["gemini"]["availability"] = "unavailable"
    Path(os.environ["ACTIVELOOM_REVIEW_PROFILE"]).write_text(json.dumps(document))
    pin_file = tmp_path / "settings.json"
    result = run(
        scripts, "pin", "--pin-file", str(pin_file), "--reviewer", "claude", "gemini"
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "unavailable" in result.stderr
    assert not pin_file.exists()


@pytest.mark.parametrize("damage", ["symlink", "json", "version", "key", "repo", "switch"])
def test_a_damaged_or_foreign_pin_file_is_refused(
    scripts: Path, tmp_path: Path, damage: str
) -> None:
    pin_file = tmp_path / "settings.json"
    base = ("pin", "--pin-file", str(pin_file), "--repo", "example/repo")
    assert run(scripts, *base, "--reviewer", "claude").returncode == 0
    document = json.loads(pin_file.read_text())
    if damage == "symlink":
        target = tmp_path / "elsewhere.json"
        pin_file.rename(target)
        pin_file.symlink_to(target)
    elif damage == "json":
        pin_file.write_text("{")
    elif damage == "version":
        pin_file.write_text(json.dumps({**document, "version": 2}))
    elif damage == "key":
        pin_file.write_text(json.dumps({**document, "extra": {}}))
    elif damage == "switch":
        pin_file.write_text(json.dumps({**document, "fallback_engines": ["claude"]}))
    result = run(
        scripts,
        *(base if damage != "repo" else (*base[:3], "--repo", "other/repo")),
        "--reviewer",
        "claude",
    )
    assert result.returncode == 2
    assert result.stdout == ""


def test_env_output_is_quoted_and_limited_to_the_allowlist(
    scripts: Path, tmp_path: Path
) -> None:
    hostile = "x'; touch " + str(tmp_path / "pwned") + "; echo '$(id)`id`*"
    pin_file = tmp_path / "settings.json"
    pin_file.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": None,
                "review_settings": {
                    "codex": {"model": hostile, "effort": "max", "source": hostile}
                },
                "worker_settings": {
                    "claude": {"model": "opus", "effort": "high", "source": hostile}
                },
            }
        )
    )
    result = run(scripts, "pin", "--pin-file", str(pin_file))
    assert result.returncode == 0, result.stderr
    assert {line.split("=", 1)[0] for line in result.stdout.splitlines()} <= ENV_KEYS
    values = evaluated(result.stdout)
    assert values["AGENT_LOOP_CODEX_MODEL"] == hostile
    assert values["AGENT_LOOP_CLAUDE_WORKER_SOURCE"] == hostile
    assert not (tmp_path / "pwned").exists()


def test_unexpected_failures_are_invalid_not_refused(
    scripts: Path, tmp_path: Path
) -> None:
    unsourced = tmp_path / "unsourced.json"
    unsourced.write_text(
        json.dumps(
            {"version": 1, "repo": None, "review_settings": {"codex": {"model": "m", "effort": "e"}}}
        )
    )
    result = run(scripts, "pin", "--pin-file", str(unsourced))
    assert (result.returncode, result.stdout) == (2, "")

    locked = tmp_path / "locked"
    locked.mkdir()
    pin_file = locked / "settings.json"
    assert run(scripts, "pin", "--pin-file", str(pin_file), "--reviewer", "codex").returncode == 0
    locked.chmod(0o500)
    try:
        result = run(scripts, "fallback", "--pin-file", str(pin_file), "--engine", "codex")
    finally:
        locked.chmod(0o700)
    assert (result.returncode, result.stdout) == (2, "")
    assert "Traceback" not in result.stderr

    crashing = scripts / "review-profile.py"
    crashing.write_text("raise SystemExit('boom')\n")
    result = run(scripts, "pin", "--pin-file", str(tmp_path / "new.json"), "--reviewer", "codex")
    assert (result.returncode, result.stdout) == (2, "")


def test_pin_resolves_only_on_first_use() -> None:
    module = load()
    state: dict[str, Any] = {}
    calls: list[str] = []

    def resolver() -> dict[str, Any]:
        calls.append("resolve")
        return {"engine": "codex", "model": "m", "effort": "high", "source": "user profile"}

    first, created = module.pin(state, "codex", "worker", resolver)
    again, created_again = module.pin(state, "codex", "worker", resolver)
    assert (created, created_again) == (True, False)
    assert first == again
    assert calls == ["resolve"]
    assert state == {"worker_settings": {"codex": first}}
    assert module.pinned(state, "codex", "reviewer") is None


def test_fallback_switch_is_recorded_once_per_role() -> None:
    module = load()
    fallback = {"model": "small", "effort": "medium"}
    state: dict[str, Any] = {
        "review_settings": {
            "codex": {"engine": "codex", "model": "big", "effort": "max",
                      "source": "user profile", "fallback": fallback}
        },
        "worker_settings": {
            "codex": {"engine": "codex", "role": "worker", "model": "big",
                      "effort": "high", "source": "user profile", "fallback": fallback}
        },
    }
    assert module.check_fallback(state, "codex", "reviewer") == fallback
    selected = module.switch_to_fallback(state, "codex", "reviewer")
    assert selected == {**fallback, "engine": "codex", "source": "capacity fallback"}
    assert state["fallback_engines"] == ["codex"]
    assert module.selected(state, "codex", "worker")["model"] == "big"
    with pytest.raises(module.SettingsError, match="also at capacity"):
        module.switch_to_fallback(state, "codex", "reviewer")
    # A switch the caller recorded as started resumes instead of refusing.
    assert module.check_fallback(state, "codex", "reviewer", in_progress=True) == fallback
    assert state["fallback_engines"] == ["codex"]
    assert module.describe(selected) == "model small, effort medium (capacity fallback)"

    state["review_settings"]["codex"].pop("fallback")
    with pytest.raises(module.SettingsError, match="pinned fallback settings are missing"):
        module.selected(state, "codex", "reviewer")


def load_runner() -> ModuleType:
    path = ROOT / ".codex/skills/critique/scripts/review-chain-runner.py"
    spec = importlib.util.spec_from_file_location("review_chain_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tampered", [False, True], ids=["pinned", "tampered"])
def test_runner_executes_only_the_pinned_snapshot_of_the_helper(
    tmp_path: Path, tampered: bool
) -> None:
    runner_module = load_runner()
    assert "review-settings.py" in runner_module.CONTROL_FILES
    source = ROOT / ".codex/skills/critique/scripts/review-settings.py"
    control = tmp_path / "checkpoint" / "control"
    control.mkdir(parents=True)
    copy = control / "review-settings.py"
    shutil.copyfile(source, copy)
    if tampered:
        copy.write_text(source.read_text() + "\n# changed after the run started\n")
    runner = runner_module.Runner(
        SimpleNamespace(repo="example/repo"), tmp_path / "checkpoint"
    )
    runner.state = {
        "control_hashes": {"review-settings.py": runner_module.digest(source)},
        "review_settings": {
            "codex": {"engine": "codex", "model": "big", "effort": "max",
                      "source": "user profile",
                      "fallback": {"model": "small", "effort": "medium"}}
        },
        "fallback_engines": ["codex"],
    }
    if tampered:
        with pytest.raises(runner_module.Blocked, match="pinned controller"):
            runner.selected_settings("codex")
        return
    assert runner.settings_helper().__file__ == str(copy)
    assert runner.selected_settings("codex") == {
        "engine": "codex", "model": "small", "effort": "medium",
        "source": "capacity fallback",
    }
    assert runner.settings_line("codex") == (
        "Reviewer settings: model small, effort medium (capacity fallback).\n"
    )
