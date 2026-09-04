"""Tests for the sync-v2 manifest shape and the one consumer config schema.

`test_sync_engine.py` covers what one target does; this covers the layer above
it — which target sets a consumer receives, which gates govern each of them,
and how a repository still carrying the pre-sync-v2 per-harness config files is
read.

The fixtures here write manifests in the real shape rather than through
`test_sync_engine.py`'s adapter, because that shape is the thing under test.
"""
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


# ---------------------------------------------------------------------------
# Manifest shape
# ---------------------------------------------------------------------------


def test_only_declared_harnesses_are_delivered(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(consumer / CANONICAL, {"harnesses": ["claude"], "allowed_destinations": ["**"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".claude/skills/a/SKILL.md").is_file()
    # A repository that never ran Codex must not acquire a `.codex` tree by
    # syncing from an upstream that happens to define the harness.
    assert not (consumer / ".codex").exists()


def test_shared_targets_reach_every_consumer(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(upstream)
    _write(consumer / CANONICAL, {"harnesses": ["codex"], "allowed_destinations": ["**"]})

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
    # Two harnesses claiming one filename makes the shim's filename-to-harness
    # mapping ambiguous, and it is resolved before anything has been read that
    # could disambiguate it.
    _manifest(upstream)
    doc = yaml.safe_load((upstream / "scripts" / "sync-targets.yml").read_text())
    doc["harnesses"]["codex"]["legacy_config"] = ".platform-config.yml"
    _write(upstream / "scripts" / "sync-targets.yml", doc)
    _write(consumer / CANONICAL, {"harnesses": ["claude"]})

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert "both declare `legacy_config: .platform-config.yml`" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Consumer config schema
# ---------------------------------------------------------------------------


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
    # Same fail-closed reasoning as the manifest's unknown-field check: every
    # optional key here *enables* something, so a typo silently disables it
    # and every gate still reads green.
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
    # The gate that bounds the write surface is the one composition rule that
    # must not union: unioning would hand every harness every other harness's
    # surface, which is exactly the separation three config files provided.
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
) -> None:
    _manifest(upstream)
    _write(
        consumer / CANONICAL,
        {"harnesses": {"claude": {}}, "allowed_destinations": [".claude/**", ".github/**"]},
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
            "harnesses": {"claude": {"skip_targets": [".claude/skills/a/SKILL.md"]}, "codex": {}},
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


# ---------------------------------------------------------------------------
# Telemetry gates
# ---------------------------------------------------------------------------


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
    # Settings-declared environment beats the ambient shell, so a consumer
    # that declared no gate must get an env block that names none — not one
    # defaulted to `off`, which would override a developer who exported `on`.
    rendered = _render_telemetry(sync_engine, upstream, consumer, monkeypatch, None, omit=True)
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
    # `emit: on` parses as True long before the engine sees a string, and the
    # rendered order follows the engine's gate table so reordering two lines
    # in a consumer config does not churn the sync diff.
    rendered = _render_telemetry(
        sync_engine, upstream, consumer, monkeypatch, {"extract": False, "emit": True}
    )
    assert (
        '{ "LOOM_REVIEW_TELEMETRY": "on", "LOOM_REVIEW_TELEMETRY_EXTRACT": "off" }' in rendered
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
        {"harnesses": ["claude"], "allowed_destinations": ["**"], "telemetry": telemetry},
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 1
    assert fragment in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Compatibility shim over the pre-sync-v2 config files
# ---------------------------------------------------------------------------


def test_legacy_files_compose_into_their_own_harnesses(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest(upstream)
    _write(consumer / ".platform-config.yml", {"allowed_destinations": [".claude/**", ".github/**"]})
    _write(consumer / ".codex-platform-config.yml", {"allowed_destinations": [".codex/**"]})

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
    # Absence is the consumer's real state, not a gap to be defaulted: a repo
    # that never carried `.codex-platform-config.yml` never ran that harness.
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
    # A shared target skipped in two files and synced by the third was being
    # routed to a single owner, not switched off. A union would silently
    # retire it at the cutover.
    _manifest(upstream)
    _write(consumer / ".platform-config.yml", {"allowed_destinations": ["**"]})
    _write(
        consumer / ".codex-platform-config.yml",
        {"allowed_destinations": ["**"], "skip_targets": [".github/shared.md"]},
    )

    assert _run(sync_engine, upstream, consumer, monkeypatch) == 0
    assert (consumer / ".github/shared.md").is_file()


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
    _write(consumer / CANONICAL, {"harnesses": ["claude"], "allowed_destinations": ["**"]})
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
    # The transitional invocation: a consumer mid-cutover still running one
    # workflow per upstream.
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
    assert "names a pre-sync-v2 per-harness config" in capsys.readouterr().err


def test_no_config_at_all_is_an_error(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Exit 2, like any other missing required file — not 1, which is reserved
    # for a config that exists and is wrong.
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
    # The config is the consent store, so it cannot be governed by the consent
    # it stores. After a compose there is more than one store on disk and each
    # one can grant what the others gate, so all of them are refused.
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
