"""Failure fixtures for the agent-loop config doctor and its --migrate mode."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / ".claude/skills/agent-loop/scripts/config-doctor.py"
TEMPLATE = ROOT / ".claude/skills/agent-loop/agent-loop.config.template"
CRITIQUE_SCRIPTS = ROOT / ".claude/skills/critique/scripts"
PINNED = {
    "AGENT_LOOP_CLAUDE_MODEL": "claude-review",
    "AGENT_LOOP_CLAUDE_EFFORT": "high",
    "AGENT_LOOP_CODEX_MODEL": "codex-review",
    "AGENT_LOOP_CODEX_EFFORT": "medium",
}
TAIL = " $AGENT_LOOP_PR_NUMBER; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "consumer"
    skill = project / ".claude/skills/agent-loop"
    scripts = skill / "scripts"
    ledger_dir = project / ".claude/skills/critique/scripts"
    scripts.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)
    shutil.copy2(ROOT / ".claude/skills/agent-loop/scripts/agent-loop-state.py", scripts)
    shutil.copy2(ROOT / ".claude/skills/agent-loop/scripts/review-push.sh", scripts)
    for name in ("review-ledger.js", "review-profile.py", "review-profile.defaults.json"):
        shutil.copy2(CRITIQUE_SCRIPTS / name, ledger_dir)
    # See tests/test_agent_loop.py: sync ships the sibling ESM manifest, and a
    # CommonJS consumer root is the context that needs it.
    shutil.copy2(ROOT / ".claude/skills/critique/scripts/package.json", ledger_dir)
    (project / "package.json").write_text(
        '{"name": "fixture-consumer", "private": true, "type": "commonjs"}\n',
        encoding="utf-8",
    )
    shutil.copy2(
        ROOT / ".claude/skills/agent-loop/prompt.txt.template",
        skill / "prompt.txt",
    )
    shutil.copy2(
        ROOT / ".claude/skills/agent-loop/agent-loop-instructions.md.template",
        project / "agent-loop-instructions.md",
    )
    (skill / "agent-loop.config").write_text(
        "review_contract_version = 3\n"
        "codex_review_hook = deepcritique $AGENT_LOOP_PR_NUMBER; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE\n"
        'claude_review_hook = claude --effort "$AGENT_LOOP_CLAUDE_EFFORT" /deepcritique $AGENT_LOOP_PR_NUMBER; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE\n',
        encoding="utf-8",
    )
    return project


def _run(
    project: Path,
    *,
    path_stubs: tuple[str, ...] = ("deepcritique", "claude"),
    args: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # The doctor resolves each review hook's program on PATH. Stub the fixture
    # hooks' programs outside the project tree so the no-mutation assertion and
    # the resolution check both hold on a machine without those CLIs.
    # PATH is exactly that directory: a developer box has the real CLIs
    # installed, and inheriting them would make the missing-CLI cases pass
    # for the wrong reason.
    bin_dir = project.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    for existing in bin_dir.iterdir():
        existing.unlink()
    for command in ("bash", "env", "node", "python3"):
        executable = shutil.which(command)
        assert executable is not None
        (bin_dir / command).symlink_to(executable)
    for name in path_stubs:
        stub = bin_dir / name
        stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("AGENT_LOOP_", "ACTIVELOOM_REVIEW_"))
    }
    env["PATH"] = str(bin_dir)
    env["ACTIVELOOM_REVIEW_PROFILE"] = str(project.parent / "review-profile.json")
    env.update(extra_env or {})
    return subprocess.run(
        ["python3", str(DOCTOR), "--project-dir", str(project), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_doctor_accepts_current_contract_without_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert "config_doctor = true" in (
        ROOT / ".claude/skills/agent-loop/agent-loop.config.template"
    ).read_text(encoding="utf-8")
    assert "CONFIG_DOCTOR=true" in (
        ROOT / ".claude/skills/agent-loop/scripts/agent-loop.sh"
    ).read_text(encoding="utf-8")
    before = sorted(path.relative_to(project) for path in project.rglob("*"))
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert "compatible" in result.stdout
    assert sorted(path.relative_to(project) for path in project.rglob("*")) == before


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("review_contract_version = 3", "review_contract_version = 2", "must be 3"),
        ("write-result", "AGENT_LOOP_REVIEW_OUTCOME_FILE", "obsolete review ownership"),
        ("/deepcritique", "/deepgrill", "must invoke deepcritique"),
        ("AGENT_LOOP_REVIEW_PUSH_HELPER", "git push", "review push helper"),
    ],
)
def test_doctor_failure_fixtures(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    config.write_text(config.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    result = _run(project)
    assert result.returncode != 0
    assert message in result.stderr


def test_doctor_rejects_worker_prompt_that_requires_masked_gh(tmp_path: Path) -> None:
    project = _project(tmp_path)
    prompt = project / ".claude/skills/agent-loop/prompt.txt"
    prompt.write_text(
        "Run gh issue view 7, then read AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY. Create a local commit; do not push.\n",
        encoding="utf-8",
    )
    result = _run(project)
    assert result.returncode != 0
    assert "require masked gh" in result.stderr


def test_doctor_rejects_worker_instructions_that_require_masked_gh(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    instructions = project / "agent-loop-instructions.md"
    instructions.write_text(
        "Read AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY, then run gh pr create.\n",
        encoding="utf-8",
    )
    result = _run(project)
    assert result.returncode != 0
    assert "require masked gh" in result.stderr


def test_doctor_warns_when_the_worker_is_not_told_where_to_write_its_handoff(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert "AGENT_LOOP_HANDOFF_FILE" not in result.stderr

    for path in (
        project / ".claude/skills/agent-loop/prompt.txt",
        project / "agent-loop-instructions.md",
    ):
        path.write_text(
            path.read_text(encoding="utf-8").replace("AGENT_LOOP_HANDOFF_FILE", "a local file"),
            encoding="utf-8",
        )
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert "do not name AGENT_LOOP_HANDOFF_FILE" in result.stderr


def test_doctor_rejects_incompatible_review_push_protocol(tmp_path: Path) -> None:
    project = _project(tmp_path)
    review_push = project / ".claude/skills/agent-loop/scripts/review-push.sh"
    review_push.write_text("#!/usr/bin/env bash\nprintf '2\\n'\n", encoding="utf-8")
    review_push.chmod(0o755)
    result = _run(project)
    assert result.returncode != 0
    assert "review-push protocol is incompatible" in result.stderr


def test_doctor_fails_before_claim_when_a_reviewer_cli_is_missing(tmp_path: Path) -> None:
    # Without this check a run claimed the issue, ran the worker, pushed, and
    # opened the draft PR before discovering the missing CLI at its review leg.
    project = _project(tmp_path)
    result = _run(project, path_stubs=("deepcritique",))
    assert result.returncode != 0
    assert "claude_review_hook invokes 'claude', which is not installed on PATH" in result.stderr


@pytest.mark.parametrize(
    ("hook_prefix", "stubs", "expect_failure"),
    [
        # A leading environment assignment is skipped; the program after it is resolved.
        ("FOO=1 claude", ("deepcritique", "claude"), False),
        ("FOO=1 claude", ("deepcritique",), True),
        # Shell syntax cannot be resolved statically and is left alone.
        ("if true; then claude", ("deepcritique",), False),
        (": && claude", ("deepcritique",), False),
    ],
)
def test_doctor_resolves_the_first_program_word_only(
    tmp_path: Path, hook_prefix: str, stubs: tuple[str, ...], expect_failure: bool
) -> None:
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    text = config.read_text(encoding="utf-8").replace(
        "claude_review_hook = claude", f"claude_review_hook = {hook_prefix}"
    )
    config.write_text(text, encoding="utf-8")
    result = _run(project, path_stubs=stubs)
    assert (result.returncode != 0) == expect_failure, result.stderr


def test_doctor_warns_when_a_codex_exec_hook_leaves_stdin_open(tmp_path: Path) -> None:
    # The wrapper redirects stdin itself; the warning protects the same hook
    # string when it is pasted and run outside the wrapper.
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    open_stdin = config.read_text(encoding="utf-8").replace(
        "codex_review_hook = deepcritique", "codex_review_hook = codex exec deepcritique"
    )
    config.write_text(open_stdin, encoding="utf-8")
    result = _run(project, path_stubs=("codex", "claude"))
    assert result.returncode == 0, result.stderr
    assert "warning: codex_review_hook runs codex exec without '</dev/null'" in result.stderr

    closed = open_stdin.replace(
        "--result-file $AGENT_LOOP_REVIEW_RESULT_FILE\nclaude_review_hook",
        "--result-file $AGENT_LOOP_REVIEW_RESULT_FILE </dev/null\nclaude_review_hook",
    )
    assert closed != open_stdin
    config.write_text(closed, encoding="utf-8")
    result = _run(project, path_stubs=("codex", "claude"))
    assert result.returncode == 0, result.stderr
    assert "codex exec" not in result.stderr


@pytest.mark.parametrize(
    ("override", "warns"),
    [
        ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=0 claude", True),
        ("env -u CLAUDE_CODE_DISABLE_BACKGROUND_TASKS claude", True),
        (": && unset CLAUDE_CODE_DISABLE_BACKGROUND_TASKS && claude", True),
        ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 claude", False),
    ],
)
def test_doctor_warns_when_a_hook_overrides_foreground_tasks(
    tmp_path: Path, override: str, warns: bool
) -> None:
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "claude_review_hook = claude", f"claude_review_hook = {override}"
        ),
        encoding="utf-8",
    )
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert (
        "claude_review_hook overrides CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" in result.stderr
    ) == warns


def _set_hooks(project: Path, *, claude: str | None = None, codex: str | None = None) -> Path:
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    lines = []
    for line in config.read_text(encoding="utf-8").splitlines():
        if claude is not None and line.startswith("claude_review_hook ="):
            line = f"claude_review_hook = {claude}{TAIL}"
        if codex is not None and line.startswith("codex_review_hook ="):
            line = f"codex_review_hook = {codex}{TAIL}"
        lines.append(line)
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config


def _write_profile(project: Path, **overrides: str) -> None:
    engines: dict[str, dict[str, object]] = {
        "claude": {"model": "claude-review", "effort": "high"},
        "codex": {"model": "codex-review", "effort": "medium"},
    }
    for name, value in overrides.items():
        engine, field = name.split("_")
        engines[engine][field] = value
    for settings in engines.values():
        settings["worker"] = dict(settings)
    defaults = json.loads(
        (CRITIQUE_SCRIPTS / "review-profile.defaults.json").read_text(encoding="utf-8")
    )
    (project.parent / "review-profile.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "defaults_version": defaults["defaults_version"],
                "confirmed_at": "2026-01-01T00:00:00Z",
                "engines": engines,
                "order": defaults["order"],
            }
        ),
        encoding="utf-8",
    )


# Realistic literal shapes: (hook key, hook command, flag the error names, literal value).
CONFLICTING_LITERALS = [
    ("claude", "claude --print --effort low /deepcritique", "--effort", "low"),
    ("claude", "claude --effort=low /deepcritique", "--effort", "low"),
    ("claude", "claude -p --model claude-other /deepcritique", "--model", "claude-other"),
    ("claude", "claude -p --model='claude-other' /deepcritique", "--model", "claude-other"),
    ("claude", 'timeout 3600 claude -p --model "claude-other" /deepcritique', "--model", "claude-other"),
    ("codex", "codex exec -m gpt-other /deepcritique", "-m", "gpt-other"),
    ("codex", "codex exec --model gpt-other /deepcritique", "--model", "gpt-other"),
    ("codex", "codex exec -c model=gpt-other /deepcritique", "-c model", "gpt-other"),
    (
        "codex",
        "codex exec -c model_reasoning_effort=high /deepcritique",
        "-c model_reasoning_effort",
        "high",
    ),
    (
        "codex",
        "codex exec -c 'model_reasoning_effort=\"xhigh\"' /deepcritique",
        "-c model_reasoning_effort",
        "xhigh",
    ),
]


@pytest.mark.parametrize(("engine", "command", "flag", "value"), CONFLICTING_LITERALS)
def test_doctor_refuses_a_hook_literal_that_conflicts_with_the_pinned_settings(
    tmp_path: Path, engine: str, command: str, flag: str, value: str
) -> None:
    project = _project(tmp_path)
    _set_hooks(project, **{engine: command})
    result = _run(
        project,
        path_stubs=("deepcritique", "claude", "codex", "timeout"),
        args=("--settings-from-env",),
        extra_env=PINNED,
    )
    assert result.returncode == 1
    field = "effort" if "effort" in flag else "model"
    expected = PINNED[f"AGENT_LOOP_{engine.upper()}_{field.upper()}"]
    assert f"{engine}_review_hook passes {flag} {value}" in result.stderr
    assert f"{engine} {field} {expected}" in result.stderr
    assert "--migrate" in result.stderr
    assert f"$AGENT_LOOP_{engine.upper()}_{field.upper()}" in result.stderr


def test_doctor_resolves_the_profile_when_run_standalone(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _set_hooks(project, claude="claude -p --effort low /deepcritique")

    _write_profile(project, claude_effort="low")
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert "matches the user profile" in result.stderr

    _write_profile(project, claude_effort="high")
    result = _run(project)
    assert result.returncode == 1
    assert "passes --effort low, but the user profile set claude effort high" in result.stderr
    assert "--migrate" in result.stderr

    # A repository override is honoured for the repository named by --repo.
    subprocess.run(
        [
            "python3",
            str(project / ".claude/skills/critique/scripts/review-profile.py"),
            "set",
            "--repo",
            "fixture/consumer",
            "claude.effort=low",
        ],
        env={**os.environ, "ACTIVELOOM_REVIEW_PROFILE": str(tmp_path / "review-profile.json")},
        check=True,
        capture_output=True,
    )
    result = _run(project, args=("--repo", "fixture/consumer"))
    assert result.returncode == 0, result.stderr
    assert "matches the repository override" in result.stderr


def test_doctor_accepts_a_matching_literal_with_a_migration_warning(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _set_hooks(project, claude="claude -p --model claude-review --effort high /deepcritique")
    result = _run(project, args=("--settings-from-env",), extra_env=PINNED)
    assert result.returncode == 0, result.stderr
    assert "claude_review_hook passes --model claude-review literally" in result.stderr
    assert "claude_review_hook passes --effort high literally" in result.stderr
    assert "--migrate" in result.stderr


def test_doctor_needs_no_profile_for_hooks_that_read_the_variables(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _set_hooks(
        project,
        claude='claude -p --model "$AGENT_LOOP_CLAUDE_MODEL" --effort="${AGENT_LOOP_CLAUDE_EFFORT}" /deepcritique',
    )
    assert not (tmp_path / "review-profile.json").exists()
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert "literally" not in result.stderr


def test_doctor_names_review_setup_when_a_literal_cannot_be_checked(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _set_hooks(project, claude="claude -p --effort low /deepcritique")
    result = _run(project)
    assert result.returncode == 1
    assert "cannot resolve claude reviewer settings" in result.stderr
    assert "review-setup" in result.stderr
    assert "--migrate" in result.stderr


def test_doctor_settings_from_env_requires_the_pinned_variables(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _set_hooks(project, claude="claude -p --effort low /deepcritique")
    result = _run(project, args=("--settings-from-env",))
    assert result.returncode == 1
    assert "needs AGENT_LOOP_CLAUDE_MODEL and AGENT_LOOP_CLAUDE_EFFORT" in result.stderr


@pytest.mark.parametrize(
    "hook",
    [
        # Another program's -m, a flag outside the engine's command, and prose.
        "python3 -m deepcritique; claude -p /deepcritique",
        "echo --effort low; claude -p /deepcritique",
        "claude -p '/deepcritique' && echo '--model x'",
    ],
)
def test_doctor_reads_literals_only_from_the_engine_command(tmp_path: Path, hook: str) -> None:
    project = _project(tmp_path)
    _set_hooks(project, claude=hook)
    # No profile exists, so any literal the doctor found would fail resolution.
    result = _run(project, path_stubs=("deepcritique", "claude", "echo"))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("key", ["claude_effort_policy", "worker_model", "worker_fallback_model", "worker_effort"])
def test_doctor_refuses_retired_keys_and_points_at_migrate(tmp_path: Path, key: str) -> None:
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    base = config.read_text(encoding="utf-8")

    config.write_text(base + f"{key} = low\n", encoding="utf-8")
    result = _run(project)
    assert result.returncode == 1
    assert f"{key} is retired" in result.stderr
    assert "--migrate" in result.stderr

    # An empty key has no effect, so it is reported rather than refused.
    config.write_text(base + f"{key} =\n", encoding="utf-8")
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert f"warning: {key} is retired and has no effect" in result.stderr


def test_doctor_no_longer_accepts_the_claude_effort_option(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = _run(project, args=("--claude-effort", "low"))
    assert result.returncode == 2
    assert "unrecognized arguments: --claude-effort" in result.stderr


LEGACY_CONFIG = """\
# Consumer-owned settings.
base_branch =
setup_hook = pnpm install --frozen-lockfile
# The gate runs the model tests.
validation_hook = pnpm test -- --model fixture --effort low
review_contract_version = 3
config_doctor = true
claude_effort_policy = low
review_max_rounds = 4
review_timeout_seconds = 7200
claude_review_hook = claude --print --effort low --model claude-old /deepcritique "$AGENT_LOOP_PR_NUMBER"; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE </dev/null
codex_review_hook = codex exec -m gpt-old -c model_reasoning_effort=medium -c 'model_reasoning_effort="high"' deepcritique "$AGENT_LOOP_PR_NUMBER"; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE </dev/null
worker_hook =
# Worker model keys.
worker_model = claude-old
worker_fallback_model =
worker_effort = low
worker_retries = 1
hook_timeout_seconds = 3600
"""

MIGRATED_CONFIG = """\
# Consumer-owned settings.
base_branch =
setup_hook = pnpm install --frozen-lockfile
# The gate runs the model tests.
validation_hook = pnpm test -- --model fixture --effort low
review_contract_version = 3
config_doctor = true
review_max_rounds = 4
review_timeout_seconds = 7200
claude_review_hook = claude --print --effort "$AGENT_LOOP_CLAUDE_EFFORT" --model "$AGENT_LOOP_CLAUDE_MODEL" /deepcritique "$AGENT_LOOP_PR_NUMBER"; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE </dev/null
codex_review_hook = codex exec -m "$AGENT_LOOP_CODEX_MODEL" -c model_reasoning_effort="$AGENT_LOOP_CODEX_EFFORT" -c model_reasoning_effort="$AGENT_LOOP_CODEX_EFFORT" deepcritique "$AGENT_LOOP_PR_NUMBER"; $AGENT_LOOP_REVIEW_PUSH_HELPER; review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE </dev/null
worker_hook =
# Worker model keys.
worker_retries = 1
hook_timeout_seconds = 3600
"""


def _migrate(project: Path) -> subprocess.CompletedProcess[str]:
    return _run(project, path_stubs=(), args=("--migrate",))


def test_migrate_rewrites_literals_and_removes_retired_keys_once(tmp_path: Path) -> None:
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    config.write_text(LEGACY_CONFIG, encoding="utf-8")
    config.chmod(0o640)
    prompt = project / ".claude/skills/agent-loop/prompt.txt"
    prompt_before = prompt.read_bytes()

    result = _migrate(project)
    assert result.returncode == 0, result.stderr
    assert config.read_text(encoding="utf-8") == MIGRATED_CONFIG
    assert config.stat().st_mode & 0o777 == 0o640
    assert prompt.read_bytes() == prompt_before
    assert "migrated claude_review_hook: --effort low -> $AGENT_LOOP_CLAUDE_EFFORT" in result.stdout
    assert "migrated codex_review_hook: -m gpt-old -> $AGENT_LOOP_CODEX_MODEL" in result.stdout
    for key in ("claude_effort_policy", "worker_model", "worker_fallback_model", "worker_effort"):
        assert f"migrated removed {key}" in result.stdout

    migrated = config.read_bytes()
    result = _migrate(project)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "agent-loop config doctor: nothing to migrate"
    assert config.read_bytes() == migrated

    # The migrated hooks pass the doctor on any profile.
    result = _run(
        project,
        path_stubs=("claude", "codex", "pnpm"),
        args=("--settings-from-env",),
        extra_env=PINNED,
    )
    assert result.returncode == 0, result.stderr
    assert "retired" not in result.stderr


def test_migrate_leaves_a_config_without_literals_byte_identical(tmp_path: Path) -> None:
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    config.write_text(config.read_text(encoding="utf-8") + "# trailing comment\r\n", encoding="utf-8")
    before = config.read_bytes()
    result = _migrate(project)
    assert result.returncode == 0, result.stderr
    assert "nothing to migrate" in result.stdout
    assert config.read_bytes() == before


def test_migrate_refuses_an_unparseable_config(tmp_path: Path) -> None:
    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    config.write_text("claude_effort_policy = low\nnot a config line\n", encoding="utf-8")
    result = _migrate(project)
    assert result.returncode == 1
    assert "invalid config line" in result.stderr
    assert config.read_text(encoding="utf-8").startswith("claude_effort_policy")


def test_template_ships_in_the_migrated_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    keys = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert keys.isdisjoint(
        {"claude_effort_policy", "worker_model", "worker_fallback_model", "worker_effort"}
    )
    spec = spec_from_file_location("config_doctor", DOCTOR)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    examples = re.findall(r"^#\s+((claude|codex)_review_hook)\s+=(.*)$", text, re.MULTILINE)
    assert [engine for _, engine, _ in examples] == ["claude", "codex"]
    for key, engine, hook in examples:
        assert f'"$AGENT_LOOP_{engine.upper()}_MODEL"' in hook
        assert f'"$AGENT_LOOP_{engine.upper()}_EFFORT"' in hook
        assert module._hook_literals(key, hook) == []

    project = _project(tmp_path)
    config = project / ".claude/skills/agent-loop/agent-loop.config"
    config.write_text(text, encoding="utf-8")
    result = _migrate(project)
    assert result.returncode == 0, result.stderr
    assert "nothing to migrate" in result.stdout
    assert config.read_text(encoding="utf-8") == text
