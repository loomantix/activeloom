"""Contract for the per-user review profile helper that launchers resolve through."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

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
FILES = ("review-profile.py", "review-profile.defaults.json", "review-settings.py")


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
    assert stored["engines"]["claude"] == {
        "model": "opus",
        "effort": "high",
        "worker": {"model": "opus", "effort": "high"},
    }
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
    assert report["gemini"] == {
        "cli": "agy",
        "installed": True,
        "suggested_availability": "available",
    }
    assert report["claude"]["installed"] is False
    assert report["claude"]["suggested_availability"] == "unavailable"


# Schema v2: engine availability, worker settings, and missing-key status.

REVIEWER_V1 = {
    "claude": {"model": "opus", "effort": "medium"},
    "codex": {"model": "inherit", "effort": "high"},
    "gemini": {"model": "gemini-3.7-flash-high", "effort": "high"},
}


def write_profile(path: Path, **fields: object) -> None:
    document: dict[str, object] = {
        "schema_version": 2,
        "defaults_version": "2026.09.13",
        "confirmed_at": "2026-01-01T00:00:00Z",
        "engines": json.loads(json.dumps(REVIEWER_V1)),
        "order": {"lean": ["claude", "codex"], "deep": ["claude", "codex", "gemini"]},
    }
    document.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def test_defaults_prefill_worker_settings_from_reviewer_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    defaults = json.loads(run(capsys, "defaults")[1])
    assert defaults["schema_version"] == 3
    for settings in defaults["engines"].values():
        assert settings["worker"] == {
            "model": settings["model"],
            "effort": settings["effort"],
        }


def test_v1_profile_is_read_without_rewriting_and_reports_missing_worker_keys(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_profile(profile, schema_version=1)
    before = profile.read_bytes()
    status, out, _ = run(capsys, "resolve", "--engine", "claude")
    assert status == 0
    assert json.loads(out) == {
        "engine": "claude",
        "model": "opus",
        "effort": "medium",
        "source": "user profile",
    }
    assert run(capsys, "order", "--tier", "deep")[1].strip() == "claude,codex,gemini"
    report = json.loads(run(capsys, "show")[1])
    assert report["missing"] == [
        f"{engine}.worker.{field}"
        for engine in ("claude", "codex", "gemini")
        for field in ("model", "effort")
    ]
    assert report["suggested"]["claude.worker.model"] == "opus"
    assert report["suggested"]["codex.worker.effort"] == "high"
    status, out, _ = run(capsys, "check")
    assert status == 3
    assert json.loads(out)["missing"] == report["missing"]
    status, out, err = run(capsys, "resolve", "--engine", "codex", "--role", "worker")
    assert status == 3
    assert json.loads(out)["missing"] == ["codex.worker.model", "codex.worker.effort"]
    assert "review-setup" in err
    assert profile.read_bytes() == before

    assert (
        run(capsys, "set", "claude.worker.model=sonnet", "claude.worker.effort=low")[0]
        == 0
    )
    stored = json.loads(profile.read_text())
    assert stored["schema_version"] == 2
    assert stored["engines"]["claude"] == {
        "model": "opus",
        "effort": "medium",
        "worker": {"model": "sonnet", "effort": "low"},
    }
    assert stored["engines"]["codex"] == REVIEWER_V1["codex"]


def test_profile_without_version_2_settings_is_still_written_as_version_1(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Older synced copies of the helper read only version 1, and the profile is
    # shared by every repository on the machine.
    write_profile(profile, schema_version=1)
    assert run(capsys, "set", "claude.effort=high", "codex.fallback=none")[0] == 0
    assert run(capsys, "set", "--repo", "example/project", "codex.model=gpt-x")[0] == 0
    stored = json.loads(profile.read_text())
    assert stored["schema_version"] == 1
    assert run(capsys, "set", "--repo", "example/project", "codex.worker.effort=low")[
        0
    ] == 0
    assert json.loads(profile.read_text())["schema_version"] == 2
    assert run(capsys, "unset", "--repo", "example/project", "codex.worker.effort")[0] == 0
    assert json.loads(profile.read_text())["schema_version"] == 1
    assert run(capsys, "set", "gemini.availability=available")[0] == 0
    assert json.loads(profile.read_text())["schema_version"] == 2


def test_profile_missing_only_new_keys_exits_3_not_2(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engines = json.loads(json.dumps(REVIEWER_V1))
    del engines["gemini"]
    write_profile(profile, engines=engines)
    assert run(capsys, "resolve", "--engine", "claude")[0] == 0
    status, out, err = run(capsys, "launch-args", "--engine", "gemini")
    assert status == 3
    assert json.loads(out)["missing"] == ["gemini.model", "gemini.effort"]
    assert "exactly" not in err
    status, out, _ = run(capsys, "check", "--need", "gemini")
    assert status == 3
    report = json.loads(out)
    assert report["complete"] is False
    assert report["missing"] == ["gemini.model", "gemini.effort"]


def test_check_answers_whether_a_run_has_what_it_needs(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "check", "--need", "claude")
    assert status == 3
    assert json.loads(out) == {
        "complete": False,
        "configured": False,
        "missing": ["claude.model", "claude.effort"],
        "path": str(profile),
        "suggested": {},
        "unavailable": [],
    }
    write_profile(profile)
    assert run(capsys, "check", "--need", "claude", "codex", "order.deep")[0] == 0
    status, out, _ = run(capsys, "check", "--need", "claude.worker")
    assert status == 3
    assert json.loads(out)["missing"] == ["claude.worker.model", "claude.worker.effort"]
    assert run(capsys, "check", "--need", "copilot")[0] == 2
    assert run(capsys, "check", "--need", "claude.reviewer")[0] == 2

    assert run(capsys, "init", "--accept-defaults", "--replace")[0] == 0
    status, out, _ = run(capsys, "check")
    assert status == 0
    assert json.loads(out)["complete"] is True
    assert json.loads(out)["missing"] == []


def test_init_prefills_workers_from_the_chosen_reviewer_values(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(
            capsys,
            "init",
            "--accept-defaults",
            "claude.model=sonnet",
            "codex.effort=max",
            "codex.worker.effort=low",
        )[0]
        == 0
    )
    engines = json.loads(profile.read_text())["engines"]
    assert engines["claude"]["worker"] == {"model": "sonnet", "effort": "medium"}
    assert engines["codex"]["worker"] == {"model": "inherit", "effort": "low"}


def test_worker_settings_resolve_separately_from_reviewer_settings(
    profile: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    assert (
        run(capsys, "set", "claude.worker.model=sonnet", "claude.worker.effort=high")[0]
        == 0
    )
    assert json.loads(run(capsys, "resolve", "--engine", "claude")[1]) == {
        "engine": "claude",
        "model": "opus",
        "effort": "medium",
        "source": "user profile",
    }
    assert json.loads(
        run(capsys, "resolve", "--engine", "claude", "--role", "worker")[1]
    ) == {
        "engine": "claude",
        "role": "worker",
        "model": "sonnet",
        "effort": "high",
        "source": "user profile",
    }
    assert (
        run(capsys, "launch-args", "--engine", "claude", "--role", "worker")[1]
        == "sonnet\nhigh\n"
    )
    # Run pins cover reviewer settings only.
    monkeypatch.setenv("ACTIVELOOM_REVIEW_MODEL", "opus")
    monkeypatch.setenv("ACTIVELOOM_REVIEW_EFFORT", "max")
    assert (
        run(capsys, "launch-args", "--engine", "claude", "--role", "worker")[1]
        == "sonnet\nhigh\n"
    )


def test_worker_fallback_follows_the_codex_fallback_rules(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    before = profile.read_bytes()
    for assignments in (
        ["claude.worker.fallback.model=sonnet"],
        ["claude.worker.fallback.model=inherit", "claude.worker.fallback.effort=low"],
        ["claude.worker.fallback.model=sonnet", "claude.worker.fallback.effort=huge"],
        ["gemini.worker.model=inherit"],
        ["claude.worker.temperature=1"],
    ):
        assert run(capsys, "set", *assignments)[0] == 2, assignments
        assert profile.read_bytes() == before
    assert (
        run(
            capsys,
            "set",
            "claude.worker.fallback.model=sonnet",
            "claude.worker.fallback.effort=low",
        )[0]
        == 0
    )
    worker = json.loads(
        run(capsys, "resolve", "--engine", "claude", "--role", "worker")[1]
    )
    assert worker["fallback"] == {"model": "sonnet", "effort": "low"}
    assert "fallback" not in json.loads(run(capsys, "resolve", "--engine", "claude")[1])
    assert run(capsys, "set", "claude.worker.fallback=none")[0] == 0
    assert (
        json.loads(run(capsys, "resolve", "--engine", "claude", "--role", "worker")[1])[
            "fallback"
        ]
        is None
    )


def test_repository_overrides_may_set_worker_keys_but_not_availability(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    assert (
        run(
            capsys,
            "set",
            "--repo",
            "example/project",
            "codex.worker.effort=low",
            "codex.worker.fallback.model=example-model",
            "codex.worker.fallback.effort=medium",
        )[0]
        == 0
    )
    scoped = json.loads(
        run(
            capsys,
            "resolve",
            "--engine",
            "codex",
            "--role",
            "worker",
            "--repo",
            "example/project",
        )[1]
    )
    assert scoped["model"] == "inherit"
    assert scoped["effort"] == "low"
    assert scoped["fallback"] == {"model": "example-model", "effort": "medium"}
    assert scoped["source"] == "repository override"
    assert run(capsys, "unset", "--repo", "example/project", "codex.worker.effort")[0] == 0
    assert (
        run(capsys, "unset", "--repo", "example/project", "codex.worker.fallback")[0]
        == 0
    )
    assert "repos" not in json.loads(profile.read_text())

    before = profile.read_bytes()
    status, _, err = run(
        capsys, "set", "--repo", "example/project", "gemini.availability=unavailable"
    )
    assert status == 2
    assert "global" in err
    assert profile.read_bytes() == before


def test_unavailable_engines_are_stored_only_by_setup_and_kept_out_of_orders(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    before = profile.read_bytes()
    # The deep order still names gemini, so marking it unavailable is refused.
    status, _, err = run(capsys, "set", "gemini.availability=unavailable")
    assert status == 1
    assert "order.deep" in err
    assert profile.read_bytes() == before
    assert run(capsys, "set", "gemini.availability=maybe")[0] == 2
    assert (
        run(
            capsys, "set", "order.deep=claude,codex", "gemini.availability=unavailable"
        )[0]
        == 0
    )
    stored = json.loads(profile.read_text())
    assert stored["engines"]["gemini"]["availability"] == "unavailable"
    assert stored["engines"]["gemini"]["model"] == "gemini-3.7-flash-high"
    snapshot = profile.read_bytes()
    assert run(capsys, "set", "order.lean=claude,gemini")[0] == 1
    assert run(capsys, "set", "--repo", "example/project", "order.lean=gemini")[0] == 1
    assert profile.read_bytes() == snapshot

    assert run(capsys, "resolve", "--engine", "gemini")[0] == 1
    assert run(capsys, "launch-args", "--engine", "gemini", "--role", "worker")[0] == 1
    status, out, _ = run(capsys, "check", "--need", "claude", "gemini.worker")
    assert status == 1
    assert json.loads(out)["unavailable"] == ["gemini"]
    # A full check skips engines the user does not have.
    assert run(capsys, "check")[0] == 0

    assert run(capsys, "set", "gemini.availability=available")[0] == 0
    assert run(capsys, "resolve", "--engine", "gemini")[0] == 0


def test_init_leaves_unavailable_engines_out_of_proposed_orders(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(capsys, "init", "--accept-defaults", "gemini.availability=unavailable")[0]
        == 0
    )
    stored = json.loads(profile.read_text())
    assert stored["order"] == {"lean": ["claude", "codex"], "deep": ["claude", "codex"]}
    assert stored["engines"]["gemini"]["availability"] == "unavailable"
    status, _, _ = run(
        capsys,
        "init",
        "--accept-defaults",
        "--replace",
        "codex.availability=unavailable",
        "order.lean=codex",
    )
    assert status == 1
    assert json.loads(profile.read_text()) == stored


def test_detect_suggests_unavailability_but_never_writes_it(
    tmp_path: Path,
    profile: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0
    before = profile.read_bytes()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    report = json.loads(run(capsys, "detect")[1])
    assert {entry["suggested_availability"] for entry in report.values()} == {
        "unavailable"
    }
    assert profile.read_bytes() == before


# --- version skew between synced copies of this helper -----------------------
#
# The profile is one machine-global file, but the helper that reads it ships as
# a per-repository copy, so at any moment some checkouts are behind. That makes
# the file a wire protocol between helper versions rather than a local config,
# and these pin the parts of that contract a reader cannot re-derive: read
# forward, refuse to write what you cannot represent, and say so when a write
# raises the floor for everyone else.


def newer_profile(profile: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """A valid current profile relabelled as the next schema, with future content."""
    run(capsys, "init", "--accept-defaults", "--replace")
    document: dict[str, Any] = json.loads(profile.read_text())
    module = load()
    document["schema_version"] = module.SCHEMA_VERSION + 1
    document["telemetry"] = {"enabled": True}
    document["engines"]["claude"]["reasoning"] = "extended"
    document["engines"]["claude"].setdefault("worker", {})["concurrency"] = 4
    document["engines"]["qwen"] = {"model": "q", "effort": "high"}
    profile.write_text(json.dumps(document, indent=2))
    return document


def test_a_newer_profile_still_resolves_the_settings_this_helper_models(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    newer_profile(profile, capsys)

    status, out, _ = run(capsys, "resolve", "--engine", "claude")

    # The unknown engine, the unknown engine key and the unknown top-level key
    # are all set aside; the reviewer pair this helper does understand resolves.
    assert status == 0
    assert json.loads(out)["engine"] == "claude"


def test_a_newer_profile_is_never_written_back(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    newer_profile(profile, capsys)
    before = profile.read_bytes()

    status, _, err = run(capsys, "set", "claude.effort=high")

    # Writing would serialize only the modelled subset and silently delete the
    # rest, which is worse than refusing: the settings lost belong to a checkout
    # that is ahead, not behind.
    assert status == 1
    assert "sync this checkout" in err
    assert profile.read_bytes() == before


def test_a_profile_that_declares_a_reader_floor_is_refused_outright(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = newer_profile(profile, capsys)
    module = load()
    document["min_reader_version"] = module.SCHEMA_VERSION + 1
    profile.write_text(json.dumps(document, indent=2))

    status, _, err = run(capsys, "resolve", "--engine", "claude")

    # Forward tolerance assumes a newer schema only ADDED content. This is the
    # escape hatch for a change that does not hold that promise, so it must not
    # be read on a best-effort basis.
    assert status == 2
    assert "min_reader_version" not in err  # the message names the remedy, not the field
    assert "sync this checkout" in err


def test_raising_the_stored_version_warns_that_older_checkouts_lose_the_profile(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "init", "--accept-defaults", "--replace")
    # The recommended defaults already carry worker pairs, which is itself a
    # version 2 profile. Strip them to get the version 1 shape an older
    # checkout would have written.
    document = json.loads(profile.read_text())
    for settings in document["engines"].values():
        settings.pop("worker", None)
    document["schema_version"] = 1
    profile.write_text(json.dumps(document, indent=2))
    assert json.loads(profile.read_text())["schema_version"] == 1

    status, _, err = run(
        capsys, "set", "claude.worker.model=opus", "claude.worker.effort=medium"
    )

    # The only moment a human is present and the consequence is still cheap to
    # avoid. Storing a worker pair is what forces version 2, and every checkout
    # on a version 1 helper stops being able to read the file.
    assert status == 0
    assert json.loads(profile.read_text())["schema_version"] == 2
    assert "schema_version 2 (was 1)" in err
    assert "sync" in err


def test_tolerance_does_not_extend_to_the_current_schema(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "init", "--accept-defaults", "--replace")
    document = json.loads(profile.read_text())
    document["telemetry"] = {"enabled": True}
    profile.write_text(json.dumps(document, indent=2))

    # At or below this helper's version an unrecognized key is a typo or a
    # corrupted file, not content from the future. Guards the forward rule
    # against widening into "ignore anything unexpected".
    assert run(capsys, "show")[0] == 2


def run_with_reader_version(
    capsys: pytest.CaptureFixture[str], version: int, *argv: str
) -> tuple[int, str, str]:
    """Drive a reader that models an older schema than the writer on disk.

    `load()` returns a fresh module per call, so lowering SCHEMA_VERSION on one
    instance simulates a checkout that has not synced yet without vendoring a
    second copy of the helper that would rot on its own schedule.
    """
    module = load()
    # setattr rather than attribute assignment: `load()` is typed ModuleType,
    # whose attributes mypy --strict will not let us assign to by name.
    setattr(module, "SCHEMA_VERSION", version)
    status = module.main(list(argv))
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_the_current_writers_output_is_readable_by_an_older_reader(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The forward rule has to hold for real writer output, not only synthetic documents.

    Every other forward-tolerance test here builds the newer profile by hand. That
    proves the reader honours the rule but not that the writer stays inside it, so
    an additive change to the writer alone would pass them all and still break every
    checkout that has not synced — the exact failure this tolerance exists to stop.
    """
    run(capsys, "init", "--accept-defaults", "--replace")
    stored = json.loads(profile.read_text())["schema_version"]
    assert stored >= 2, "writer must store a version an older reader can be behind"

    status, out, _ = run_with_reader_version(
        capsys, stored - 1, "resolve", "--engine", "claude"
    )

    assert status == 0
    assert json.loads(out)["engine"] == "claude"


def test_an_older_reader_that_models_everything_present_may_still_write(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Forward tolerance must not refuse more than it has to.

    The write refusal is keyed on content this reader cannot represent, not on
    the version number alone. A reader that is merely behind, and models every
    setting the profile actually holds, loses nothing by saving it — refusing
    there would strand a checkout that is perfectly capable of the edit. The
    truncation case, where content really would be dropped, is covered by
    test_a_newer_profile_is_never_written_back.
    """
    run(capsys, "init", "--accept-defaults", "--replace")
    stored = json.loads(profile.read_text())["schema_version"]
    engines_before = json.loads(profile.read_text())["engines"]

    status, _, err = run_with_reader_version(
        capsys, stored - 1, "set", "claude.effort=high"
    )

    assert status == 0, err
    after = json.loads(profile.read_text())
    assert after["engines"]["claude"]["effort"] == "high"
    # Every engine and every setting that was there is still there.
    assert set(after["engines"]) == set(engines_before)
    for engine, settings in engines_before.items():
        assert set(after["engines"][engine]) >= set(settings)


def test_every_key_the_current_writer_emits_is_modelled_by_the_reader(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hold the shared key constants to the writer.

    `prune_foreign` keeps exactly the modelled keys and `validate_profile`
    rejects everything else. A key the writer emits but neither constant names
    would be pruned out of a newer profile silently, so this fails at the moment
    the writer gains it rather than when somebody's checkout stops reading.
    """
    run(capsys, "init", "--accept-defaults", "--replace")
    module = load()
    document = json.loads(profile.read_text())

    assert set(document) <= module.PROFILE_KEYS
    for engine, settings in document.get("engines", {}).items():
        assert set(settings) <= module.engine_settings_keys(engine)
        worker = settings.get("worker")
        if isinstance(worker, dict):
            assert set(worker) <= module.ENGINE_WORKER_KEYS
    for override in document.get("repos", {}).values():
        assert set(override) <= module.REPO_OVERRIDE_KEYS


def test_reviewit_availability_can_be_set_and_checked(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "init", "--accept-defaults")[0] == 0

    # Initially reviewit is not configured; check --need reviewit succeeds.
    status, out, _ = run(capsys, "check", "--need", "reviewit")
    assert status == 0

    # Marking reviewit unavailable succeeds and reflects in profile and show.
    status, _, _ = run(capsys, "set", "reviewit.availability=unavailable")
    assert status == 0
    stored = json.loads(profile.read_text())
    assert stored["reviewit"]["availability"] == "unavailable"

    status, out, _ = run(capsys, "show")
    assert status == 0
    assert json.loads(out)["reviewit"]["availability"] == "unavailable"

    # check --need reviewit now refuses with status 1.
    status, out, err = run(capsys, "check", "--need", "reviewit")
    assert status == 1
    assert "marked unavailable: reviewit" in err
    assert json.loads(out)["unavailable"] == ["reviewit"]

    # General check without --need still succeeds.
    assert run(capsys, "check")[0] == 0

    # Setting reviewit back to available succeeds.
    assert run(capsys, "set", "reviewit.availability=available")[0] == 0
    assert run(capsys, "check", "--need", "reviewit")[0] == 0

    # Invalid availability is rejected.
    assert run(capsys, "set", "reviewit.availability=maybe")[0] == 2

    # Repository override cannot set reviewit.
    status, _, err = run(
        capsys, "set", "--repo", "example/project", "reviewit.availability=unavailable"
    )
    assert status == 2
    assert "global only" in err


def test_a_profile_holding_reviewit_stays_readable_by_a_reader_without_it(
    profile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Storing `reviewit` must raise the stored version past readers that lack it.

    Stored at their own version, those readers treat the key as corruption and
    fail every preflight on the machine; stored above it, they read what they
    model and refuse only the write.
    """
    assert run(capsys, "init", "--accept-defaults", "--replace")[0] == 0
    status, _, err = run(capsys, "set", "reviewit.availability=unavailable")
    assert status == 0, err
    stored = json.loads(profile.read_text())["schema_version"]
    assert stored == load().SCHEMA_VERSION
    assert f"storing schema_version {stored}" in err

    def older_reader(*argv: str) -> tuple[int, str]:
        module = load()
        setattr(module, "SCHEMA_VERSION", stored - 1)
        setattr(module, "PROFILE_KEYS", module.PROFILE_KEYS - {"reviewit"})
        status = module.main(list(argv))
        return status, capsys.readouterr().out

    status, out = older_reader("resolve", "--engine", "claude")
    assert status == 0
    assert json.loads(out)["engine"] == "claude"

    before = profile.read_bytes()
    assert older_reader("set", "claude.effort=high")[0] == 1
    assert profile.read_bytes() == before
