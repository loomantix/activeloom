"""Contract for the per-user review profile helper that launchers resolve through."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "prompts/skills/review-setup/scripts"
COPIES = [
    ROOT / ".claude/skills/review-setup/scripts",
    ROOT / ".codex/skills/review-setup/scripts",
    ROOT / ".agents/skills/review-setup/scripts",
    ROOT / ".claude/skills/critique/scripts",
    ROOT / ".codex/skills/critique/scripts",
]
FILES = ("review-profile.py", "review-profile.defaults.json")


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "review_profile", ROOT / ".claude/skills/review-setup/scripts/review-profile.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config" / "review-profile.json"
    monkeypatch.setenv("ACTIVELOOM_REVIEW_PROFILE", str(path))
    monkeypatch.delenv("ACTIVELOOM_REVIEW_MODEL", raising=False)
    monkeypatch.delenv("ACTIVELOOM_REVIEW_EFFORT", raising=False)
    return path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = load().main(list(argv))
    captured = capsys.readouterr()
    return status, captured.out, captured.err


@pytest.mark.parametrize("copy", COPIES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_shipped_copy_matches_the_single_source(copy: Path) -> None:
    for name in FILES:
        assert (copy / name).read_bytes() == (SOURCE / name).read_bytes()


def test_recommended_defaults_are_a_valid_profile(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, out, _ = run(capsys, "defaults")
    assert status == 0
    defaults = json.loads(out)
    assert set(defaults["engines"]) == {"claude", "codex", "gemini"}
    assert set(defaults["order"]) == {"lean", "deep"}


def test_no_profile_blocks_resolution_and_points_at_setup(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in (
        ["resolve", "--engine", "claude"],
        ["launch-args", "--engine", "gemini"],
        ["order", "--tier", "deep"],
        ["set", "claude.effort=high"],
    ):
        status, out, err = run(capsys, *command)
        assert status == 3, command
        assert out == ""
        assert "review-setup" in err
    assert not profile.exists()


def test_show_reports_an_unconfigured_profile_without_failing(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "show")
    report = json.loads(out)
    assert status == 0
    assert report["configured"] is False
    assert report["path"] == str(profile)


def test_init_writes_confirmed_defaults_privately(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, _, _ = run(capsys, "init", "--accept-defaults", "claude.effort=high")
    assert status == 0
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    stored = json.loads(profile.read_text())
    assert stored["engines"]["claude"] == {"model": "opus", "effort": "high"}
    assert stored["confirmed_at"]
    status, out, _ = run(capsys, "resolve", "--engine", "claude")
    assert json.loads(out) == {
        "engine": "claude",
        "model": "opus",
        "effort": "high",
        "source": "user profile",
    }


def test_init_refuses_to_overwrite_without_replace(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    before = profile.read_bytes()
    assert run(capsys, "init", "--accept-defaults", "codex.effort=max")[0] == 1
    assert profile.read_bytes() == before
    assert (
        run(capsys, "init", "--accept-defaults", "--replace", "codex.effort=max")[0]
        == 0
    )
    assert json.loads(profile.read_text())["engines"]["codex"]["effort"] == "max"


def test_capacity_fallback_is_optional_atomic_and_overridable(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    assert "fallback" not in json.loads(run(capsys, "resolve", "--engine", "codex")[1])
    assert run(capsys, "set", "codex.fallback.model=gpt-5.6-sol")[0] == 2
    assert "fallback" not in json.loads(profile.read_text())["engines"]["codex"]
    assert (
        run(
            capsys,
            "set",
            "codex.fallback.model=gpt-5.6-sol",
            "codex.fallback.effort=medium",
        )[0]
        == 0
    )
    settings = json.loads(run(capsys, "resolve", "--engine", "codex")[1])
    assert settings["fallback"] == {"model": "gpt-5.6-sol", "effort": "medium"}
    assert (
        run(capsys, "set", "--repo", "example/project", "codex.fallback=none")[0] == 0
    )
    assert (
        json.loads(
            run(capsys, "resolve", "--engine", "codex", "--repo", "example/project")[1]
        )["fallback"]
        is None
    )
    assert run(capsys, "unset", "--repo", "example/project", "codex.fallback")[0] == 0
    assert (
        json.loads(
            run(capsys, "resolve", "--engine", "codex", "--repo", "example/project")[1]
        )["fallback"]
        == settings["fallback"]
    )
    assert run(capsys, "set", "codex.fallback=none")[0] == 0
    assert (
        json.loads(run(capsys, "resolve", "--engine", "codex")[1])["fallback"] is None
    )


@pytest.mark.parametrize(
    "assignments",
    [
        ["codex.fallback.model=inherit", "codex.fallback.effort=medium"],
        ["codex.fallback.model=--unsafe", "codex.fallback.effort=medium"],
        ["codex.fallback.model=gpt-5.6-sol", "codex.fallback.effort=extreme"],
        ["claude.fallback.model=sonnet", "claude.fallback.effort=medium"],
    ],
)
def test_invalid_fallback_does_not_change_profile(
    profile: Path, capsys: pytest.CaptureFixture[str], assignments: list[str]
) -> None:
    run(capsys, "init", "--accept-defaults")
    before = profile.read_bytes()
    assert run(capsys, "set", *assignments)[0] == 2
    assert profile.read_bytes() == before


def test_init_from_file_validates_the_whole_document(
    tmp_path: Path, profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "chosen.json"
    source.write_text(
        json.dumps(
            {
                "engines": {
                    "claude": {"model": "sonnet", "effort": "low"},
                    "codex": {"model": "inherit", "effort": "medium"},
                    "gemini": {"model": "gemini-3.7-flash-high", "effort": "high"},
                },
                "order": {"lean": ["codex"], "deep": ["codex", "claude"]},
            }
        )
    )
    assert run(capsys, "init", "--from-file", str(source))[0] == 0
    assert run(capsys, "order", "--tier", "deep")[1].strip() == "codex,claude"


@pytest.mark.parametrize(
    "assignment",
    [
        "claude.effort=extreme",
        "gemini.effort=xhigh",
        "gemini.model=inherit",
        "claude.model=--dangerously-skip-permissions",
        "claude.model=has space",
        "order.deep=claude,claude",
        "order.deep=claude,copilot",
        "order.nightly=claude",
        "claude.temperature=1",
        "claude.effort",
    ],
)
def test_invalid_settings_are_rejected_without_writing(
    profile: Path, capsys: pytest.CaptureFixture[str], assignment: str
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    before = profile.read_bytes()
    status, _, err = run(capsys, "set", assignment)
    assert status == 2
    assert err.startswith("review profile:")
    assert profile.read_bytes() == before


def test_repository_overrides_layer_over_user_settings(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "init", "--accept-defaults")
    assert (
        run(
            capsys,
            "set",
            "--repo",
            "example/project",
            "codex.model=example-model",
            "order.lean=codex,gemini",
        )[0]
        == 0
    )
    other = json.loads(run(capsys, "resolve", "--engine", "codex")[1])
    scoped = json.loads(
        run(capsys, "resolve", "--engine", "codex", "--repo", "example/project")[1]
    )
    assert other["model"] == "inherit"
    assert scoped == {
        "engine": "codex",
        "model": "example-model",
        "effort": "high",
        "source": "repository override",
    }
    assert run(capsys, "order", "--tier", "lean", "--repo", "example/project")[
        1
    ].strip() == ("codex,gemini")

    assert run(capsys, "unset", "--repo", "example/project", "codex.model")[0] == 0
    assert "repos" in json.loads(profile.read_text())
    assert run(capsys, "unset", "--repo", "example/project", "order.lean")[0] == 0
    assert "repos" not in json.loads(profile.read_text())
    assert run(capsys, "unset", "--repo", "example/project")[0] == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_repository_override_identity_is_case_insensitive(
    profile: Path, capsys: pytest.CaptureFixture[str], legacy: bool
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    assert run(
        capsys, "set", "--repo", "Example/Project", "codex.model=example-model",
        "order.lean=codex,gemini",
    )[0] == 0
    document = json.loads(profile.read_text())
    assert list(document["repos"]) == ["example/project"]
    if legacy:
        document["repos"]["Example/Project"] = document["repos"].pop("example/project")
        profile.write_text(json.dumps(document))
    resolved = json.loads(
        run(capsys, "resolve", "--engine", "codex", "--repo", "EXAMPLE/PROJECT")[1]
    )
    assert resolved["model"] == "example-model"
    assert resolved["source"] == "repository override"
    assert run(capsys, "order", "--tier", "lean", "--repo", "example/PROJECT")[
        1
    ].strip() == "codex,gemini"
    assert run(capsys, "set", "--repo", "EXAMPLE/project", "codex.effort=max")[0] == 0
    assert list(json.loads(profile.read_text())["repos"]) == ["example/project"]
    assert run(capsys, "unset", "--repo", "EXAMPLE/project", "codex.model")[0] == 0
    assert run(capsys, "unset", "--repo", "example/PROJECT")[0] == 0
    assert "repos" not in json.loads(profile.read_text())


def test_import_rejects_duplicate_repository_case_variants(
    tmp_path: Path, profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    before = profile.read_bytes()
    document = json.loads(before)
    document["repos"] = {
        "Example/Project": {"engines": {"codex": {"effort": "low"}}},
        "example/project": {"engines": {"codex": {"effort": "max"}}},
    }
    source = tmp_path / "duplicate-repos.json"
    source.write_text(json.dumps(document))
    status, _, error = run(capsys, "init", "--replace", "--from-file", str(source))
    assert status == 2
    assert "duplicate repository" in error
    assert profile.read_bytes() == before


def test_run_pinned_settings_take_precedence_and_are_validated(
    profile: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACTIVELOOM_REVIEW_MODEL", "opus")
    monkeypatch.setenv("ACTIVELOOM_REVIEW_EFFORT", "max")
    status, out, _ = run(capsys, "launch-args", "--engine", "claude")
    assert status == 0
    assert out == "opus\nmax\n"
    assert not profile.exists()

    monkeypatch.setenv("ACTIVELOOM_REVIEW_EFFORT", "max")
    assert run(capsys, "launch-args", "--engine", "gemini")[0] == 2

    monkeypatch.delenv("ACTIVELOOM_REVIEW_EFFORT")
    assert run(capsys, "resolve", "--engine", "claude")[0] == 2


def test_profile_file_problems_are_reported_not_ignored(
    tmp_path: Path, profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile.parent.mkdir(parents=True)
    profile.write_text("{not json")
    assert run(capsys, "resolve", "--engine", "claude")[0] == 2

    run(capsys, "init", "--accept-defaults", "--replace")
    document = json.loads(profile.read_text())
    document["extra"] = True
    profile.write_text(json.dumps(document))
    status, _, err = run(capsys, "show")
    assert status == 2
    assert "unknown keys" in err

    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    profile.unlink()
    os.symlink(target, profile)
    assert run(capsys, "resolve", "--engine", "claude")[0] == 2


def test_profile_location_follows_xdg_and_rejects_relative_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ACTIVELOOM_REVIEW_PROFILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert run(capsys, "path")[1].strip() == str(
        tmp_path / "activeloom/review-profile.json"
    )
    monkeypatch.setenv("ACTIVELOOM_REVIEW_PROFILE", "relative.json")
    assert run(capsys, "path")[0] == 2


def test_detect_reports_each_engine_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "agy"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    report = json.loads(run(capsys, "detect")[1])
    assert report["gemini"] == {"cli": "agy", "installed": True}
    assert report["claude"]["installed"] is False
