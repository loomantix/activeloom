"""Regression tests for deterministic local-review ledger mutations."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest


HEAD = "a" * 40
AFTER = "b" * 40
REPO = "example/repository"
PATCH = """@@ -10,3 +10,4 @@
 context
-old
+new
+more
 context
"""


def _review_row(comment_id: int, body: str, *, login: str = "reviewer") -> dict[str, Any]:
    return {"id": comment_id, "body": body, "user": {"login": login}}


def _finding_body(
    fingerprint: str,
    content: str = "Finding.",
    *,
    round_number: int = 2,
    severity: str = "major",
    lens: str = "correctness",
    head: str = HEAD,
    occurrence: int = 1,
) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return (
        f"<!-- local-review:v3 engine=codex round={round_number} head={head} "
        f"fingerprint={fingerprint} occurrence={occurrence} severity={severity} lens={lens} "
        f"content-sha256={digest} -->\n{content}"
    )


def _disposition_body(
    fingerprint: str,
    content: str = "Fixed and validated.",
    *,
    head: str = AFTER,
    round_number: int = 2,
    outcome: str = "fixed",
    occurrence: int = 1,
) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return (
        f"<!-- local-review-disposition:v3 engine=codex round={round_number} "
        f"head={head} fingerprint={fingerprint} occurrence={occurrence} outcome={outcome} "
        f"content-sha256={digest} -->\n{content}"
    )


def _threads_file(tmp_path: Path, threads: list[dict[str, Any]]) -> Path:
    path = tmp_path / "threads.json"
    path.write_text(
        json.dumps(
            [
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": threads,
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _heads_file(tmp_path: Path, *heads: str) -> Path:
    path = tmp_path / "heads.json"
    path.write_text(json.dumps(list(heads)), encoding="utf-8")
    return path


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


@pytest.fixture(autouse=True)
def authenticated_actor(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_ledger, "_current_actor", lambda: "reviewer")


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
            return json.dumps(_review_row(123, body_text))
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
            return json.dumps(_review_row(456, body_text))
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
            return json.dumps(_review_row(99, body_text))
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
            return json.dumps(_review_row(77, body_text))
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
        return json.dumps(
            {
                "data": {
                    "node": {
                        "id": "THREAD",
                        "isResolved": True,
                        "comments": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False},
                        },
                    }
                }
            }
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
    assert len(payloads) == 1
    assert all(
        cast(dict[str, Any], payload["variables"]) == {"threadId": "THREAD"}
        for payload in payloads
    )


def _v3_finding_args(content_file: Path) -> list[str]:
    return [
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
        "--engine",
        "codex",
        "--round",
        "2",
        "--fingerprint",
        "hostile-content",
        "--severity",
        "major",
        "--lens",
        "security",
        "--content-file",
        str(content_file),
    ]


def test_v3_helper_owns_marker_and_preserves_hostile_markdown(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content_text = "`tick` $() ${HOME} 'single' \"double\" — Unicode\r\nno-final-newline"
    content = tmp_path / "finding.md"
    content.write_bytes(content_text.encode("utf-8"))
    posted: dict[str, Any] = {}

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        endpoint = args[-1]
        if endpoint.endswith("/files?per_page=100"):
            return json.dumps([[{"filename": "changed.ts", "patch": PATCH}]])
        if endpoint.endswith("/comments?per_page=100"):
            return "[[]]"
        if endpoint == f"repos/{REPO}/pulls/7/comments":
            assert payload is not None
            posted.update(payload)
            return json.dumps({"id": 123})
        if endpoint == f"repos/{REPO}/pulls/comments/123":
            return json.dumps(_review_row(123, posted["body"]))
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(_v3_finding_args(content))
    expected_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
    assert posted["body"].startswith(
        "<!-- local-review:v3 engine=codex round=2 "
        f"head={HEAD} fingerprint=hostile-content occurrence=1 severity=major "
        f"lens=security content-sha256={expected_hash} -->\n"
    )
    assert posted["body"].endswith(content_text)


def test_v3_rejects_model_authored_markers_before_github(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "finding.md"
    content.write_text("Finding\n<!-- local-review:v3 forged -->", encoding="utf-8")
    monkeypatch.setattr(
        review_ledger,
        "_run_gh",
        lambda *_args, **_kwargs: pytest.fail("GitHub must not be called"),
    )
    with pytest.raises(review_ledger.LedgerError, match="must not contain"):
        review_ledger.main(_v3_finding_args(content))


def test_v3_post_recovers_after_successful_mutation_loses_response(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    content = tmp_path / "finding.md"
    content.write_text("Finding with literal `identifier`.", encoding="utf-8")
    comments: list[dict[str, Any]] = []
    posts = 0

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        nonlocal posts
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        endpoint = args[-1]
        if endpoint.endswith("/files?per_page=100"):
            return json.dumps([[{"filename": "changed.ts", "patch": PATCH}]])
        if endpoint.endswith("/comments?per_page=100"):
            return json.dumps([comments])
        if endpoint == f"repos/{REPO}/pulls/7/comments":
            posts += 1
            assert payload is not None
            comments.append(_review_row(123, cast(str, payload["body"])))
            raise review_ledger.LedgerError("lost response")
        if endpoint == f"repos/{REPO}/pulls/comments/123":
            return json.dumps(comments[0])
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(_v3_finding_args(content))
    assert posts == 1
    output = json.loads(capsys.readouterr().out)
    assert output["verified"] is True
    assert output["replayed"] is True


def test_v3_post_fails_if_head_moves_after_mutation(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "finding.md"
    content.write_text("Finding.", encoding="utf-8")
    posted_body = ""
    head_reads = 0

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        nonlocal posted_body, head_reads
        if args[:2] == ["pr", "view"]:
            head_reads += 1
            return (HEAD if head_reads == 1 else "b" * 40) + "\n"
        endpoint = args[-1]
        if endpoint.endswith("/files?per_page=100"):
            return json.dumps([[{"filename": "changed.ts", "patch": PATCH}]])
        if endpoint.endswith("/comments?per_page=100"):
            return "[[]]"
        if endpoint == f"repos/{REPO}/pulls/7/comments":
            assert payload is not None
            posted_body = cast(str, payload["body"])
            return json.dumps({"id": 123})
        if endpoint == f"repos/{REPO}/pulls/comments/123":
            return json.dumps(_review_row(123, posted_body))
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    with pytest.raises(review_ledger.LedgerError, match="PR head mismatch"):
        review_ledger.main(_v3_finding_args(content))
    assert head_reads == 2


def test_v3_dispose_reconciles_after_resolve_response_failure(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "disposition.md"
    content.write_text("Fixed in the current head and validated.", encoding="utf-8")
    comments: list[dict[str, Any]] = [
        _review_row(
            88,
            _finding_body("hostile-content", lens="security"),
        )
    ]
    resolved = False
    reply_posts = 0
    mutation_attempts = 0

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        nonlocal resolved, reply_posts, mutation_attempts
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        endpoint = args[-1]
        if endpoint.endswith("/comments?per_page=100"):
            return json.dumps([comments])
        if endpoint == f"repos/{REPO}/pulls/7/comments/88/replies":
            reply_posts += 1
            assert payload is not None
            comments.append(_review_row(99, cast(str, payload["body"])))
            return json.dumps({"id": 99})
        if endpoint == f"repos/{REPO}/pulls/comments/99":
            return json.dumps(next(row for row in comments if row["id"] == 99))
        if args == ["api", "graphql"]:
            assert payload is not None
            query = cast(str, payload["query"])
            if query.startswith("query"):
                return json.dumps(
                    {
                        "data": {
                            "node": {
                                "id": "THREAD",
                                "isResolved": resolved,
                                "comments": {
                                    "nodes": [{"databaseId": 88}],
                                    "pageInfo": {"hasNextPage": False},
                                },
                            }
                        }
                    }
                )
            mutation_attempts += 1
            resolved = True
            if mutation_attempts == 1:
                raise review_ledger.LedgerError("lost resolve response")
            return json.dumps(
                {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": "THREAD", "isResolved": True}
                        }
                    }
                }
            )
        raise AssertionError(args)

    args = [
        "dispose",
        "--repo",
        REPO,
        "--pr",
        "7",
        "--head",
        HEAD,
        "--engine",
        "codex",
        "--round",
        "2",
        "--fingerprint",
        "hostile-content",
        "--occurrence",
        "1",
        "--outcome",
        "fixed",
        "--comment-id",
        "88",
        "--thread-id",
        "THREAD",
        "--content-file",
        str(content),
    ]
    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(args)
    assert reply_posts == 1
    assert mutation_attempts == 1


def test_v3_dispose_rejects_mismatched_thread_before_reply(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "disposition.md"
    content.write_text("Fixed and validated.", encoding="utf-8")
    finding = _finding_body("mismatch")
    reply_posts = 0

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        nonlocal reply_posts
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        endpoint = args[-1]
        if endpoint.endswith("/comments?per_page=100"):
            return json.dumps([[_review_row(88, finding)]])
        if endpoint == f"repos/{REPO}/pulls/7/comments/88/replies":
            reply_posts += 1
            return json.dumps({"id": 99})
        if args == ["api", "graphql"]:
            return json.dumps(
                {
                    "data": {
                        "node": {
                            "id": "WRONG-THREAD",
                            "isResolved": False,
                            "comments": {
                                "nodes": [{"databaseId": 999}],
                                "pageInfo": {"hasNextPage": False},
                            },
                        }
                    }
                }
            )
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    with pytest.raises(review_ledger.LedgerError, match="comment-id"):
        review_ledger.main(
            [
                "dispose",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                HEAD,
                "--engine",
                "codex",
                "--round",
                "2",
                "--fingerprint",
                "mismatch",
                "--outcome",
                "fixed",
                "--comment-id",
                "88",
                "--thread-id",
                "WRONG-THREAD",
                "--content-file",
                str(content),
            ]
        )
    assert reply_posts == 0


def test_v3_reopen_occurrence_is_sequential_and_idempotent(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "recurrence.md"
    content.write_text("The same root cause recurred on this head.", encoding="utf-8")
    comments: list[dict[str, Any]] = [
        _review_row(
            88,
            _finding_body("repeat", "First occurrence.", round_number=1),
        ),
        _review_row(
            89,
            _disposition_body("repeat", round_number=1),
        ),
    ]
    resolved = True
    reply_posts = 0

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        nonlocal resolved, reply_posts
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        endpoint = args[-1]
        if endpoint.endswith("/comments?per_page=100"):
            return json.dumps([comments])
        if endpoint == f"repos/{REPO}/pulls/7/comments/88/replies":
            reply_posts += 1
            assert payload is not None
            comments.append(_review_row(99, cast(str, payload["body"])))
            return json.dumps({"id": 99})
        if endpoint == f"repos/{REPO}/pulls/comments/99":
            return json.dumps(next(row for row in comments if row["id"] == 99))
        if args == ["api", "graphql"]:
            assert payload is not None
            query = cast(str, payload["query"])
            if query.startswith("query"):
                return json.dumps(
                    {
                        "data": {
                            "node": {
                                "id": "THREAD",
                                "isResolved": resolved,
                                "comments": {
                                    "nodes": [{"databaseId": 88}],
                                    "pageInfo": {"hasNextPage": False},
                                },
                            }
                        }
                    }
                )
            assert "unresolveReviewThread" in query
            resolved = False
            return json.dumps(
                {
                    "data": {
                        "unresolveReviewThread": {
                            "thread": {"id": "THREAD", "isResolved": False}
                        }
                    }
                }
            )
        raise AssertionError(args)

    args = [
        "reopen-occurrence",
        "--repo",
        REPO,
        "--pr",
        "7",
        "--head",
        HEAD,
        "--engine",
        "codex",
        "--round",
        "2",
        "--fingerprint",
        "repeat",
        "--occurrence",
        "2",
        "--severity",
        "major",
        "--lens",
        "correctness",
        "--comment-id",
        "88",
        "--thread-id",
        "THREAD",
        "--content-file",
        str(content),
    ]
    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(args)
    review_ledger.main(args)
    assert reply_posts == 1
    assert resolved is False


def test_v3_reopen_rejects_an_undisposed_prior_occurrence(
    review_ledger: ModuleType,
) -> None:
    rows = [_review_row(88, _finding_body("repeat", round_number=1))]
    records = review_ledger._finding_records(rows, "repeat")
    with pytest.raises(review_ledger.LedgerError, match="prior finding occurrence"):
        review_ledger._require_prior_occurrences_disposed(rows, records, 2)


def test_v3_ignores_foreign_actor_fingerprint_roots(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "finding.md"
    content.write_text("Authenticated finding.", encoding="utf-8")
    foreign = _review_row(
        88,
        _finding_body("hostile-content", lens="security"),
        login="other-user",
    )
    posted: dict[str, Any] = {}

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        endpoint = args[-1]
        if endpoint.endswith("/files?per_page=100"):
            return json.dumps([[{"filename": "changed.ts", "patch": PATCH}]])
        if endpoint.endswith("/comments?per_page=100"):
            return json.dumps([[foreign]])
        if endpoint == f"repos/{REPO}/pulls/7/comments":
            assert payload is not None
            posted.update(payload)
            return json.dumps({"id": 123})
        if endpoint == f"repos/{REPO}/pulls/comments/123":
            return json.dumps(_review_row(123, cast(str, posted["body"])))
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(_v3_finding_args(content))
    assert posted


def test_v3_rejects_invalid_authenticated_content_hash(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forged = _review_row(
        88,
        _finding_body("hash-check").replace("Finding.", "Edited finding."),
    )

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        assert payload is None
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if args[-1].endswith("/comments?per_page=100"):
            return json.dumps([[forged]])
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    with pytest.raises(review_ledger.LedgerError, match="invalid content hash"):
        review_ledger.main(
            [
                "reconcile",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                HEAD,
                "--fingerprint",
                "hash-check",
            ]
        )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("existing_head", [HEAD, "b" * 40])
def test_v3_dispose_rejects_conflicting_stable_identity_before_mutation(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    existing_head: str,
) -> None:
    content = tmp_path / "disposition.md"
    content.write_text("Fixed and validated.", encoding="utf-8")
    rows = [
        _review_row(88, _finding_body("conflict")),
        _review_row(
            99,
            _disposition_body(
                "conflict",
                "Previously dismissed.",
                head=existing_head,
                outcome="dismissed",
            ),
        ),
    ]

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        assert payload is None
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if args[-1].endswith("/comments?per_page=100"):
            return json.dumps([rows])
        raise AssertionError("mutation must not run for a conflicting disposition")

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    with pytest.raises(review_ledger.LedgerError, match="conflicting content or outcome"):
        review_ledger.main(
            [
                "dispose",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                HEAD,
                "--engine",
                "codex",
                "--round",
                "2",
                "--fingerprint",
                "conflict",
                "--outcome",
                "fixed",
                "--comment-id",
                "88",
                "--thread-id",
                "THREAD",
                "--content-file",
                str(content),
            ]
        )


def test_verify_ledger_requires_complete_result_set_and_material_major_fix(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    threads = _threads_file(
        tmp_path,
        [
            {
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "body": _finding_body("major-fix", head="d" * 40),
                            "databaseId": 1,
                            "author": {"login": "reviewer"},
                        },
                        {
                            "body": _disposition_body("major-fix"),
                            "databaseId": 2,
                            "author": {"login": "reviewer"},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            },
            {
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "body": _finding_body(
                                "minor-deferred", severity="minor"
                            ),
                            "databaseId": 3,
                            "author": {"login": "reviewer"},
                        },
                        {
                            "body": _disposition_body(
                                "minor-deferred", outcome="deferred"
                            ),
                            "databaseId": 4,
                            "author": {"login": "reviewer"},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            },
        ],
    )
    result_file = tmp_path / "result.json"
    result = {
        "version": 3,
        "status": "changed",
        "engine": "codex",
        "round": 2,
        "baseSha": "c" * 40,
        "beforeSha": HEAD,
        "afterSha": AFTER,
        "classification": "material",
        "findingFingerprints": ["major-fix", "minor-deferred"],
        "finalLaneComplete": True,
    }
    result_file.write_text(json.dumps(result), encoding="utf-8")
    heads = _heads_file(tmp_path, HEAD, "d" * 40, AFTER)
    monkeypatch.setattr(
        review_ledger,
        "_run_gh",
        lambda args, payload=None: AFTER + "\n"
        if args[:2] == ["pr", "view"]
        else pytest.fail(str(args)),
    )
    args = [
        "verify-ledger",
        "--repo",
        REPO,
        "--pr",
        "7",
        "--head",
        AFTER,
        "--threads-file",
        str(threads),
        "--engine",
        "codex",
        "--round",
        "2",
        "--base",
        "c" * 40,
        "--before",
        HEAD,
        "--result-file",
        str(result_file),
        "--allowed-heads-file",
        str(heads),
    ]
    review_ledger.main(args)
    assert json.loads(capsys.readouterr().out)["threadsVerified"] == 2

    result["findingFingerprints"] = ["major-fix"]
    result_file.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(review_ledger.LedgerError, match="exactly match"):
        review_ledger.main(args)

    result["findingFingerprints"] = ["major-fix", "minor-deferred"]
    result["classification"] = "minor"
    result_file.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(review_ledger.LedgerError, match="material classification"):
        review_ledger.main(args)

    deferred_only = _threads_file(
        tmp_path,
        [
            {
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "body": _finding_body(
                                "minor-deferred", severity="minor"
                            ),
                            "databaseId": 3,
                            "author": {"login": "reviewer"},
                        },
                        {
                            "body": _disposition_body(
                                "minor-deferred", outcome="deferred"
                            ),
                            "databaseId": 4,
                            "author": {"login": "reviewer"},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        ],
    )
    assert deferred_only == threads
    result["findingFingerprints"] = ["minor-deferred"]
    result["classification"] = "minor"
    result_file.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(review_ledger.LedgerError, match="at least one fixed"):
        review_ledger.main(args)


def test_verify_ledger_rejects_deferred_blockers_and_earlier_undisposed_findings(
    review_ledger: ModuleType,
) -> None:
    deferred_blocker = {
        "isResolved": True,
        "comments": {
            "nodes": [
                {
                    "body": _finding_body("blocker", severity="blocking"),
                    "author": {"login": "reviewer"},
                },
                {
                    "body": _disposition_body("blocker", outcome="deferred"),
                    "author": {"login": "reviewer"},
                },
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }
    with pytest.raises(review_ledger.LedgerError, match="must be fixed"):
        review_ledger._verify_thread_dispositions([deferred_blocker])

    recurrence = {
        "isResolved": True,
        "comments": {
            "nodes": [
                {
                    "body": _finding_body("repeat", round_number=1),
                    "author": {"login": "reviewer"},
                },
                {
                    "body": _finding_body("repeat", occurrence=2),
                    "author": {"login": "reviewer"},
                },
                {
                    "body": _disposition_body("repeat", occurrence=2),
                    "author": {"login": "reviewer"},
                },
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }
    with pytest.raises(review_ledger.LedgerError, match="exactly one"):
        review_ledger._verify_thread_dispositions([recurrence])


def test_validate_result_enforces_observed_transition(
    review_ledger: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    after = "b" * 40
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(
            {
                "version": 3,
                "status": "changed",
                "engine": "claude",
                "round": 2,
                "baseSha": "c" * 40,
                "beforeSha": HEAD,
                "afterSha": after,
                "classification": "material",
                "findingFingerprints": ["finding-1"],
                "finalLaneComplete": True,
            }
        ),
        encoding="utf-8",
    )
    assert (
        review_ledger.main(
            [
                "validate-result",
                "--engine",
                "claude",
                "--round",
                "2",
                "--base",
                "c" * 40,
                "--before",
                HEAD,
                "--head",
                after,
                "--result-file",
                str(result_file),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "changed"
    data = json.loads(result_file.read_text(encoding="utf-8"))
    data["afterSha"] = HEAD
    result_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(review_ledger.LedgerError, match="afterSha mismatch"):
        review_ledger.main(
            [
                "validate-result",
                "--engine",
                "claude",
                "--round",
                "2",
                "--base",
                "c" * 40,
                "--before",
                HEAD,
                "--head",
                after,
                "--result-file",
                str(result_file),
            ]
        )

    data["afterSha"] = after
    data["round"] = True
    result_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(review_ledger.LedgerError, match="must be integers"):
        review_ledger.main(
            [
                "validate-result",
                "--engine",
                "claude",
                "--round",
                "1",
                "--base",
                "c" * 40,
                "--before",
                HEAD,
                "--head",
                after,
                "--result-file",
                str(result_file),
            ]
        )


def test_attest_rejects_changed_results_without_ledger_evidence(
    review_ledger: ModuleType,
    tmp_path: Path,
) -> None:
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(
            {
                "version": 3,
                "status": "changed",
                "engine": "codex",
                "round": 2,
                "baseSha": "c" * 40,
                "beforeSha": HEAD,
                "afterSha": AFTER,
                "classification": "material",
                "findingFingerprints": ["missing"],
                "finalLaneComplete": True,
            }
        ),
        encoding="utf-8",
    )
    threads = _threads_file(tmp_path, [])
    heads = _heads_file(tmp_path, HEAD, AFTER)
    with pytest.raises(review_ledger.LedgerError, match="exactly match"):
        review_ledger.main(
            [
                "attest",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                AFTER,
                "--engine",
                "codex",
                "--round",
                "2",
                "--base",
                "c" * 40,
                "--before",
                HEAD,
                "--result-file",
                str(result_file),
                "--threads-file",
                str(threads),
                "--allowed-heads-file",
                str(heads),
                "--actor",
                "reviewer",
                "--expected-result-sha256",
                hashlib.sha256(result_file.read_bytes()).hexdigest(),
            ]
        )


def test_attest_rejects_clean_result_with_same_round_fix(
    review_ledger: ModuleType,
    tmp_path: Path,
) -> None:
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(
            {
                "version": 3,
                "status": "clean",
                "engine": "codex",
                "round": 2,
                "baseSha": "c" * 40,
                "beforeSha": HEAD,
                "afterSha": HEAD,
                "classification": None,
                "findingFingerprints": [],
                "finalLaneComplete": True,
            }
        ),
        encoding="utf-8",
    )
    threads = _threads_file(
        tmp_path,
        [
            {
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "body": _finding_body(
                                "same-head-blocker", severity="blocking"
                            ),
                            "author": {"login": "reviewer"},
                        },
                        {
                            "body": _disposition_body("same-head-blocker", head=HEAD),
                            "author": {"login": "reviewer"},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        ],
    )
    heads = _heads_file(tmp_path, HEAD)
    with pytest.raises(review_ledger.LedgerError, match="cannot have same-round fixes"):
        review_ledger.main(
            [
                "attest",
                "--repo",
                REPO,
                "--pr",
                "7",
                "--head",
                HEAD,
                "--engine",
                "codex",
                "--round",
                "2",
                "--base",
                "c" * 40,
                "--before",
                HEAD,
                "--result-file",
                str(result_file),
                "--threads-file",
                str(threads),
                "--allowed-heads-file",
                str(heads),
                "--actor",
                "reviewer",
                "--expected-result-sha256",
                hashlib.sha256(result_file.read_bytes()).hexdigest(),
            ]
        )


def test_clean_result_allows_same_round_minor_deferral(
    review_ledger: ModuleType,
    tmp_path: Path,
) -> None:
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(
            {
                "version": 3,
                "status": "clean",
                "engine": "codex",
                "round": 2,
                "baseSha": "c" * 40,
                "beforeSha": HEAD,
                "afterSha": HEAD,
                "classification": None,
                "findingFingerprints": [],
                "finalLaneComplete": True,
            }
        ),
        encoding="utf-8",
    )
    threads = _threads_file(
        tmp_path,
        [
            {
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "body": _finding_body(
                                "minor-deferral", severity="minor"
                            ),
                            "author": {"login": "reviewer"},
                        },
                        {
                            "body": _disposition_body(
                                "minor-deferral", head=HEAD, outcome="deferred"
                            ),
                            "author": {"login": "reviewer"},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        ],
    )
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert review_ledger._verify_result_evidence(
        SimpleNamespace(engine="codex", round=2),
        review_ledger._load_review_threads(threads),
        data=data,
    )
