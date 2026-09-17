"""Tests for the sync-v2 manifest shape and consumer config schema."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


CANONICAL = ".activeloom-config.yml"


def _write(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))


def _harness(root: str, legacy: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
    return {"root": root, "legacy_config": legacy, "targets": targets}


def _copy(source: str, destination: str, **extra: Any) -> dict[str, Any]:
    return {"source": source, "destination": destination, "substitutions": [], **extra}


def _manifest(upstream: Path, **overrides: Any) -> None:
    """A two-harness manifest plus one shared target, with sources on disk."""
    (upstream / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("claude-src.md", "codex-src.md", "shared-src.md"):
        (upstream / name).write_text(f"{name}\n")
    doc: dict[str, Any] = {
        "harnesses": {
            "claude": _harness(
                ".claude",
                ".platform-config.yml",
                [_copy("claude-src.md", ".claude/skills/a/SKILL.md")],
            ),
            "codex": _harness(
                ".codex",
                ".codex-platform-config.yml",
                [_copy("codex-src.md", ".codex/skills/a/SKILL.md")],
            ),
        },
        "shared": {"targets": [_copy("shared-src.md", ".github/shared.md")]},
    }
    doc.update(overrides)
    _write(upstream / "scripts" / "sync-targets.yml", doc)


def _run(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: Path | None = None,
) -> int:
    argv = [
        "sync-engine.py",
        "--upstream-repo",
        str(upstream),
        "--consumer-dir",
        str(consumer),
    ]
    if config is not None:
        argv += ["--config", str(config)]
    monkeypatch.setattr("sys.argv", argv)
    return int(sync_engine.main())


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    path = tmp_path / "upstream"
    path.mkdir()
    return path


@pytest.fixture
def consumer(tmp_path: Path) -> Path:
    path = tmp_path / "consumer"
    path.mkdir()
    return path


def test_only_declared_harnesses_are_delivered(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL, {"harnesses": ["claude"], "allowed_destinations": ["**"]}
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".claude/skills/a/SKILL.md").is_file()
    assert not (consumer / ".codex").exists()


def test_shared_targets_reach_every_consumer(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL, {"harnesses": ["codex"], "allowed_destinations": ["**"]}
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".github/shared.md").is_file()


def test_manifest_rejects_unknown_top_level_key(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream, harneses={})  # codespell:ignore — deliberate typo
    _write(consumer / CANONICAL, {"harnesses": ["claude"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert "unknown top-level key(s): harneses" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["root", "legacy_config"])
def test_manifest_harness_requires_metadata(
    field: str,
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    doc = yaml.safe_load((upstream / "scripts" / "sync-targets.yml").read_text())
    del doc["harnesses"]["claude"][field]
    _write(upstream / "scripts" / "sync-targets.yml", doc)
    _write(consumer / CANONICAL, {"harnesses": ["claude"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert f"needs a non-empty string `{field}`" in capsys.readouterr().err


def test_manifest_rejects_duplicate_legacy_config(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    doc = yaml.safe_load((upstream / "scripts" / "sync-targets.yml").read_text())
    doc["harnesses"]["codex"]["legacy_config"] = ".platform-config.yml"
    _write(upstream / "scripts" / "sync-targets.yml", doc)
    _write(consumer / CANONICAL, {"harnesses": ["claude"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert (
        "both declare `legacy_config: .platform-config.yml`" in capsys.readouterr().err
    )


def test_harnesses_mapping_form_matches_list_form(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL,
        {"harnesses": {"claude": None, "codex": {}}, "allowed_destinations": ["**"]},
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".claude/skills/a/SKILL.md").is_file()
    assert (consumer / ".codex/skills/a/SKILL.md").is_file()


def test_config_requires_harnesses(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(consumer / CANONICAL, {"allowed_destinations": ["**"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert "`harnesses` is required" in capsys.readouterr().err


def test_config_rejects_empty_harnesses(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(consumer / CANONICAL, {"harnesses": []})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert "`harnesses` is empty" in capsys.readouterr().err


def test_config_rejects_harness_the_manifest_does_not_define(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(consumer / CANONICAL, {"harnesses": ["claude", "gemini"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "names gemini" in err
    assert "known: claude, codex" in err


@pytest.mark.parametrize(
    "doc,fragment",
    [
        ({"harnesses": ["claude"], "skipTargets": []}, "unknown key(s): skipTargets"),
        (
            {"harnesses": {"claude": {"skipTargets": []}}},
            "unknown key(s) under `harnesses.claude`: skipTargets",
        ),
    ],
)
def test_config_rejects_unknown_keys(
    doc: dict[str, Any],
    fragment: str,
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(consumer / CANONICAL, doc)

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert fragment in capsys.readouterr().err


def test_harness_allowed_destinations_replace_rather_than_union(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL,
        {
            "harnesses": {"claude": {"allowed_destinations": [".codex/**"]}},
            "allowed_destinations": ["**"],
        },
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "not in consumer's `allowed_destinations` (harness claude)" in err
    assert ".claude/skills/a/SKILL.md" in err


def test_top_level_allowed_destinations_govern_a_harness_that_declares_none(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL,
        {"harnesses": {"claude": {}}, "allowed_destinations": [".github/**"]},
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "not in consumer's `allowed_destinations` (harness claude)" in err
    assert ".claude/skills/a/SKILL.md" in err
    assert not (consumer / ".claude/skills/a/SKILL.md").exists()


def test_top_level_allowed_destinations_admit_an_inheriting_harness(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL,
        {
            "harnesses": {"claude": {}},
            "allowed_destinations": [".claude/**", ".github/**"],
        },
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".claude/skills/a/SKILL.md").is_file()


def test_skip_targets_union_across_levels(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL,
        {
            "harnesses": {
                "claude": {"skip_targets": [".claude/skills/a/SKILL.md"]},
                "codex": {},
            },
            "skip_targets": [".github/shared.md"],
            "allowed_destinations": ["**"],
        },
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert not (consumer / ".claude/skills/a/SKILL.md").exists()
    assert not (consumer / ".github/shared.md").exists()
    assert (consumer / ".codex/skills/a/SKILL.md").is_file()


def test_harness_substitutions_override_the_top_level(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    (upstream / "claude-src.md").write_text("value: <<NAME>>\n")
    doc = yaml.safe_load((upstream / "scripts" / "sync-targets.yml").read_text())
    doc["harnesses"]["claude"]["targets"][0]["substitutions"] = ["NAME"]
    _write(upstream / "scripts" / "sync-targets.yml", doc)
    _write(
        consumer / CANONICAL,
        {
            "harnesses": {"claude": {"substitutions": {"NAME": "narrow"}}},
            "substitutions": {"NAME": "broad"},
            "allowed_destinations": ["**"],
        },
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".claude/skills/a/SKILL.md").read_text() == "value: narrow\n"


def test_top_level_substitutions_reach_a_harness_that_does_not_redeclare_them(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    (upstream / "claude-src.md").write_text("own: <<NAME>>\nshared: <<SHARED>>\n")
    doc = yaml.safe_load((upstream / "scripts" / "sync-targets.yml").read_text())
    doc["harnesses"]["claude"]["targets"][0]["substitutions"] = ["NAME", "SHARED"]
    _write(upstream / "scripts" / "sync-targets.yml", doc)
    _write(
        consumer / CANONICAL,
        {
            "harnesses": {"claude": {"substitutions": {"NAME": "narrow"}}},
            "substitutions": {"NAME": "broad", "SHARED": "inherited"},
            "allowed_destinations": ["**"],
        },
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".claude/skills/a/SKILL.md").read_text() == (
        "own: narrow\nshared: inherited\n"
    )


def test_reserved_substitution_key_is_rejected(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL,
        {
            "harnesses": ["claude"],
            "substitutions": {"REVIEW_TELEMETRY_ENV": '{"X": "y"}'},
            "allowed_destinations": ["**"],
        },
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert "may not declare REVIEW_TELEMETRY_ENV" in capsys.readouterr().err


def _render_telemetry(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    telemetry: object,
    omit: bool = False,
) -> str:
    _manifest(upstream)
    (upstream / "claude-src.md").write_text('{ "env": <<REVIEW_TELEMETRY_ENV>> }\n')
    doc = yaml.safe_load((upstream / "scripts" / "sync-targets.yml").read_text())
    doc["harnesses"]["claude"]["targets"][0]["substitutions"] = ["REVIEW_TELEMETRY_ENV"]
    _write(upstream / "scripts" / "sync-targets.yml", doc)
    config: dict[str, Any] = {"harnesses": ["claude"], "allowed_destinations": ["**"]}
    if not omit:
        config["telemetry"] = telemetry
    _write(consumer / CANONICAL, config)
    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    return (consumer / ".claude/skills/a/SKILL.md").read_text()


def test_telemetry_absent_renders_an_empty_object(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty gate config must not default to off.
    rendered = _render_telemetry(
        sync_engine, upstream, consumer, monkeypatch, None, omit=True
    )
    assert json.loads(rendered)["env"] == {}


def test_telemetry_renders_only_declared_gates(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = _render_telemetry(
        sync_engine, upstream, consumer, monkeypatch, {"extract": "on"}
    )
    assert json.loads(rendered)["env"] == {"LOOM_REVIEW_TELEMETRY_EXTRACT": "on"}


def test_telemetry_accepts_yaml_booleans_and_fixes_key_order(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Boolean inputs are normalized and output order is deterministic.
    rendered = _render_telemetry(
        sync_engine, upstream, consumer, monkeypatch, {"extract": False, "emit": True}
    )
    assert (
        '{ "LOOM_REVIEW_TELEMETRY": "on", "LOOM_REVIEW_TELEMETRY_EXTRACT": "off" }'
        in rendered
    )


@pytest.mark.parametrize(
    "telemetry,fragment",
    [
        ({"emit": "maybe"}, "`telemetry.emit` must be `on` or `off`"),
        ({"emitt": "on"}, "unknown `telemetry` key(s): emitt"),
        ("on", "`telemetry` must be a mapping"),
    ],
)
def test_telemetry_rejects_bad_input(
    telemetry: object,
    fragment: str,
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL,
        {
            "harnesses": ["claude"],
            "allowed_destinations": ["**"],
            "telemetry": telemetry,
        },
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert fragment in capsys.readouterr().err


def test_legacy_files_compose_into_their_own_harnesses(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(
        consumer / ".platform-config.yml",
        {"allowed_destinations": [".claude/**", ".github/**"]},
    )
    _write(
        consumer / ".codex-platform-config.yml", {"allowed_destinations": [".codex/**"]}
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".claude/skills/a/SKILL.md").is_file()
    assert (consumer / ".codex/skills/a/SKILL.md").is_file()
    assert "Composed a sync-v2 config from" in capsys.readouterr().out


def test_a_missing_legacy_file_means_that_harness_is_absent(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(consumer / ".platform-config.yml", {"allowed_destinations": ["**"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".claude/skills/a/SKILL.md").is_file()
    assert not (consumer / ".codex").exists()


def test_shared_skip_is_the_intersection_of_the_legacy_files(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(consumer / ".platform-config.yml", {"allowed_destinations": ["**"]})
    _write(
        consumer / ".codex-platform-config.yml",
        {"allowed_destinations": ["**"], "skip_targets": [".github/shared.md"]},
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".github/shared.md").is_file()


def test_legacy_config_rejects_an_unknown_top_level_key(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(
        consumer / ".platform-config.yml",
        {"allowed_destination": [".claude/**"], "skip_targets": []},
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "unknown key(s): allowed_destination" in err
    assert not (consumer / ".claude").exists()


def test_shared_skip_survives_when_every_legacy_file_skips_it(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    for name in (".platform-config.yml", ".codex-platform-config.yml"):
        _write(
            consumer / name,
            {"allowed_destinations": ["**"], "skip_targets": [".github/shared.md"]},
        )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert not (consumer / ".github/shared.md").exists()


def test_legacy_files_disagreeing_on_a_substitution_fail_closed(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(consumer / ".platform-config.yml", {"substitutions": {"NAME": "one"}})
    _write(consumer / ".codex-platform-config.yml", {"substitutions": {"NAME": "two"}})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert "disagree on `substitutions.NAME`" in capsys.readouterr().err


def test_canonical_config_wins_over_surviving_legacy_files(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL, {"harnesses": ["claude"], "allowed_destinations": ["**"]}
    )
    _write(consumer / ".codex-platform-config.yml", {"allowed_destinations": ["**"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert not (consumer / ".codex").exists()


def test_explicit_legacy_config_selects_one_harness(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(consumer / ".platform-config.yml", {"allowed_destinations": ["**"]})
    _write(consumer / ".codex-platform-config.yml", {"allowed_destinations": ["**"]})

    rc = _run(
        sync_engine,
        upstream,
        consumer,
        monkeypatch,
        config=consumer / ".codex-platform-config.yml",
    )
    assert rc == 0
    assert (consumer / ".codex/skills/a/SKILL.md").is_file()
    assert not (consumer / ".claude").exists()
    # GitHub parses ::warning workflow commands from stdout.
    assert "names a pre-sync-v2 per-harness config" in capsys.readouterr().out


def test_no_config_at_all_is_an_error(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)

    with pytest.raises(SystemExit) as excinfo:
        _run(sync_engine, upstream, consumer, monkeypatch)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "missing required file" in err
    assert "no pre-sync-v2 config file" in err


def test_every_composed_config_file_is_protected_from_being_written(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    doc = yaml.safe_load((upstream / "scripts" / "sync-targets.yml").read_text())
    doc["harnesses"]["codex"]["targets"].append(
        _copy("codex-src.md", ".codex-platform-config.yml")
    )
    _write(upstream / "scripts" / "sync-targets.yml", doc)
    _write(consumer / ".platform-config.yml", {"allowed_destinations": ["**"]})
    _write(consumer / ".codex-platform-config.yml", {"allowed_destinations": ["**"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert "refusing to write the consumer's own sync config" in capsys.readouterr().err


@pytest.mark.parametrize("destination", [CANONICAL, ".codex-platform-config.yml"])
@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_sync_cannot_seed_a_future_config(
    destination: str,
    explicit: bool,
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    doc = yaml.safe_load((upstream / "scripts/sync-targets.yml").read_text())
    doc["harnesses"]["claude"]["targets"].append(_copy("claude-src.md", destination))
    _write(upstream / "scripts/sync-targets.yml", doc)
    config = consumer / ".platform-config.yml"
    _write(config, {"allowed_destinations": ["**"]})
    original = config.read_bytes()

    assert (
        _run(sync_engine, upstream, consumer, monkeypatch, config if explicit else None)
        == 1
    )
    assert not (consumer / destination).exists()
    assert not (consumer / ".claude").exists()
    assert config.read_bytes() == original


@pytest.mark.parametrize("bypass_preflight", [False, True])
def test_config_deletion_cannot_select_weaker_legacy_permissions(
    bypass_preflight: bool,
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    doc = yaml.safe_load((upstream / "scripts/sync-targets.yml").read_text())
    doc["harnesses"]["claude"]["targets"] = [{"destination": CANONICAL, "delete": True}]
    _write(upstream / "scripts/sync-targets.yml", doc)
    _write(
        consumer / CANONICAL,
        {
            "harnesses": ["claude"],
            "allowed_destinations": ["**"],
            "allow_sensitive_writes": [],
        },
    )
    _write(
        consumer / ".platform-config.yml",
        {
            "allowed_destinations": ["**"],
            "allow_sensitive_writes": [".github/workflows/example.yml"],
        },
    )
    original = (consumer / CANONICAL).read_bytes()
    if bypass_preflight:
        monkeypatch.setattr(sync_engine, "config_write_targets", lambda *args: [])

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert (consumer / CANONICAL).read_bytes() == original


@pytest.mark.parametrize("scope", ["codex", "shared"])
def test_legacy_sensitive_consent_stays_with_its_harness(
    scope: str,
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    workflow = ".github/workflows/example.yml"
    doc = yaml.safe_load((upstream / "scripts/sync-targets.yml").read_text())
    target = _copy("shared-src.md", workflow)
    if scope == "shared":
        doc["shared"]["targets"].append(target)
    else:
        doc["harnesses"][scope]["targets"].append(target)
    _write(upstream / "scripts/sync-targets.yml", doc)
    _write(
        consumer / ".platform-config.yml",
        {
            "allowed_destinations": ["**"],
            "allow_sensitive_writes": [workflow],
        },
    )
    _write(
        consumer / ".codex-platform-config.yml",
        {
            "allowed_destinations": ["**"],
            "allow_sensitive_writes": [],
        },
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == (
        0 if scope == "shared" else 1
    )
    assert (consumer / workflow).exists() is (scope == "shared")
    if scope == "codex":
        assert not (consumer / ".claude").exists()


def test_one_fail_open_legacy_file_keeps_the_shared_scope_open(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fail-open legacy file keeps the synthesized shared scope open."""
    _manifest(upstream)
    _write(consumer / ".platform-config.yml", {})
    _write(
        consumer / ".codex-platform-config.yml", {"allowed_destinations": [".codex/**"]}
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".github/shared.md").exists()


def test_shared_allowlist_is_enforced_when_every_legacy_file_declares_one(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The union still gates the shared scope when no input was fail-open."""
    _manifest(upstream)
    _write(consumer / ".platform-config.yml", {"allowed_destinations": [".claude/**"]})
    _write(
        consumer / ".codex-platform-config.yml", {"allowed_destinations": [".codex/**"]}
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert not (consumer / ".github/shared.md").exists()


def test_legacy_shared_skip_accepts_mixed_source_and_destination_spellings(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    for name, skip in [
        (".platform-config.yml", "shared-src.md"),
        (".codex-platform-config.yml", ".github/shared.md"),
    ]:
        _write(
            consumer / name, {"allowed_destinations": ["**"], "skip_targets": [skip]}
        )
    destination = consumer / ".github/shared.md"
    destination.parent.mkdir()
    destination.write_text("consumer-owned content\n")

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert destination.read_text() == "consumer-owned content\n"


@pytest.mark.parametrize("harness", ["claude", "codex", "gemini"])
def test_real_manifest_delivers_repository_telemetry(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "scripts/sync-targets.yml").read_text())
    selected = manifest["harnesses"][harness]
    # Assert gate reader and pass-key helper delivery for telemetry measurement.
    scripts = f"{selected['root']}/skills/critique/scripts"
    expected = {
        f"{scripts}/review-telemetry.json",
        f"{scripts}/review-telemetry-gates.js",
        f"{scripts}/telemetry-pass-key.js",
    }
    targets = [
        target
        for target in selected["targets"]
        if target.get("destination", "") in expected
    ]
    assert {target["destination"] for target in targets} == expected
    for target in targets:
        path = upstream / target["source"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((root / target["source"]).read_bytes())
    _write(
        upstream / "scripts/sync-targets.yml",
        {
            "harnesses": {harness: {**selected, "targets": targets}},
            "shared": {"targets": []},
        },
    )
    _write(
        consumer / CANONICAL,
        {
            "harnesses": [harness],
            "allowed_destinations": ["**"],
            "telemetry": {"emit": "on", "extract": "off"},
        },
    )
    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert json.loads((consumer / f"{scripts}/review-telemetry.json").read_text()) == {
        "LOOM_REVIEW_TELEMETRY": "on",
        "LOOM_REVIEW_TELEMETRY_EXTRACT": "off",
    }
    for name in ("review-telemetry-gates.js", "telemetry-pass-key.js"):
        delivered = consumer / scripts / name
        assert delivered.read_bytes() == (
            root / ".claude/skills/critique/scripts" / name
        ).read_bytes()
