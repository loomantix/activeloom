"""Regression tests for deterministic local-review ledger mutations."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest


HEAD = "a" * 40
REPO = "example/repository"
PATCH = """@@ -10,3 +10,4 @@
 context
-old
+new
+more
 context
"""


@pytest.fixture(scope="session")
def review_ledger() -> ModuleType:
    path = (
        Path(__file__).resolve().parent.parent
        / ".codex"
        / "skills"
        / "grill"
        / "scripts"
        / "review-ledger.py"
    )
    spec = importlib.util.spec_from_file_location("review_ledger", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = ModuleType("review_ledger")
    sys.modules["review_ledger"] = module
    spec.loader.exec_module(module)
    return module


def test_diff_lines_tracks_both_sides(review_ledger: ModuleType) -> None:
    left, right = review_ledger._diff_lines(PATCH)
    assert left == {10, 11, 12}
    assert right == {10, 11, 12, 13}


def test_preflight_anchor_is_read_only_and_reports_verified_anchor(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        assert payload is None
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if "--paginate" in args:
            return json.dumps([[{"filename": "changed.ts", "patch": PATCH}]])
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(
        [
            "preflight-anchor",
            "--repo",
            REPO,
            "--pr",
            "7",
            "--head",
            HEAD,
            "--path",
            "changed.ts",
            "--line",
            "11",
        ]
    )
    assert len(calls) == 2
    assert json.loads(capsys.readouterr().out) == {
        "anchor": "RIGHT:11",
        "path": "changed.ts",
        "verified": True,
    }


def test_invalid_anchor_fails_before_posting(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = tmp_path / "finding.md"
    body.write_text(
        "<!-- local-review:v1 engine=codex round=1 head="
        f"{HEAD} fingerprint=anchor-test -->\nFinding",
        encoding="utf-8",
    )
    calls: list[tuple[list[str], dict[str, Any] | None]] = []

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        calls.append((args, payload))
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if "--paginate" in args:
            return json.dumps([[{"filename": "changed.ts", "patch": PATCH}]])
        raise AssertionError("mutation must not run for an invalid anchor")

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    with pytest.raises(review_ledger.LedgerError, match="nearest valid lines"):
        review_ledger.main(
            [
                "post-finding",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                HEAD,
                "--path",
                "changed.ts",
                "--line",
                "99",
                "--body-file",
                str(body),
            ]
        )
    assert len(calls) == 2


def test_stale_head_fails_before_reading_patch_or_posting(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        assert payload is None
        calls.append(args)
        return "b" * 40 + "\n"

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    with pytest.raises(review_ledger.LedgerError, match="PR head mismatch"):
        review_ledger.main(
            [
                "preflight-anchor",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                HEAD,
                "--path",
                "changed.ts",
                "--line",
                "11",
            ]
        )
    assert len(calls) == 1


def test_post_finding_uses_exact_json_schema_and_verifies_readback(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body_text = (
        "<!-- local-review:v1 engine=codex round=1 head="
        f"{HEAD} fingerprint=payload-test -->\nFinding"
    )
    body = tmp_path / "finding.md"
    body.write_text(body_text, encoding="utf-8")
    mutation_payloads: list[dict[str, Any]] = []

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if "--paginate" in args:
            return json.dumps([[{"filename": "changed.ts", "patch": PATCH}]])
        if args[-1] == f"repos/{REPO}/pulls/7/comments":
            assert payload is not None
            mutation_payloads.append(payload)
            return json.dumps({"id": 123})
        if args[-1] == f"repos/{REPO}/pulls/comments/123":
            return json.dumps({"id": 123, "body": body_text})
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    assert (
        review_ledger.main(
            [
                "post-finding",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                HEAD,
                "--path",
                "changed.ts",
                "--line",
                "11",
                "--body-file",
                str(body),
            ]
        )
        == 0
    )
    assert mutation_payloads == [
        {
            "body": body_text,
            "commit_id": HEAD,
            "path": "changed.ts",
            "line": 11,
            "side": "RIGHT",
        }
    ]
    assert json.loads(capsys.readouterr().out) == {
        "comment_id": 123,
        "verified": True,
    }


def test_file_level_fallback_is_explicit(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body_text = (
        "<!-- local-review:v1 engine=codex round=1 head="
        f"{HEAD} fingerprint=file-test -->\nFinding"
    )
    body = tmp_path / "finding.md"
    body.write_text(body_text, encoding="utf-8")
    posted: dict[str, Any] = {}

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if "--paginate" in args:
            return json.dumps([[{"filename": "changed.ts"}]])
        if args[-1] == f"repos/{REPO}/pulls/7/comments":
            assert payload is not None
            posted.update(payload)
            return json.dumps({"id": 456})
        if args[-1] == f"repos/{REPO}/pulls/comments/456":
            return json.dumps({"id": 456, "body": body_text})
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(
        [
            "post-finding",
            "--repo",
            REPO,
            "--pr",
            "7",
            "--head",
            HEAD,
            "--path",
            "changed.ts",
            "--file-level",
            "--body-file",
            str(body),
        ]
    )
    assert posted == {
        "body": body_text,
        "commit_id": HEAD,
        "path": "changed.ts",
        "subject_type": "file",
    }


def test_reply_uses_dedicated_endpoint_and_verifies_body(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body_text = (
        "<!-- local-review-disposition:v1 engine=codex round=1 head="
        f"{HEAD} fingerprint=reply-test outcome=fixed -->\nFixed"
    )
    body = tmp_path / "reply.md"
    body.write_text(body_text, encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any] | None]] = []

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        calls.append((args, payload))
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if args[-1] == f"repos/{REPO}/pulls/7/comments/88/replies":
            return json.dumps({"id": 99})
        if args[-1] == f"repos/{REPO}/pulls/comments/99":
            return json.dumps({"id": 99, "body": body_text})
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(
        [
            "reply",
            "--repo",
            REPO,
            "--pr",
            "7",
            "--head",
            HEAD,
            "--comment-id",
            "88",
            "--body-file",
            str(body),
        ]
    )
    assert calls[1][1] == {"body": body_text}


def test_post_pr_comment_uses_json_and_verifies_readback(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body_text = (
        "<!-- local-review-pass:v1 engine=codex round=1 head="
        f"{HEAD} -->\nNo new material findings."
    )
    body = tmp_path / "pass.md"
    body.write_text(body_text, encoding="utf-8")
    posted: list[dict[str, Any]] = []

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if args[-1] == f"repos/{REPO}/issues/7/comments":
            assert payload is not None
            posted.append(payload)
            return json.dumps({"id": 77})
        if args[-1] == f"repos/{REPO}/issues/comments/77":
            return json.dumps({"id": 77, "body": body_text})
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(
        [
            "post-pr-comment",
            "--repo",
            REPO,
            "--pr",
            "7",
            "--head",
            HEAD,
            "--body-file",
            str(body),
        ]
    )
    assert posted == [{"body": body_text}]


def test_empty_body_fails_before_github(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = tmp_path / "empty.md"
    body.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        review_ledger,
        "_run_gh",
        lambda *_args, **_kwargs: pytest.fail("GitHub must not be called"),
    )
    with pytest.raises(review_ledger.LedgerError, match="empty"):
        review_ledger.main(
            [
                "reply",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                HEAD,
                "--comment-id",
                "88",
                "--body-file",
                str(body),
            ]
        )


def test_resolve_requires_matching_head_and_verified_response(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, Any]] = []

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        assert args == ["api", "graphql"]
        assert payload is not None
        payloads.append(payload)
        if len(payloads) == 1:
            return json.dumps(
                {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": "THREAD", "isResolved": True}
                        }
                    }
                }
            )
        return json.dumps(
            {"data": {"node": {"id": "THREAD", "isResolved": True}}}
        )

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(
        [
            "resolve",
            "--repo",
            REPO,
            "--pr",
            "7",
            "--head",
            HEAD,
            "--thread-id",
            "THREAD",
        ]
    )
    assert len(payloads) == 2
    assert all(
        cast(dict[str, Any], payload["variables"]) == {"threadId": "THREAD"}
        for payload in payloads
    )
