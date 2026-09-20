"""Integration coverage for the Codex-root agent-loop wrapper's pinned reviewer and worker settings."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = REPO_ROOT / ".codex/skills/agent-loop"
STATE_HELPER = SKILL / "scripts/agent-loop-state.py"
CRITIQUE_SCRIPTS = REPO_ROOT / ".codex/skills/critique/scripts"
LAUNCHER = SKILL / "scripts/run-codex-review.sh"
V3_TAIL = (
    " /deepcritique $AGENT_LOOP_PR_NUMBER; $AGENT_LOOP_REVIEW_PUSH_HELPER; "
    "node review-ledger.js write-result --result-file $AGENT_LOOP_REVIEW_RESULT_FILE"
)
V3_HOOKS = {
    "claude_review_hook": (
        'claude --model "$AGENT_LOOP_CLAUDE_MODEL" --effort "$AGENT_LOOP_CLAUDE_EFFORT"' + V3_TAIL
    ),
    "codex_review_hook": (
        'codex exec -m "$AGENT_LOOP_CODEX_MODEL" '
        '-c model_reasoning_effort="$AGENT_LOOP_CODEX_EFFORT"' + V3_TAIL
    ),
}
V4_HOOKS = {
    "claude_review_hook": '"$AGENT_LOOP_CODEX_REVIEW_LAUNCHER" --engine claude',
    "codex_review_hook": '"$AGENT_LOOP_CODEX_REVIEW_LAUNCHER" --engine codex',
}


def _profile(**engines: dict[str, object] | None) -> dict[str, object]:
    """A complete review profile; an engine given as None is removed."""
    defaults = json.loads(
        (CRITIQUE_SCRIPTS / "review-profile.defaults.json").read_text(encoding="utf-8")
    )
    settings: dict[str, dict[str, object]] = {
        "claude": {
            "model": "claude-review",
            "effort": "medium",
            "worker": {"model": "claude-worker", "effort": "high"},
        },
        "codex": {
            "model": "codex-review",
            "effort": "high",
            "worker": {
                "model": "worker-primary",
                "effort": "xhigh",
                "fallback": {"model": "worker-fallback", "effort": "medium"},
            },
        },
        "gemini": {
            "model": "gemini-review",
            "effort": "high",
            "worker": {"model": "gemini-worker", "effort": "high"},
        },
    }
    for engine, value in engines.items():
        if value is None:
            settings.pop(engine)
        else:
            settings[engine] = value
    return {
        "schema_version": 2,
        "defaults_version": defaults["defaults_version"],
        "confirmed_at": "2026-01-01T00:00:00Z",
        "engines": settings,
        "order": defaults["order"],
    }


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


# Records argv and the settings environment, then fails. The first worker
# attempt reports a capacity rejection so the wrapper switches to the fallback.
CLI_STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ['AGENT_STATE_DIR'])
record = {'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items()
          if k.startswith(('AGENT_LOOP_CLAUDE_', 'AGENT_LOOP_CODEX_', 'AGENT_LOOP_GEMINI_'))
          or k == 'AGENT_LOOP_NONINTERACTIVE'}}
with (state / (pathlib.Path(sys.argv[0]).name + '.jsonl')).open('a') as handle:
    handle.write(json.dumps(record) + '\n')
calls = sum(1 for _ in (state / (pathlib.Path(sys.argv[0]).name + '.jsonl')).open())
print('Selected model is at capacity' if calls == 1 else 'worker failed')
sys.exit(1)
"""

GH_STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ['AGENT_STATE_DIR'])
with (state / 'gh.log').open('a') as handle:
    handle.write(' '.join(args) + '\n')
claimed = state / 'claimed'
if args[:2] == ['repo', 'view']:
    print('owner/repo')
elif args[:2] == ['api', 'user']:
    print('tester')
elif args[:2] == ['issue', 'view']:
    print(json.dumps({'number': int(args[2]), 'title': 'fixture', 'body': '',
        'state': 'OPEN', 'labels': [{'name': 'dev: agent'}],
        'assignees': [{'login': 'tester'}] if claimed.exists() else []}))
elif args[:2] == ['issue', 'edit']:
    claimed.touch()
else:
    sys.exit(3)
"""

READY_STUB = r"""#!/usr/bin/env python3
import os, pathlib, sys
state = pathlib.Path(os.environ['AGENT_STATE_DIR'])
with (state / 'ready.log').open('a') as handle:
    handle.write(' '.join(sys.argv[1:]) + '\n')
print(os.environ.get('AGENT_READY_JSON', '[]'))
"""


class Consumer:
    def __init__(self, tmp_path: Path, contract: int, extra_config: str = "") -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "consumer"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        self.profile = tmp_path / "review-profile.json"
        self.state.mkdir()
        remote = tmp_path / "remote.git"
        _git("init", "--bare", str(remote), cwd=tmp_path)
        _git("init", "-b", "main", str(self.repo), cwd=tmp_path)
        for key, value in (
            ("user.name", "Test"),
            ("user.email", "test@example.invalid"),
            ("commit.gpgsign", "false"),
        ):
            _git("config", key, value, cwd=self.repo)

        skill = self.repo / ".codex/skills/agent-loop"
        shutil.copytree(SKILL / "scripts", skill / "scripts")
        critique = self.repo / ".codex/skills/critique/scripts"
        critique.mkdir(parents=True)
        for name in (
            "review-ledger.js",
            "review-settings.py",
            "review-profile.py",
            "review-profile.defaults.json",
        ):
            shutil.copy2(CRITIQUE_SCRIPTS / name, critique / name)
        _executable(self.repo / ".codex/skills/issues/scripts/ready.py", READY_STUB)
        shutil.copy2(SKILL / "prompt.txt.template", skill / "prompt.txt")
        shutil.copy2(
            SKILL / "agent-loop-instructions.md.template",
            self.repo / "agent-loop-instructions.md",
        )
        (self.repo / "AGENTS.md").write_text("# instructions\n", encoding="utf-8")
        if contract == 4:
            # The pinned base must carry both engines' review surfaces.
            for engine in ("codex", "claude"):
                paths = subprocess.run(
                    [str(LAUNCHER), "--required-paths", engine],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.split()
                for relative in paths:
                    target = self.repo / relative
                    if target.exists():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = REPO_ROOT / relative
                    if relative.endswith(("review-ledger.js", "package.json")):
                        shutil.copy2(source, target)
                    else:
                        target.write_text(f"{relative}\n", encoding="utf-8")
        hooks = V4_HOOKS if contract == 4 else V3_HOOKS
        (skill / "agent-loop.config").write_text(
            f"review_contract_version = {contract}\n"
            "config_doctor = true\n"
            "validation_hook = true\n"
            f"claude_review_hook = {hooks['claude_review_hook']}\n"
            f"codex_review_hook = {hooks['codex_review_hook']}\n"
            "retry_delay_seconds = 0\n"
            f"worktree_root = {tmp_path / 'worktrees'}\n"
            f"log_root = {tmp_path / 'logs'}\n" + extra_config,
            encoding="utf-8",
        )
        _git("add", ".", cwd=self.repo)
        _git("commit", "-m", "fixture", cwd=self.repo)
        _git("remote", "add", "origin", str(remote), cwd=self.repo)
        _git("push", "-u", "origin", "main", cwd=self.repo)

        _executable(self.bin / "gh", GH_STUB)
        # Contract v4 pins each reviewer's install surface, which must be a
        # package directory or a native executable.
        for engine in ("codex", "claude"):
            package = tmp_path / "packages" / engine
            _executable(package / engine, CLI_STUB)
            (package / "package.json").write_text('{"name": "stub"}\n', encoding="utf-8")
        self.profile.write_text(json.dumps(_profile()), encoding="utf-8")

    def run(
        self, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("AGENT_LOOP_", "ACTIVELOOM_REVIEW_", "GH_"))
        }
        env["PATH"] = os.pathsep.join(
            [
                str(self.bin),
                str(self.tmp / "packages/codex"),
                str(self.tmp / "packages/claude"),
                env["PATH"],
            ]
        )
        env["AGENT_STATE_DIR"] = str(self.state)
        env["ACTIVELOOM_REVIEW_PROFILE"] = str(self.profile)
        env.update(extra_env or {})
        return subprocess.run(
            [str(self.repo / ".codex/skills/agent-loop/scripts/agent-loop.sh"), *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )

    def log(self, name: str) -> str:
        path = self.state / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def calls(self, cli: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log(f"{cli}.jsonl").splitlines()]


def _assert_nothing_claimed(consumer: Consumer) -> None:
    assert "issue edit" not in consumer.log("gh.log")
    assert "issue view" not in consumer.log("gh.log")
    assert consumer.log("ready.log") == ""
    assert not (consumer.tmp / "worktrees").exists()


@pytest.mark.parametrize("contract", [3, 4])
def test_missing_profile_fails_closed_before_any_issue_is_touched(
    tmp_path: Path, contract: int
) -> None:
    consumer = Consumer(tmp_path, contract)
    consumer.profile.unlink()
    result = consumer.run("--issues", "5")
    assert result.returncode == 4, result.stderr
    [line] = [text for text in result.stderr.splitlines() if text.startswith("agent-loop:")]
    assert "is missing" in line and "codex.model" in line and "codex.worker.model" in line
    assert "review-setup" in line
    _assert_nothing_claimed(consumer)


def test_profile_missing_worker_keys_names_only_those(tmp_path: Path) -> None:
    consumer = Consumer(tmp_path, 3)
    consumer.profile.write_text(
        json.dumps(_profile(codex={"model": "codex-review", "effort": "high"})),
        encoding="utf-8",
    )
    result = consumer.run("--issues", "5")
    assert result.returncode == 4
    [line] = [text for text in result.stderr.splitlines() if text.startswith("agent-loop:")]
    assert "codex.worker.model" in line and "codex.worker.effort" in line
    assert "claude." not in line
    _assert_nothing_claimed(consumer)


def test_unavailable_reviewer_engine_fails_closed(tmp_path: Path) -> None:
    consumer = Consumer(tmp_path, 3)
    profile = _profile()
    profile["engines"]["claude"]["availability"] = "unavailable"  # type: ignore[index]
    consumer.profile.write_text(json.dumps(profile), encoding="utf-8")
    result = consumer.run("--issues", "5")
    assert result.returncode == 4
    assert "claude" in result.stderr and "unavailable" in result.stderr
    _assert_nothing_claimed(consumer)


@pytest.mark.parametrize("contract", [3, 4])
def test_dry_run_pins_reviewers_and_worker_from_the_profile(
    tmp_path: Path, contract: int
) -> None:
    # No claude_effort_policy: the Codex engine now launches with the Claude
    # effort the profile sets.
    consumer = Consumer(tmp_path, contract)
    result = consumer.run("--dry-run", extra_env={"AGENT_READY_JSON": '[{"number": 5}]'})
    assert result.returncode == 0, result.stderr
    assert "Pinned codex reviewer settings: model codex-review, effort high" in result.stdout
    assert "Pinned claude reviewer settings: model claude-review, effort medium" in result.stdout
    assert "Pinned codex worker settings: model worker-primary, effort xhigh" in result.stdout
    assert "agent-loop config doctor: compatible" in result.stdout
    assert "Dry-run only" in result.stdout
    assert "issue edit" not in consumer.log("gh.log")


def test_v4_settings_helper_runs_from_the_pinned_base(tmp_path: Path) -> None:
    consumer = Consumer(tmp_path, 4)
    helper = consumer.repo / ".codex/skills/critique/scripts/review-settings.py"
    helper.write_text(helper.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")
    result = consumer.run("--issues", "5")
    assert result.returncode == 1
    assert "review settings helper differs from the pinned base blob" in result.stderr
    _assert_nothing_claimed(consumer)


def test_retired_effort_policy_is_refused_by_the_doctor(tmp_path: Path) -> None:
    consumer = Consumer(tmp_path, 4, extra_config="claude_effort_policy = low\n")
    result = consumer.run("--issues", "5")
    assert result.returncode == 1
    assert "claude_effort_policy is retired" in result.stderr
    assert "Remove it from the config" in result.stderr
    _assert_nothing_claimed(consumer)


def test_conflicting_hook_literal_is_refused_before_claim(tmp_path: Path) -> None:
    consumer = Consumer(tmp_path, 3)
    config = consumer.repo / ".codex/skills/agent-loop/agent-loop.config"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            '--effort "$AGENT_LOOP_CLAUDE_EFFORT"', "--effort low"
        ),
        encoding="utf-8",
    )
    result = consumer.run("--issues", "5")
    assert result.returncode == 1
    assert "passes --effort low" in result.stderr and "effort medium" in result.stderr
    assert "Edit the hook so it reads $AGENT_LOOP_CLAUDE_EFFORT" in result.stderr
    _assert_nothing_claimed(consumer)


def test_default_worker_runs_pinned_settings_then_its_fallback(tmp_path: Path) -> None:
    consumer = Consumer(tmp_path, 3)
    result = consumer.run(
        "--issues",
        "5",
        extra_env={
            "AGENT_READY_JSON": '[{"number": 5}]',
            # Inherited values are discarded in favor of this run's pins.
            "AGENT_LOOP_CODEX_WORKER_MODEL": "inherited",
            "AGENT_LOOP_CLAUDE_EFFORT": "low",
        },
    )
    assert result.returncode != 0
    assert "issue edit 5 --add-assignee @me" in consumer.log("gh.log")
    first, second = consumer.calls("codex")
    assert first["argv"][:3] == ["exec", "--dangerously-bypass-approvals-and-sandbox", "-C"]
    assert first["argv"][4:8] == ["-m", "worker-primary", "-c", 'model_reasoning_effort="xhigh"']
    assert second["argv"][4:8] == ["-m", "worker-fallback", "-c", 'model_reasoning_effort="medium"']
    env = first["env"]
    assert env["AGENT_LOOP_NONINTERACTIVE"] == "1"
    assert env["AGENT_LOOP_CODEX_WORKER_MODEL"] == "worker-primary"
    assert env["AGENT_LOOP_CLAUDE_MODEL"] == "claude-review"
    assert env["AGENT_LOOP_CLAUDE_EFFORT"] == "medium"
    assert env["AGENT_LOOP_CODEX_MODEL"] == "codex-review"
    assert second["env"]["AGENT_LOOP_CODEX_WORKER_SOURCE"] == "capacity fallback"
    assert "Capacity fallback: codex worker model worker-fallback" in result.stdout
    pins = json.loads(
        next((tmp_path / "logs").glob("*/review-settings.json")).read_text(encoding="utf-8")
    )
    assert pins["worker_fallback_engines"] == ["codex"]


def test_inherit_worker_model_omits_the_model_flag(tmp_path: Path) -> None:
    consumer = Consumer(tmp_path, 3)
    profile = _profile()
    profile["engines"]["codex"]["worker"] = {"model": "inherit", "effort": "high"}  # type: ignore[index]
    consumer.profile.write_text(json.dumps(profile), encoding="utf-8")
    consumer.run("--issues", "5", extra_env={"AGENT_READY_JSON": '[{"number": 5}]'})
    calls = consumer.calls("codex")
    assert calls
    assert "-m" not in calls[0]["argv"]
    assert 'model_reasoning_effort="high"' in calls[0]["argv"]


def test_resume_launches_with_the_recorded_pins_after_the_profile_is_removed(
    tmp_path: Path,
) -> None:
    consumer = Consumer(tmp_path, 3)
    log_dir = tmp_path / "logs" / "consumer-issue-5-run"
    worktree = tmp_path / "worktrees" / "consumer-issue-5-run"
    log_dir.mkdir(parents=True, mode=0o700)
    worktree.mkdir(parents=True)
    pins = log_dir / "startup-pins.json"
    pins.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": "owner/repo",
                "review_settings": {
                    "codex": {"model": "recorded-codex", "effort": "low", "source": "profile"},
                    "claude": {"model": "recorded-claude", "effort": "high", "source": "profile"},
                },
                "worker_settings": {
                    "codex": {"model": "recorded-worker", "effort": "medium", "source": "profile"}
                },
            }
        ),
        encoding="utf-8",
    )
    pins.chmod(0o600)
    state_file = log_dir / "run-state.json"
    sha = "a" * 40
    subprocess.run(
        [
            "python3",
            str(STATE_HELPER),
            "create",
            "--file", str(state_file),
            "--run-id", "run",
            "--repo", "owner/repo",
            "--issue", "5",
            "--issue-title-sha256", "b" * 64,
            "--issue-body-sha256", "c" * 64,
            "--base-branch", "main",
            "--branch", "agent-loop/issue-5-run",
            "--worktree", str(worktree),
            "--log-dir", str(log_dir),
            "--pr", "9",
            "--pr-url", "https://example.invalid/pull/9",
            "--base-sha", sha,
            "--head-sha", sha,
            "--review-settings-file", str(pins),
        ],
        check=True,
        capture_output=True,
    )
    consumer.profile.unlink()
    result = consumer.run("--resume-run", str(state_file))
    assert (
        "Review settings restored from run state: Codex recorded-codex/low, "
        "Claude recorded-claude/high, worker recorded-worker/medium"
    ) in result.stdout, result.stderr
    assert "is missing" not in result.stderr
    restored = json.loads((log_dir / "review-settings.json").read_text(encoding="utf-8"))
    assert restored["review_settings"]["codex"]["model"] == "recorded-codex"


def _state_helper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(STATE_HELPER), *args], capture_output=True, text=True, check=False
    )


def _pin_file(path: Path, **extra: object) -> Path:
    document = {
        "version": 1,
        "repo": "owner/repo",
        "review_settings": {"codex": {"model": "m", "effort": "high", "source": "profile"}},
        "worker_settings": {
            "codex": {
                "model": "w",
                "effort": "high",
                "source": "profile",
                "fallback": {"model": "f", "effort": "low"},
            }
        },
        **extra,
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


def _create_state(tmp_path: Path, pin_file: Path | None) -> Path:
    state = tmp_path / "run-state.json"
    sha = "a" * 40
    args = [
        "create",
        "--file", str(state),
        "--run-id", "run",
        "--repo", "owner/repo",
        "--issue", "5",
        "--issue-title-sha256", "b" * 64,
        "--issue-body-sha256", "c" * 64,
        "--base-branch", "main",
        "--branch", "agent-loop/issue-5",
        "--worktree", str(tmp_path),
        "--log-dir", str(tmp_path),
        "--pr", "9",
        "--pr-url", "https://example.invalid/pull/9",
        "--base-sha", sha,
        "--head-sha", sha,
    ]
    if pin_file is not None:
        args += ["--review-settings-file", str(pin_file)]
    result = _state_helper(*args)
    assert result.returncode == 0, result.stderr
    return state


def test_state_records_pins_and_only_lets_them_grow(tmp_path: Path) -> None:
    pins = _pin_file(tmp_path / "pins.json")
    state = _create_state(tmp_path, pins)
    recorded = json.loads(_state_helper("show", "--file", str(state)).stdout)["reviewSettings"]
    assert recorded["worker_settings"]["codex"]["model"] == "w"

    switched = _pin_file(tmp_path / "pins.json", worker_fallback_engines=["codex"])
    assert _state_helper("settings-save", "--file", str(state), "--pin-file", str(switched)).returncode == 0

    changed = _pin_file(
        tmp_path / "pins.json",
        review_settings={"codex": {"model": "other", "effort": "high", "source": "profile"}},
        worker_fallback_engines=["codex"],
    )
    refused = _state_helper("settings-save", "--file", str(state), "--pin-file", str(changed))
    assert refused.returncode != 0
    assert "already pinned" in refused.stderr


def test_state_restore_writes_the_recorded_pins(tmp_path: Path) -> None:
    state = _create_state(tmp_path, _pin_file(tmp_path / "pins.json"))
    target = tmp_path / "restored.json"
    assert _state_helper("settings-restore", "--file", str(state), "--pin-file", str(target)).returncode == 0
    assert json.loads(target.read_text(encoding="utf-8"))["review_settings"]["codex"]["model"] == "m"
    assert target.stat().st_mode & 0o777 == 0o600


def test_state_without_pins_still_validates(tmp_path: Path) -> None:
    state = _create_state(tmp_path, None)
    target = tmp_path / "restored.json"
    assert _state_helper("settings-restore", "--file", str(state), "--pin-file", str(target)).returncode == 0
    assert not target.exists()


def test_state_rejects_a_malformed_pin_file(tmp_path: Path) -> None:
    pins = _pin_file(tmp_path / "pins.json", worker_fallback_engines=["claude"])
    state = tmp_path / "run-state.json"
    result = _state_helper(
        "create",
        "--file", str(state),
        "--run-id", "run",
        "--repo", "owner/repo",
        "--issue", "5",
        "--issue-title-sha256", "b" * 64,
        "--issue-body-sha256", "c" * 64,
        "--base-branch", "main",
        "--branch", "agent-loop/issue-5",
        "--worktree", str(tmp_path),
        "--log-dir", str(tmp_path),
        "--pr", "9",
        "--pr-url", "https://example.invalid/pull/9",
        "--base-sha", "a" * 40,
        "--head-sha", "a" * 40,
        "--review-settings-file", str(pins),
    )
    assert result.returncode != 0
    assert not state.exists()
