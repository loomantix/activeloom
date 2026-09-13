"""Execution-level contract for the automatic Claude review launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
HEAD = "a" * 40


PROFILE_SOURCE = ROOT / "prompts/skills/review-setup/scripts"


def _write_profile(path: Path, *, model: str, effort: str) -> None:
    defaults = json.loads((PROFILE_SOURCE / "review-profile.defaults.json").read_text())
    engines = defaults["engines"]
    engines["claude"] = {"model": model, "effort": effort}
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "defaults_version": defaults["defaults_version"],
                "confirmed_at": "2026-01-01T00:00:00Z",
                "engines": engines,
                "order": defaults["order"],
            }
        )
    )


def _invoke(
    tmp_path: Path, extra_env: dict[str, str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    launcher = tmp_path / "run-claude-review.sh"
    shutil.copyfile(
        ROOT / ".codex/skills/critique/scripts/run-claude-review.sh", launcher
    )
    shutil.copyfile(
        ROOT / ".codex/skills/critique/scripts/review-launch-state.py",
        tmp_path / "review-launch-state.py",
    )
    for name in ("review-profile.py", "review-profile.defaults.json"):
        shutil.copyfile(
            ROOT / ".codex/skills/critique/scripts" / name, tmp_path / name
        )
    # Only the launcher is under test; authorize the synthetic PR locally.
    (tmp_path / "local-review-handoff.py").write_text(
        "import sys\nassert sys.argv[1] == 'authorize-pass'\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scripts = {
        "git": (
            "import sys\n"
            "args = sys.argv[1:]\n"
            f"if args == ['rev-parse', 'HEAD']: print({HEAD!r})\n"
            "elif args == ['ls-remote', '--exit-code', 'origin', 'refs/heads/feature']:\n"
            f"    print({HEAD!r} + '\\trefs/heads/feature')\n"
            "elif args == ['status', '--porcelain']: pass\n"
            "else: raise SystemExit('unexpected git call')\n"
        ),
        "gh": (
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['repo', 'view']: print('example/repository')\n"
            "elif args[:2] == ['api', 'user']: print('reviewer')\n"
            "elif args[:2] == ['pr', 'view']:\n"
            f"    print({HEAD!r} + '\\tfeature\\texample/repository\\treviewer')\n"
            "else: raise SystemExit('unexpected gh call')\n"
        ),
        "claude": (
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['PROBE_RESULT']).write_text(json.dumps({\n"
            "    'background': os.environ.get('CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'),\n"
            "    'argv': sys.argv[1:],\n"
            "}))\n"
        ),
    }
    for name, source in scripts.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env python3\n" + source)
        path.chmod(0o755)
    env = {
        **{
            k: v
            for k, v in os.environ.items()
            if not k.startswith("ACTIVELOOM_")
            and k != "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
        },
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_REVIEW_CLI": str(bin_dir / "claude"),
        "PROBE_RESULT": str(tmp_path / "invocation.json"),
        "ACTIVELOOM_REVIEW_PROFILE": str(tmp_path / "review-profile.json"),
        **extra_env,
    }
    return subprocess.run(
        [
            "bash",
            str(launcher),
            "--repo",
            "example/repository",
            "--pr",
            "123",
            "--base",
            HEAD,
            "--head",
            HEAD,
            "--round",
            "1",
        ],
        cwd=tmp_path,
        env=env,
        check=check,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("inherited", [None, "0", "1"])
def test_launcher_disables_background_tasks(
    tmp_path: Path, inherited: str | None
) -> None:
    _write_profile(tmp_path / "review-profile.json", model="opus", effort="medium")
    extra = {} if inherited is None else {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": inherited}
    _invoke(tmp_path, extra)
    invocation = json.loads((tmp_path / "invocation.json").read_text())
    assert invocation["background"] == "1"
    assert invocation["argv"][:8] == [
        "--model",
        "opus",
        "--effort",
        "medium",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--print",
    ]


def test_launcher_omits_the_model_flag_for_an_inherited_model(tmp_path: Path) -> None:
    _write_profile(tmp_path / "review-profile.json", model="inherit", effort="xhigh")
    _invoke(tmp_path, {})
    argv = json.loads((tmp_path / "invocation.json").read_text())["argv"]
    assert "--model" not in argv
    assert argv[:2] == ["--effort", "xhigh"]


def test_run_pinned_settings_override_the_profile(tmp_path: Path) -> None:
    _write_profile(tmp_path / "review-profile.json", model="opus", effort="medium")
    _invoke(
        tmp_path,
        {"ACTIVELOOM_REVIEW_MODEL": "sonnet", "ACTIVELOOM_REVIEW_EFFORT": "low"},
    )
    argv = json.loads((tmp_path / "invocation.json").read_text())["argv"]
    assert argv[:4] == ["--model", "sonnet", "--effort", "low"]


@pytest.mark.parametrize(
    "profile_state", ["missing", "invalid-effort"], ids=["no-profile", "invalid"]
)
def test_launcher_refuses_to_start_without_a_valid_profile(
    tmp_path: Path, profile_state: str
) -> None:
    if profile_state == "invalid-effort":
        _write_profile(tmp_path / "review-profile.json", model="opus", effort="extreme")
    result = _invoke(tmp_path, {}, check=False)
    assert result.returncode != 0
    assert not (tmp_path / "invocation.json").exists()
    assert "review profile" in result.stderr
