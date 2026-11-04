"""Contract for the review-setup helper that launches `claude --cloud` on a pty."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "prompts/skills/review-setup/scripts/cloud-launch.py"
COPIES = [ROOT / root / "skills/review-setup/scripts/cloud-launch.py" for root in (".claude", ".codex", ".agents")]

pytestmark = pytest.mark.skipif(os.name != "posix", reason="needs a POSIX pty")

# Stands in for the CLI: refuses without a terminal, as `claude --cloud` does.
FAKE_CLAUDE = r"""#!{python}
import json, os, sys, time
mode = os.environ.get("FAKE_MODE", "url")
with open(os.environ["FAKE_ARGV"], "w") as fh:
    json.dump(sys.argv[1:], fh)
if not os.isatty(sys.stdout.fileno()):
    print("Error: --cloud requires an interactive terminal.", file=sys.stderr)
    sys.exit(1)
if mode == "url":
    print("\x1b[1mCreated cloud session\x1b[0m")
    print("View: \x1b[36mhttps://claude.ai/code/session_01AbC\x1b[0m.")
elif mode == "url-then-hang":
    print("https://claude.ai/code/session_hang", flush=True)
    time.sleep(60)
elif mode == "fail":
    print("Error: not signed in", file=sys.stderr)
    sys.exit(3)
elif mode == "hang":
    time.sleep(60)
"""


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cloud_launch", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    claude = tmp_path / "claude"
    claude.write_text(FAKE_CLAUDE.replace("{python}", sys.executable))
    claude.chmod(0o755)
    argv_file = tmp_path / "argv.json"
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Work #560.\nIt's \"quoted\" and $(not expanded).\n")
    monkeypatch.setenv("FAKE_ARGV", str(argv_file))
    return {"claude": claude, "argv": argv_file, "prompt": prompt}


def run(fake: dict[str, Path], *extra: str, mode: str = "url") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SOURCE), "--claude", str(fake["claude"]), "--prompt-file", str(fake["prompt"]), *extra],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "FAKE_MODE": mode},
    )


def test_fake_cli_refuses_without_a_terminal(fake: dict[str, Path]) -> None:
    result = subprocess.run(
        [str(fake["claude"]), "--cloud", "x"],
        capture_output=True,
        text=True,
        env={**os.environ, "FAKE_MODE": "url"},
    )
    assert result.returncode == 1
    assert "interactive terminal" in result.stderr


def test_prints_url_and_passes_prompt_verbatim(fake: dict[str, Path]) -> None:
    result = run(fake, "--model", "opus", "--effort", "high")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"url": "https://claude.ai/code/session_01AbC", "exit_code": 0, "timed_out": False}
    assert json.loads(fake["argv"].read_text()) == [
        "--cloud",
        fake["prompt"].read_text(),
        "--model",
        "opus",
        "--effort",
        "high",
    ]


def test_inherit_model_leaves_the_flag_out(fake: dict[str, Path]) -> None:
    assert run(fake, "--model", "inherit", "--effort", "medium").returncode == 0
    assert json.loads(fake["argv"].read_text()) == ["--cloud", fake["prompt"].read_text(), "--effort", "medium"]


def test_no_url_exits_1_with_cleaned_output(fake: dict[str, Path]) -> None:
    result = run(fake, mode="fail")
    assert result.returncode == 1
    out = json.loads(result.stdout)
    assert out["url"] is None and out["exit_code"] == 3 and out["timed_out"] is False
    assert "not signed in" in out["output_tail"]


def test_timeout_without_url(fake: dict[str, Path]) -> None:
    result = run(fake, "--timeout", "1", mode="hang")
    assert result.returncode == 1
    out = json.loads(result.stdout)
    assert out["url"] is None and out["timed_out"] is True


def test_cli_that_lingers_after_the_url_is_stopped(
    fake: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load()
    monkeypatch.setattr(module, "EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setenv("FAKE_MODE", "url-then-hang")
    code = module.main(["--claude", str(fake["claude"]), "--prompt-file", str(fake["prompt"]), "--timeout", "30"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["url"] == "https://claude.ai/code/session_hang" and out["timed_out"] is False


@pytest.mark.parametrize("case", ["missing-prompt", "empty-prompt", "missing-cli"])
def test_invalid_input_exits_2(fake: dict[str, Path], tmp_path: Path, case: str) -> None:
    claude, prompt = str(fake["claude"]), str(fake["prompt"])
    if case == "missing-prompt":
        prompt = str(tmp_path / "absent.md")
    elif case == "empty-prompt":
        Path(prompt).write_text("  \n")
    else:
        claude = str(tmp_path / "no-such-claude")
    result = subprocess.run(
        [sys.executable, "-I", str(SOURCE), "--claude", claude, "--prompt-file", prompt],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert result.stdout == ""


def test_find_url_ignores_other_hosts_and_escapes() -> None:
    module = load()
    assert module.find_url("docs https://code.claude.com/docs then \x1b[4mhttps://claude.ai/code/s_1\x1b[0m") == (
        "https://claude.ai/code/s_1"
    )
    assert module.find_url("https://example.com/claude.ai/code/x") is None


def test_rendered_copies_match_source() -> None:
    for copy in COPIES:
        assert copy.read_bytes() == SOURCE.read_bytes(), copy
        assert os.access(copy, os.X_OK), copy
