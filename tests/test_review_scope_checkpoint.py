"""A new review run on an over-scoped PR needs a recorded scope decision."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

HEAD = "a" * 40
BASE = "b" * 40


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / ".codex/skills/critique/scripts/local-review-handoff.py"
    )
    spec = importlib.util.spec_from_file_location("scope_controller", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_verify_signed_pr_history", lambda *_: None)
    monkeypatch.setattr(module, "_verify_head", lambda *_: None)
    return module


def start(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signals: dict[str, Any],
    *extra: str,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Review the scoped change.")
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(controller, "_issue_comments", lambda *_: rows or [])
    monkeypatch.setattr(controller, "_scope_signals", lambda *_: signals)

    def post(*args: Any) -> tuple[int, bool]:
        posted.append({"id": 20, "body": args[-1]})
        return 20, False

    monkeypatch.setattr(controller, "_post_issue_comment", post)
    args = controller._parser().parse_args(
        [
            "start-run",
            "--repo",
            "example/repo",
            "--pr",
            "1",
            "--head",
            HEAD,
            "--base",
            BASE,
            "--tier",
            "deep",
            "--authorization-file",
            str(authorization),
            *extra,
        ]
    )
    args.handler(args)
    return posted


def signals(commits: int = 0, files: list[str] | None = None) -> dict[str, Any]:
    return {"behaviour_commits": commits, "missing_patch_files": files or []}


@pytest.mark.parametrize(
    "scope", [signals(commits=6), signals(files=["scripts/large.sh"])]
)
def test_checkpoint_refuses_a_new_run_without_a_decision(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: dict[str, Any],
) -> None:
    posted: list[dict[str, Any]] = []
    with pytest.raises(controller.HandoffError, match="scope checkpoint"):
        posted = start(controller, monkeypatch, tmp_path, scope)
    assert posted == []


def test_below_the_checkpoint_the_run_content_is_unchanged(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    posted = start(controller, monkeypatch, tmp_path, signals(commits=5))
    record = controller._run_records(posted)[0]
    assert record["content"] == "Review the scoped change."


def test_a_decision_is_bound_after_the_engine_plan(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    posted = start(
        controller,
        monkeypatch,
        tmp_path,
        signals(commits=10, files=["scripts/large.sh"]),
        "--cycle",
        "claude,gemini",
        "--scope-decision",
        "keep",
    )
    record = controller._run_records(posted)[0]
    assert record["sequence"] == ["claude", "gemini"]
    assert record["content"].startswith(
        "<!-- local-review-sequence:v1 engines=claude,gemini -->\n\n"
        "<!-- local-review-scope:v1 behaviour-commits=10 missing-patch-files=1 "
        "decision=keep -->\n\n"
    )
    posted[0]["body"] = posted[0]["body"].replace("decision=keep", "decision=split")
    with pytest.raises(controller.HandoffError, match="digest"):
        controller._run_records(posted)


def test_an_active_run_replays_without_reading_scope(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    posted = start(controller, monkeypatch, tmp_path, signals())

    def unreachable(*_: Any) -> dict[str, Any]:
        raise AssertionError("a replay must not recount scope")

    monkeypatch.setattr(controller, "_run_end", lambda *_: None)
    replayed = start(controller, monkeypatch, tmp_path, signals(), rows=posted)
    assert replayed == []
    monkeypatch.setattr(controller, "_scope_signals", unreachable)
    args = SimpleNamespace(
        repo="example/repo",
        pr=1,
        head=HEAD,
        base=BASE,
        tier="deep",
        restart=False,
        authorization_file=str(tmp_path / "authorization.txt"),
    )
    controller._start_run(args)


def test_signals_count_behaviour_commits_and_patchless_files(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def commit(message: str, parents: int = 1) -> dict[str, Any]:
        return {"commit": {"message": message}, "parents": [{}] * parents}

    def changed(name: str, *, patch: bool, lines: int = 3, status: str = "modified") -> dict[str, Any]:
        row: dict[str, Any] = {
            "filename": name,
            "status": status,
            "additions": lines,
            "deletions": 0,
        }
        if patch:
            row["patch"] = "@@"
        return row

    pages = {
        "commits": [
            [
                commit("feat(loop): park a failed issue"),
                commit("fix: retry an empty pass\n\nbody"),
                commit("perf!: append events in shell"),
                commit("refactor: share statuses"),
                commit("test: cover parking"),
                commit("Merge main into the branch", parents=2),
                commit("fixup the fixture"),
            ]
        ],
        "files": [
            [
                changed("scripts/large.sh", patch=False),
                changed("scripts/small.sh", patch=True),
                changed("assets/logo.png", patch=False, lines=0),
                changed("scripts/gone.sh", patch=False, status="removed"),
            ]
        ],
    }

    def output(args: list[str], payload: Any = None) -> Any:
        return pages["commits" if "/commits?" in args[-1] else "files"]

    monkeypatch.setattr(controller, "_json_output", output)
    assert controller._scope_signals("example/repo", 1) == {
        "behaviour_commits": 3,
        "missing_patch_files": ["scripts/large.sh"],
    }
