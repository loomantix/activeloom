"""The Codex-root agent-loop config doctor: retired keys and hook literals."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".codex/skills/agent-loop"
DOCTOR = SKILL / "scripts/config-doctor.py"
TEMPLATE = SKILL / "agent-loop.config.template"
CRITIQUE_SCRIPTS = ROOT / ".codex/skills/critique/scripts"
PINNED = {
    "AGENT_LOOP_CLAUDE_MODEL": "claude-review",
    "AGENT_LOOP_CLAUDE_EFFORT": "medium",
    "AGENT_LOOP_CODEX_MODEL": "codex-review",
    "AGENT_LOOP_CODEX_EFFORT": "high",
}
TAIL = (
    " /deepcritique $AGENT_LOOP_PR_NUMBER; $AGENT_LOOP_REVIEW_PUSH_HELPER; "
    "node review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE"
)


def _project(tmp_path: Path, *, claude_hook: str, codex_hook: str, extra: str = "") -> Path:
    project = tmp_path / "consumer"
    skill = project / ".codex/skills/agent-loop"
    scripts = skill / "scripts"
    critique = project / ".codex/skills/critique/scripts"
    scripts.mkdir(parents=True)
    critique.mkdir(parents=True)
    for name in ("agent-loop-state.py", "review-push.sh", "run-codex-review.sh"):
        shutil.copy2(SKILL / "scripts" / name, scripts)
    for name in ("review-ledger.js", "review-profile.py", "review-profile.defaults.json"):
        shutil.copy2(CRITIQUE_SCRIPTS / name, critique)
    shutil.copy2(SKILL / "prompt.txt.template", skill / "prompt.txt")
    shutil.copy2(
        SKILL / "agent-loop-instructions.md.template", project / "agent-loop-instructions.md"
    )
    (skill / "agent-loop.config").write_text(
        "# consumer settings\n"
        "review_contract_version = 3\n"
        "config_doctor = true\n"
        f"claude_review_hook = {claude_hook}\n"
        f"codex_review_hook = {codex_hook}\n"
        "review_max_rounds = 4\n" + extra,
        encoding="utf-8",
    )
    return project


def _profile(claude_effort: str = "medium") -> dict[str, object]:
    defaults = json.loads(
        (CRITIQUE_SCRIPTS / "review-profile.defaults.json").read_text(encoding="utf-8")
    )
    engines = json.loads(json.dumps(defaults["engines"]))
    engines["claude"].update(model="claude-review", effort=claude_effort)
    engines["codex"].update(model="codex-review", effort="high")
    return {
        "schema_version": 2,
        "defaults_version": defaults["defaults_version"],
        "confirmed_at": "2026-01-01T00:00:00Z",
        "engines": engines,
        "order": defaults["order"],
    }


def _run(
    project: Path, *args: str, env_settings: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("AGENT_LOOP_", "ACTIVELOOM_REVIEW_"))
    }
    env["ACTIVELOOM_REVIEW_PROFILE"] = str(project.parent / "review-profile.json")
    env.update(env_settings or {})
    return subprocess.run(
        ["python3", str(DOCTOR), "--project-dir", str(project), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


CLAUDE_VARIABLES = 'claude --model "$AGENT_LOOP_CLAUDE_MODEL" --effort "$AGENT_LOOP_CLAUDE_EFFORT"' + TAIL
CODEX_VARIABLES = (
    'codex exec -m "$AGENT_LOOP_CODEX_MODEL" -c model_reasoning_effort="$AGENT_LOOP_CODEX_EFFORT"'
    + TAIL
)


def test_hooks_reading_the_pinned_variables_pass_without_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path, claude_hook=CLAUDE_VARIABLES, codex_hook=CODEX_VARIABLES)
    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
    result = _run(project, "--settings-from-env", env_settings=PINNED)
    assert result.returncode == 0, result.stderr
    assert "compatible" in result.stdout
    assert result.stderr == ""
    assert {path: path.read_bytes() for path in project.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize(
    ("claude_hook", "codex_hook", "flag", "literal", "pinned", "variable"),
    [
        (
            "claude --effort low" + TAIL,
            CODEX_VARIABLES,
            "--effort",
            "low",
            "medium",
            "$AGENT_LOOP_CLAUDE_EFFORT",
        ),
        (
            "claude --model opus" + TAIL,
            CODEX_VARIABLES,
            "--model",
            "opus",
            "claude-review",
            "$AGENT_LOOP_CLAUDE_MODEL",
        ),
        (
            CLAUDE_VARIABLES,
            "codex exec -m gpt-6-astra" + TAIL,
            "-m",
            "gpt-6-astra",
            "codex-review",
            "$AGENT_LOOP_CODEX_MODEL",
        ),
        (
            CLAUDE_VARIABLES,
            "codex exec -c model_reasoning_effort=max" + TAIL,
            "-c model_reasoning_effort",
            "max",
            "high",
            "$AGENT_LOOP_CODEX_EFFORT",
        ),
    ],
)
def test_conflicting_hook_literal_is_refused(
    tmp_path: Path,
    claude_hook: str,
    codex_hook: str,
    flag: str,
    literal: str,
    pinned: str,
    variable: str,
) -> None:
    project = _project(tmp_path, claude_hook=claude_hook, codex_hook=codex_hook)
    result = _run(project, "--settings-from-env", env_settings=PINNED)
    assert result.returncode == 1
    assert f"passes {flag} {literal}" in result.stderr
    assert f" {pinned}. " in result.stderr
    assert variable in result.stderr
    assert "Edit the hook so it reads" in result.stderr


def test_matching_literal_is_accepted_with_a_warning(tmp_path: Path) -> None:
    project = _project(
        tmp_path, claude_hook="claude --effort medium" + TAIL, codex_hook=CODEX_VARIABLES
    )
    result = _run(project, "--settings-from-env", env_settings=PINNED)
    assert result.returncode == 0, result.stderr
    assert "warning" in result.stderr
    assert "Edit the hook so it reads" in result.stderr


def test_standalone_run_checks_literals_against_the_review_profile(tmp_path: Path) -> None:
    project = _project(
        tmp_path, claude_hook="claude --effort low" + TAIL, codex_hook=CODEX_VARIABLES
    )
    (tmp_path / "review-profile.json").write_text(json.dumps(_profile()), encoding="utf-8")
    result = _run(project)
    assert result.returncode == 1
    assert "passes --effort low" in result.stderr
    assert "the user profile set claude effort medium" in result.stderr


def test_settings_from_env_requires_the_pinned_values(tmp_path: Path) -> None:
    project = _project(
        tmp_path, claude_hook="claude --effort medium" + TAIL, codex_hook=CODEX_VARIABLES
    )
    result = _run(project, "--settings-from-env")
    assert result.returncode == 1
    assert "AGENT_LOOP_CLAUDE_MODEL" in result.stderr


@pytest.mark.parametrize(
    "key", ["claude_effort_policy", "worker_model", "worker_fallback_model", "worker_effort"]
)
def test_retired_key_is_refused_when_set_and_warned_when_empty(tmp_path: Path, key: str) -> None:
    project = _project(
        tmp_path,
        claude_hook=CLAUDE_VARIABLES,
        codex_hook=CODEX_VARIABLES,
        extra=f"{key} = low\n",
    )
    refused = _run(project, "--settings-from-env", env_settings=PINNED)
    assert refused.returncode == 1
    assert f"{key} is retired" in refused.stderr
    assert "Remove it from the config" in refused.stderr

    config = project / ".codex/skills/agent-loop/agent-loop.config"
    config.write_text(
        config.read_text(encoding="utf-8").replace(f"{key} = low", f"{key} ="), encoding="utf-8"
    )
    warned = _run(project, "--settings-from-env", env_settings=PINNED)
    assert warned.returncode == 0, warned.stderr
    assert f"warning: {key} is retired" in warned.stderr


def test_template_ships_without_retired_keys_or_literals() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    for key in ("claude_effort_policy", "worker_model", "worker_fallback_model", "worker_effort"):
        assert not any(line.startswith(f"{key} ") for line in text.splitlines())
    hooks = {
        "claude_review_hook": ("AGENT_LOOP_CLAUDE_MODEL", "AGENT_LOOP_CLAUDE_EFFORT"),
        "codex_review_hook": ("AGENT_LOOP_CODEX_MODEL", "AGENT_LOOP_CODEX_EFFORT"),
    }
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip()
        if key not in hooks:
            continue
        value = line.split("=", 1)[1]
        # Every model and effort flag must read a pinned variable, never a literal.
        for flag_value in re.findall(
            r"(?:--model|--effort|-m|-c model=|-c model_reasoning_effort=)\s*(\S+)", value
        ):
            assert "$AGENT_LOOP_" in flag_value, f"{key} passes a literal: {flag_value}"
