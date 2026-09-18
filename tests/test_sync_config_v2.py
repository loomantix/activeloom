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


def _harness(root: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
    return {"root": root, "targets": targets}


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
                [_copy("claude-src.md", ".claude/skills/a/SKILL.md")],
            ),
            "codex": _harness(
                ".codex",
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


def test_manifest_harness_requires_a_root(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    doc = yaml.safe_load((upstream / "scripts" / "sync-targets.yml").read_text())
    del doc["harnesses"]["claude"]["root"]
    _write(upstream / "scripts" / "sync-targets.yml", doc)
    _write(consumer / CANONICAL, {"harnesses": ["claude"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert "needs a non-empty string `root`" in capsys.readouterr().err


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
    assert f"missing required file: {consumer / CANONICAL}" in err
    assert "a consumer needs one config declaring which harnesses it runs" in err


@pytest.mark.parametrize(
    "retired",
    [".platform-config.yml", ".codex-platform-config.yml", ".gemini-platform-config.yml"],
)
def test_only_a_pre_sync_v2_config_is_a_clear_error(
    retired: str,
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one file the consumer has, the file it needs, and what to write."""
    _manifest(upstream)
    _write(consumer / retired, {"allowed_destinations": ["**"]})

    with pytest.raises(SystemExit) as excinfo:
        _run(sync_engine, upstream, consumer, monkeypatch)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert retired in err
    assert CANONICAL in err
    assert "declaring which harnesses this repository runs" in err
    assert not (consumer / ".claude").exists()


@pytest.mark.parametrize(
    "destination", [CANONICAL, ".platform-config.yml", ".gemini-platform-config.yml"]
)
def test_a_manifest_cannot_write_any_selectable_config_path(
    destination: str,
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Retired filenames stay unwritable even though the engine ignores them."""
    _manifest(upstream)
    doc = yaml.safe_load((upstream / "scripts/sync-targets.yml").read_text())
    doc["harnesses"]["claude"]["targets"].append(_copy("claude-src.md", destination))
    _write(upstream / "scripts/sync-targets.yml", doc)
    config = consumer / CANONICAL
    _write(config, {"harnesses": ["claude"], "allowed_destinations": ["**"]})
    original = config.read_bytes()

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert "refusing to write the consumer's own sync config" in capsys.readouterr().err
    assert not (consumer / ".claude").exists()
    assert config.read_bytes() == original


@pytest.mark.parametrize("bypass_preflight", [False, True])
def test_the_consumer_config_cannot_be_deleted_by_a_manifest(
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
    original = (consumer / CANONICAL).read_bytes()
    if bypass_preflight:
        monkeypatch.setattr(sync_engine, "config_write_targets", lambda *args: [])

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert (consumer / CANONICAL).read_bytes() == original


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
