"""Real Git objects, rather than fabricated matching surface versions."""

from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts/check-agy-surface.py"
LEDGER = "skills/critique/scripts/review-ledger.version"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), "--repo", str(repo), *args],
        capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    for root in (".agents", ".claude", ".codex"):
        path = tmp_path / root / LEDGER
        path.parent.mkdir(parents=True)
        path.write_text("1.11.0\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-qm", "ledger")
    pin = git(tmp_path, "rev-parse", "HEAD")
    for root in (".claude", ".codex"):
        (tmp_path / root / "skills/critique/scripts/run-agy-review.sh").write_text(
            f'agy_surface_sha="{pin}"\n'
        )
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-qm", "pin")
    return tmp_path


def test_matching_real_pin(repo: Path) -> None:
    assert run(repo).returncode == 0
    assert run(repo, "--ref", "HEAD").returncode == 0


def test_ledger_bump_without_pin_move(repo: Path) -> None:
    for root in (".agents", ".claude", ".codex"):
        (repo / root / LEDGER).write_text("1.12.0\n")
    result = run(repo)
    assert result.returncode == 1
    assert "ledger is 1.11.0; engine ledger is 1.12.0" in result.stderr
    assert "squash merges" in result.stderr
    # Release checking must inspect the candidate, not the working directory.
    assert run(repo, "--ref", "HEAD").returncode == 0


@pytest.mark.parametrize("root", [".agents", ".claude", ".codex"])
def test_each_engine_is_checked(repo: Path, root: str) -> None:
    (repo / root / LEDGER).write_text("different\n")
    result = run(repo)
    assert result.returncode == 1
    assert "versions differ" in result.stderr


def test_disagreeing_pins(repo: Path) -> None:
    (repo / ".codex/skills/critique/scripts/run-agy-review.sh").write_text(
        f'agy_surface_sha="{"f" * 40}"\n'
    )
    assert "pins differ" in run(repo).stderr


def test_missing_pin_fails_with_fetch_guidance(repo: Path) -> None:
    for root in (".claude", ".codex"):
        (repo / root / "skills/critique/scripts/run-agy-review.sh").write_text(
            f'agy_surface_sha="{"f" * 40}"\n'
        )
    result = run(repo)
    assert result.returncode == 1
    assert "fetch-depth: 0" in result.stderr


def test_release_gate_rejects_skew_before_querying_regression(repo: Path) -> None:
    for root in (".agents", ".claude", ".codex"):
        (repo / root / LEDGER).write_text("1.12.0\n")
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "skew")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/require-regression-green.sh"),
         git(repo, "rev-parse", "HEAD")],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "Agy surface check failed" in result.stderr


def test_workflow_gates_run_before_distribution() -> None:
    for name in ("sync-from-upstream.yml.template", "sync-from-upstream-token.yml.template"):
        source = (ROOT / ".github/workflows" / name).read_text()
        assert source.index("scripts/check-agy-surface.py") < source.index("- name: Run sync engine")
        assert "git fetch --depth=1" not in source
    publish = (ROOT / ".github/workflows/publish-activeloom.yml").read_text()
    assert publish.index("scripts/check-agy-surface.py") < publish.index("- name: Pack publish artifact")
    ci = (ROOT / ".github/workflows/ci.yml").read_text().split("  python-types-and-tests:")[1]
    assert ci.index("fetch-depth: 0") < ci.index("scripts/check-agy-surface.py")
