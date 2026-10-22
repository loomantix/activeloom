"""A user-owned restart approval lets an orchestrated herder restart an ended run (#459)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = "acme/widgets"
HEAD = "c" * 40


def load() -> ModuleType:
    path = ROOT / "prompts/skills/hreview/scripts/herd-config.py"
    spec = importlib.util.spec_from_file_location("herd_config_restart", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


herd = load()


@pytest.fixture(autouse=True)
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    return tmp_path / "config"


def approve(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "repo": REPO,
        "pr": 42,
        "head": HEAD,
        "ttl_hours": 4,
        "deep": False,
        "scope_decision": None,
        "confirm": "restart 42\n",
    }
    values.update(overrides)
    result: dict[str, Any] = herd.approve_restart(**values)
    return result


def test_record_is_private_outside_the_checkout_and_listed_by_paths(
    config_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = approve(scope_decision="keep")
    path = Path(record["path"])
    assert path.parent == config_home / "activeloom/herd-approvals/acme__widgets"
    assert path.stat().st_mode & 0o777 == 0o600
    stored = json.loads(path.read_text())
    assert {k: stored[k] for k in ("repo", "pr", "head", "used", "scope_decision")} == {
        "repo": REPO,
        "pr": 42,
        "head": HEAD,
        "used": None,
        "scope_decision": "keep",
    }
    assert stored["expires"] > stored["created"]
    assert herd.main(["paths", "--repo", REPO, "--skill", "horch"]) == 0
    assert json.loads(capsys.readouterr().out)["approvals"] == str(path.parent)


def test_claim_is_single_use() -> None:
    record = approve()
    assert herd.check_restart(REPO, 42, HEAD)["id"] == record["id"]
    claimed = herd.claim_restart(REPO, 42, HEAD)
    assert claimed["id"] == record["id"] and claimed["used"]
    assert json.loads(Path(claimed["path"]).read_text())["used"] == claimed["used"]
    with pytest.raises(herd.ApprovalError):
        herd.claim_restart(REPO, 42, HEAD)
    with pytest.raises(herd.ApprovalError):
        herd.check_restart(REPO, 42, HEAD)


@pytest.mark.parametrize(
    ("repo", "pr", "head"),
    [("acme/other", 42, HEAD), (REPO, 43, HEAD), (REPO, 42, "d" * 40)],
)
def test_claim_requires_the_same_repo_pr_and_exact_head(repo: str, pr: int, head: str) -> None:
    approve()
    with pytest.raises(herd.ApprovalError):
        herd.claim_restart(repo, pr, head)
    assert herd.check_restart(REPO, 42, HEAD)["used"] is None


def test_a_record_whose_fields_disagree_with_its_name_is_refused() -> None:
    record = approve()
    path = Path(record["path"])
    stored = json.loads(path.read_text())
    stored["pr"] = 7
    path.write_text(json.dumps(stored))
    with pytest.raises(herd.ApprovalError, match="pr does not match"):
        herd.claim_restart(REPO, 42, HEAD)


def test_expired_approval_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    approve(ttl_hours=1)
    later = herd._now() + timedelta(hours=2)
    monkeypatch.setattr(herd, "_now", lambda: later)
    with pytest.raises(herd.ApprovalError, match="expired"):
        herd.claim_restart(REPO, 42, HEAD)


def test_group_writable_record_is_refused() -> None:
    path = Path(approve()["path"])
    os.chmod(path, 0o620)
    with pytest.raises(herd.ApprovalError, match="writable only by this user"):
        herd.claim_restart(REPO, 42, HEAD)


@pytest.mark.parametrize(
    "overrides",
    [{"confirm": "yes\n"}, {"ttl_hours": 0}, {"ttl_hours": 73}, {"head": "abc"}, {"pr": 0}],
)
def test_approve_refuses_bad_input_and_writes_nothing(
    config_home: Path, overrides: dict[str, Any]
) -> None:
    with pytest.raises(herd.UsageError):
        approve(**overrides)
    assert not (config_home / "activeloom/herd-approvals").exists()


def test_approve_needs_an_interactive_terminal(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("restart 42\n"))
    code = herd.main(["approve-restart", "--repo", REPO, "--pr", "42", "--head", HEAD])
    assert code == 2
    assert not (config_home / "activeloom/herd-approvals").exists()


def test_missing_approval_exits_4(capsys: pytest.CaptureFixture[str]) -> None:
    assert herd.main(["check-restart", "--repo", REPO, "--pr", "42", "--head", HEAD]) == 4
    assert "no valid restart approval" in capsys.readouterr().err


@pytest.mark.parametrize("root", [".claude", ".codex", ".agents"])
def test_skills_claim_but_never_write_an_approval(root: str) -> None:
    def skill(name: str) -> str:
        return (ROOT / root / "skills" / name / "SKILL.md").read_text()

    hreview = skill("hreview")
    ended = hreview.split("**Ended**", 1)[1].split("10. Roster", 1)[0]
    assert "check-restart --repo $repo --pr <n> --head <head>" in ended
    assert ended.index("`--restart` typed by the user") < ended.index("check-restart")
    # The record is read, and a missing scope decision stops, before the approval is used.
    assert ended.index("check-restart") < ended.index("do not claim it") < ended.index("`claim-restart`")
    assert ended.index("`claim-restart`") < ended.index("including a restart a peer asked for")
    assert "Any other exit from either command is a block" in ended
    assert "user restart approval <id>, recorded <created>" in ended
    assert "Never run `approve-restart` or write a record" in ended
    assert "approve-restart --repo <repo> --pr <n> --head <full head sha>" in ended

    horch = skill("horch")
    chain = horch.split("**Review chain**", 1)[1].split("**Build**", 1)[0]
    assert "check-restart --repo $repo --pr <n> --head <head>" in chain
    assert "it claims the approval itself" in chain
    assert chain.index("check-restart") < chain.index("**None** (exit 4")
    assert "Any other exit from `check-restart` is a block" in chain
    assert "<absolute path of $C> approve-restart" in chain
    assert "Never run `approve-restart` or write its record yourself" in chain


def test_a_head_sharing_the_filename_prefix_is_refused() -> None:
    approve()
    with pytest.raises(herd.ApprovalError, match="head does not match"):
        herd.claim_restart(REPO, 42, HEAD[:12] + "d" * 28)


def test_a_tampered_repo_field_is_refused() -> None:
    path = Path(approve()["path"])
    stored = json.loads(path.read_text())
    stored["repo"] = "acme/other"
    path.write_text(json.dumps(stored))
    with pytest.raises(herd.ApprovalError, match="repo does not match"):
        herd.claim_restart(REPO, 42, HEAD)


def test_a_split_cannot_be_approved(config_home: Path) -> None:
    with pytest.raises(herd.UsageError, match="split"):
        approve(scope_decision="split")
    assert not (config_home / "activeloom/herd-approvals").exists()


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_approve_from_a_terminal_stores_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", _Terminal("restart 42\n"))
    args = ["--repo", REPO, "--pr", "42", "--head", HEAD]
    code = herd.main(["approve-restart", *args, "--deep", "--scope-decision", "keep", "--ttl-hours", "2"])
    assert code == 0
    stored = herd.check_restart(REPO, 42, HEAD)
    assert stored["deep"] is True and stored["scope_decision"] == "keep"
    created, expires = (herd._parse_moment(Path("."), stored[key]) for key in ("created", "expires"))
    assert expires - created == timedelta(hours=2)
    assert herd.main(["claim-restart", *args]) == 0
    assert herd.main(["claim-restart", *args]) == 4


@pytest.mark.parametrize(
    "tamper",
    [
        {"expires": "2099-01-01T00:00:00"},
        {"expires": "2099-01-01T00:00:00Z"},
        {"created": "2099-01-01T00:00:00Z"},
        {"id": None},
        {"id": "../escape"},
        {"scope_decision": "split"},
        {"deep": "yes"},
    ],
)
def test_a_malformed_record_exits_4(tamper: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    path = Path(approve()["path"])
    stored = json.loads(path.read_text())
    stored.update(tamper)
    path.write_text(json.dumps(stored))
    assert herd.main(["claim-restart", "--repo", REPO, "--pr", "42", "--head", HEAD]) == 4
    assert "no valid restart approval" in capsys.readouterr().err
    assert path.exists()
