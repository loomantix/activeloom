"""This repository's own reviews select the issue store without changing installs."""

import json
import os
from pathlib import Path
import subprocess
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
STORE = "LOOM_REVIEW_TELEMETRY_STORE"


def test_claude_sessions_select_issue_store() -> None:
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    assert settings["env"] == {STORE: "issue"}


def test_codex_sessions_select_issue_store() -> None:
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
    assert config["shell_environment_policy"]["set"] == {STORE: "issue"}


def test_new_herd_panes_select_issue_store() -> None:
    config = json.loads((ROOT / ".activeloom-herd.json").read_text())
    assert config["telemetry"] == {"store": "issue"}


@pytest.mark.parametrize(
    ("command", "harness"),
    [("claude", ".claude"), ("codex", ".codex"), ("agy", ".agents")],
)
@pytest.mark.parametrize("store", [None, "", "pr"])
def test_standalone_launch_routes_the_bundle_and_preserves_process_contract(
    tmp_path: Path, command: str, harness: str, store: str | None
) -> None:
    # A child process loads the real gate reader without starting a model session
    # or emitting telemetry. CLI-native settings are outside this launch test.
    executable = tmp_path / command
    executable.write_text(
        "#!/usr/bin/env node\n"
        "(async () => {\n"
        "  const {resolveGates} = await import(process.env.TEST_GATE);\n"
        "  console.log(JSON.stringify({gates: resolveGates(), "
        "argv: process.argv.slice(2), cwd: process.cwd()}));\n"
        "  process.exit(17);\n"
        "})();\n"
    )
    executable.chmod(0o755)
    env = dict(os.environ)
    env.pop(STORE, None)
    if store is not None:
        env[STORE] = store
    env.update(
        PATH=f"{tmp_path}{os.pathsep}{env['PATH']}",
        TEST_GATE=(
            ROOT / harness / "skills/critique/scripts/review-telemetry-gates.js"
        ).as_uri(),
        LOOM_REVIEW_TELEMETRY="off",
        LOOM_REVIEW_TELEMETRY_EXTRACT="off",
    )
    args = ["--model", "unchanged target", "a 'quoted' prompt", ""]
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/review-session.sh"), command, *args],
        cwd=ROOT / "scripts",
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 17, result.stderr
    observed = json.loads(result.stdout)
    assert observed["gates"]["store"] == (store or "issue")
    assert not observed["gates"]["emission"]["enabled"]
    assert not observed["gates"]["extraction"]["enabled"]
    assert observed["argv"] == args
    assert observed["cwd"] == str(ROOT / "scripts")


def test_standalone_launch_refuses_another_checkout(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/review-session.sh"), "agy", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "same checkout" in result.stderr


def test_skill_directories_carry_no_repository_gates() -> None:
    # install-skills.sh links these directories into user-level skill homes,
    # so a gate file here would switch every repository using those installs.
    for harness in (".claude", ".codex", ".agents"):
        assert not (ROOT / harness / "skills/critique/scripts/review-telemetry.json").exists()
