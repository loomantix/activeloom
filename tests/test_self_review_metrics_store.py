"""This repository's own reviews select the issue store without changing installs."""

import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
STORE = "LOOM_REVIEW_TELEMETRY_STORE"


def test_claude_sessions_select_issue_store() -> None:
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    assert settings["env"] == {STORE: "issue"}


def test_codex_sessions_select_issue_store() -> None:
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
    assert config["shell_environment_policy"]["set"] == {STORE: "issue"}


def test_skill_directories_carry_no_repository_gates() -> None:
    # install-skills.sh links these directories into user-level skill homes,
    # so a gate file here would switch every repository using those installs.
    for harness in (".claude", ".codex", ".agents"):
        assert not (ROOT / harness / "skills/critique/scripts/review-telemetry.json").exists()
