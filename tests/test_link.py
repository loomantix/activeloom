"""Unit tests for `.codex/skills/issues/scripts/link.py`."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINK_SCRIPT = REPO_ROOT / ".codex/skills/issues/scripts/link.py"


def _load_link_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("link_script", LINK_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {LINK_SCRIPT}")
    module = ModuleType("link_script")
    module.__file__ = str(LINK_SCRIPT)
    sys.modules["link_script"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def link_mod() -> ModuleType:
    return _load_link_module()


def test_has_ref(link_mod: ModuleType) -> None:
    body = "## Dependencies\n- Blocked by #10\n- Blocks #20\n"
    assert link_mod.has_ref(body, "Blocked by", 10)
    assert link_mod.has_ref(body, "Blocks", 20)
    assert not link_mod.has_ref(body, "Blocked by", 20)
    assert not link_mod.has_ref(body, "Blocks", 10)


def test_add_ref_creates_section_when_missing(link_mod: ModuleType) -> None:
    body = "Issue description"
    result = link_mod.add_ref(body, "- Blocked by #10")
    assert "## Dependencies\n- Blocked by #10" in result
    assert result.startswith("Issue description\n\n## Dependencies")


def test_add_ref_appends_to_existing_section(link_mod: ModuleType) -> None:
    body = "Issue description\n\n## Dependencies\n- Blocked by #10\n\n## Acceptance Criteria\n- AC1"
    result = link_mod.add_ref(body, "- Blocks #20")
    expected = "Issue description\n\n## Dependencies\n- Blocked by #10\n- Blocks #20\n\n## Acceptance Criteria\n- AC1"
    assert result == expected


def test_link_issues_rejects_self_link(
    link_mod: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = link_mod.link_issues(10, "blocks", 10)
    assert rc == 1
    assert "Cannot link an issue to itself" in capsys.readouterr().err


def test_link_issues_blocks_writes_blocked_issue_first(
    link_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 10 blocks 20 => 20 is blocked (needs 'Blocked by #10'), 10 is blocking (needs 'Blocks #20')
    bodies = {
        10: "Issue 10 description",
        20: "Issue 20 description",
    }
    write_order: list[int] = []

    def fake_fetch_body(num: int) -> str:
        return bodies.get(num, "")

    def fake_set_body(num: int, body: str) -> None:
        write_order.append(num)
        bodies[num] = body

    monkeypatch.setattr(link_mod, "fetch_body", fake_fetch_body)
    monkeypatch.setattr(link_mod, "set_body", fake_set_body)

    rc = link_mod.link_issues(10, "blocks", 20)
    assert rc == 0
    assert write_order == [20, 10]  # 20 (blocked) MUST be written before 10 (blocking)
    assert link_mod.has_ref(bodies[20], "Blocked by", 10)
    assert link_mod.has_ref(bodies[10], "Blocks", 20)


def test_link_issues_blocked_by_writes_blocked_issue_first(
    link_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 20 blocked-by 10 => 20 is blocked (needs 'Blocked by #10'), 10 is blocking (needs 'Blocks #20')
    bodies = {
        10: "Issue 10 description",
        20: "Issue 20 description",
    }
    write_order: list[int] = []

    def fake_fetch_body(num: int) -> str:
        return bodies.get(num, "")

    def fake_set_body(num: int, body: str) -> None:
        write_order.append(num)
        bodies[num] = body

    monkeypatch.setattr(link_mod, "fetch_body", fake_fetch_body)
    monkeypatch.setattr(link_mod, "set_body", fake_set_body)

    rc = link_mod.link_issues(20, "blocked-by", 10)
    assert rc == 0
    assert write_order == [20, 10]  # 20 (blocked) written first
    assert link_mod.has_ref(bodies[20], "Blocked by", 10)
    assert link_mod.has_ref(bodies[10], "Blocks", 20)


def test_link_issues_preserves_concurrent_edit_to_reciprocal_side(
    link_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = {
        10: "Issue 10 description",
        20: "Issue 20 description",
    }

    def fake_fetch_body(num: int) -> str:
        return bodies.get(num, "")

    def fake_set_body(num: int, body: str) -> None:
        bodies[num] = body
        if num == 20:
            bodies[10] += "\n\nConcurrent user edit"

    monkeypatch.setattr(link_mod, "fetch_body", fake_fetch_body)
    monkeypatch.setattr(link_mod, "set_body", fake_set_body)

    assert link_mod.link_issues(10, "blocks", 20) == 0
    assert "Concurrent user edit" in bodies[10]
    assert link_mod.has_ref(bodies[20], "Blocked by", 10)
    assert link_mod.has_ref(bodies[10], "Blocks", 20)


def test_link_issues_emits_repair_guidance_on_reciprocal_failure(
    link_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bodies = {
        10: "Issue 10 description",
        20: "Issue 20 description",
    }

    def fake_fetch_body(num: int) -> str:
        return bodies.get(num, "")

    def fake_set_body(num: int, body: str) -> None:
        if num == 10:
            raise SystemExit(1)
        bodies[num] = body

    monkeypatch.setattr(link_mod, "fetch_body", fake_fetch_body)
    monkeypatch.setattr(link_mod, "set_body", fake_set_body)

    with pytest.raises(SystemExit):
        link_mod.link_issues(10, "blocks", 20)

    err = capsys.readouterr().err
    assert "#20 was updated with 'Blocked by #10'" in err
    assert "updating #10 with 'Blocks #20' failed" in err
    assert "To repair the reciprocal link manually" in err
