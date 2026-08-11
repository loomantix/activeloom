"""Regression tests for deterministic local-review ledger mutations."""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest


HEAD = "a" * 40
REPO = "example/repository"
ACTOR = "reviewer"
PATCH = """@@ -10,3 +10,4 @@
 context
-old
+new
+more
 context
"""


def _v3_finding_body(
    *,
    head: str = HEAD,
    engine: str = "codex",
    round_number: int = 2,
    fingerprint: str = "finding",
    occurrence: int = 1,
    severity: str = "major",
    content: str = "Finding.",
) -> str:
    digest = hashlib.sha256(content.encode()).hexdigest()
    return (
        f"<!-- local-review:v3 engine={engine} round={round_number} head={head} "
        f"fingerprint={fingerprint} occurrence={occurrence} severity={severity} "
        f"lens=correctness content-sha256={digest} -->\n{content}"
    )


def _v3_disposition_body(
    *,
    head: str = HEAD,
    engine: str = "codex",
    round_number: int = 2,
    fingerprint: str = "finding",
    occurrence: int = 1,
    outcome: str = "fixed",
    content: str = "Fixed and validated.",
) -> str:
    digest = hashlib.sha256(content.encode()).hexdigest()
    return (
        f"<!-- local-review-disposition:v3 engine={engine} round={round_number} "
        f"head={head} fingerprint={fingerprint} occurrence={occurrence} "
        f"outcome={outcome} content-sha256={digest} -->\n{content}"
    )


def _pseudo_v3_thread(
    *, resolved: bool = True, reply: bool = True, body: str | None = None
) -> dict[str, Any]:
    marker_body = body or (
        "Historical finding.\n\n"
        "<!-- local-review:v3 engine=claude fingerprint=historical-finding -->"
    )
    nodes = [
        {"databaseId": 1, "body": marker_body, "author": {"login": ACTOR}}
    ]
    if reply:
        nodes.append(
            {
                "databaseId": 2,
                "body": "Fixed before contract v3 was finalized.",
                "author": {"login": ACTOR},
            }
        )
    return {
        "id": "PSEUDO-THREAD",
        "isResolved": resolved,
        "repository": {"nameWithOwner": REPO},
        "pullRequest": {"number": 7},
        "comments": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": False},
        },
    }


def _v1_thread(*, resolved: bool = True, disposition: bool = True) -> dict[str, Any]:
    nodes = [
        {
            "databaseId": 10,
            "body": "<!-- local-review:v1 engine=codex round=1 "
            f"head={HEAD} fingerprint=legacy -->\nLegacy finding.",
            "author": {"login": ACTOR},
        }
    ]
    if disposition:
        nodes.append(
            {
                "databaseId": 11,
                "body": "<!-- local-review-disposition:v1 engine=codex round=1 "
                f"head={HEAD} fingerprint=legacy outcome=fixed -->\nFixed.",
                "author": {"login": ACTOR},
            }
        )
    return {
        "id": "V1-THREAD",
        "isResolved": resolved,
        "repository": {"nameWithOwner": REPO},
        "pullRequest": {"number": 7},
        "comments": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": False},
        },
    }


def _unowned_pseudo_v3_thread() -> dict[str, Any]:
    thread = _pseudo_v3_thread()
    del thread["comments"]["nodes"][0]["author"]
    return thread


@pytest.fixture(scope="session")
def review_ledger() -> ModuleType:
    path = (
        Path(__file__).resolve().parent.parent
        / ".claude"
        / "skills"
        / "critique"
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
    monkeypatch.setattr(review_ledger, "_current_login", lambda: ACTOR)


def test_settled_pseudo_v3_and_v1_history_are_not_current_evidence(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    deferred = _pseudo_v3_thread(
        body="Historical finding.\n\n"
        "<!-- local-review:v3 engine=claude fingerprint=historical-deferred outcome=deferred -->"
    )
    threads = [_pseudo_v3_thread(), deferred, _v1_thread()]
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: threads)
    assert review_ledger._verify_complete_v3_threads(REPO, 7, ACTOR) == []


def test_settled_history_is_ignored_by_write_result_and_attestation_evidence(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    threads = [_pseudo_v3_thread(), _v1_thread()]
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: threads)
    monkeypatch.setattr(review_ledger, "_verify_review_base", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_verify_git_transition", lambda *_args: None)
    result_file = tmp_path / "result.json"
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        base="c" * 40,
        before=HEAD,
        head=HEAD,
        engine="claude",
        round=1,
        result_file=str(result_file),
        classification=None,
    )
    review_ledger._write_result(args)
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["findingFingerprints"] == []
    review_ledger._verify_result_evidence(args, data, ACTOR)


def test_write_result_ignores_settled_same_round_history_outside_transition(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    after = "b" * 40
    old_head = "d" * 40
    old_finding = review_ledger.FINDING_V3_RE.search(
        _v3_finding_body(head=old_head, round_number=1, fingerprint="reused")
    )
    old_disposition = review_ledger.DISPOSITION_V3_RE.search(
        _v3_disposition_body(head=old_head, round_number=1, fingerprint="reused")
    )
    current_finding = review_ledger.FINDING_V3_RE.search(
        _v3_finding_body(head=HEAD, round_number=1, fingerprint="current")
    )
    current_disposition = review_ledger.DISPOSITION_V3_RE.search(
        _v3_disposition_body(head=after, round_number=1, fingerprint="current")
    )
    assert all(
        marker is not None
        for marker in (old_finding, old_disposition, current_finding, current_disposition)
    )
    monkeypatch.setattr(review_ledger, "_verify_review_base", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_verify_git_transition", lambda *_args: None)
    monkeypatch.setattr(
        review_ledger,
        "_verify_complete_v3_threads",
        lambda *_args: [
            (old_finding, old_disposition),
            (current_finding, current_disposition),
        ],
    )
    monkeypatch.setattr(
        review_ledger, "_load_allowed_heads", lambda _args: {HEAD: 0, after: 1}
    )
    result_file = tmp_path / "result.json"
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        base="c" * 40,
        before=HEAD,
        head=after,
        engine="codex",
        round=1,
        result_file=str(result_file),
        allowed_heads_file="heads.json",
        classification="material",
    )
    review_ledger._write_result(args)
    assert json.loads(result_file.read_text(encoding="utf-8"))[
        "findingFingerprints"
    ] == ["current"]


def test_write_result_aggregates_sequential_same_fingerprint_recurrences(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    middle = "b" * 40
    after = "c" * 40
    pairs = []
    for occurrence, finding_head, disposition_head in (
        (1, HEAD, middle),
        (2, middle, after),
    ):
        finding = review_ledger.FINDING_V3_RE.search(
            _v3_finding_body(
                head=finding_head,
                round_number=1,
                fingerprint="recurred",
                occurrence=occurrence,
            )
        )
        disposition = review_ledger.DISPOSITION_V3_RE.search(
            _v3_disposition_body(
                head=disposition_head,
                round_number=1,
                fingerprint="recurred",
                occurrence=occurrence,
            )
        )
        assert finding is not None and disposition is not None
        pairs.append((finding, disposition))
    monkeypatch.setattr(review_ledger, "_verify_review_base", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_verify_git_transition", lambda *_args: None)
    monkeypatch.setattr(
        review_ledger, "_verify_complete_v3_threads", lambda *_args: pairs
    )
    monkeypatch.setattr(
        review_ledger,
        "_load_allowed_heads",
        lambda _args: {HEAD: 0, middle: 1, after: 2},
    )
    result_file = tmp_path / "result.json"
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        base="d" * 40,
        before=HEAD,
        head=after,
        engine="codex",
        round=1,
        result_file=str(result_file),
        allowed_heads_file="heads.json",
        classification="material",
    )
    review_ledger._write_result(args)
    assert json.loads(result_file.read_text(encoding="utf-8"))[
        "findingFingerprints"
    ] == ["recurred"]


def test_write_result_rejects_changed_transition_without_ledger_evidence(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    after = "b" * 40
    monkeypatch.setattr(review_ledger, "_verify_review_base", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_verify_git_transition", lambda *_args: None)
    monkeypatch.setattr(
        review_ledger, "_verify_complete_v3_threads", lambda *_args: []
    )
    monkeypatch.setattr(
        review_ledger, "_load_allowed_heads", lambda _args: {HEAD: 0, after: 1}
    )
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        base="c" * 40,
        before=HEAD,
        head=after,
        engine="codex",
        round=1,
        result_file=str(tmp_path / "result.json"),
        allowed_heads_file="heads.json",
        classification="minor",
    )
    with pytest.raises(review_ledger.LedgerError, match="require ledger evidence"):
        review_ledger._write_result(args)


@pytest.mark.parametrize(
    ("thread", "message"),
    [
        (_pseudo_v3_thread(resolved=False), "not settled"),
        (_pseudo_v3_thread(reply=False), "not settled"),
        (
            _pseudo_v3_thread(
                body="Historical finding.\n\n<!-- local-review:v3 engine=claude fingerprint=bad extra=yes -->"
            ),
            "malformed or unsupported",
        ),
        (
            _pseudo_v3_thread(
                body=_v3_finding_body().replace("Finding.", "Edited finding.")
            ),
            "content hash mismatch",
        ),
        (_v1_thread(resolved=False), "unresolved"),
        (_v1_thread(disposition=False), "matching disposition"),
        (_unowned_pseudo_v3_thread(), "ownership"),
    ],
)
def test_historical_review_records_fail_closed(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    thread: dict[str, Any],
    message: str,
) -> None:
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: [thread])
    with pytest.raises(review_ledger.LedgerError, match=message):
        review_ledger._verify_complete_v3_threads(REPO, 7, ACTOR)


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
        if args == ["api", "user"]:
            return json.dumps({"login": ACTOR})
        assert args == ["api", "graphql"]
        assert payload is not None
        payloads.append(payload)
        query = cast(str, payload["query"])
        if "resolveReviewThread" in query:
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
            {
                "data": {
                    "node": {
                        "id": "THREAD",
                        "isResolved": len(payloads) > 1,
                        "repository": {"nameWithOwner": REPO},
                        "pullRequest": {"number": 7},
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 88,
                                    "body": "<!-- local-review:v1 engine=codex round=1 head="
                                    + HEAD
                                    + " fingerprint=legacy -->\nFinding.",
                                    "author": {"login": ACTOR},
                                }
                            ],
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
    assert len(payloads) == 3
    assert all(
        cast(dict[str, Any], payload["variables"]) == {"threadId": "THREAD"}
        for payload in payloads
    )


def test_resolve_rejects_same_pr_human_thread(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutations = 0

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        nonlocal mutations
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        if args == ["api", "user"]:
            return json.dumps({"login": ACTOR})
        assert args == ["api", "graphql"] and payload is not None
        query = cast(str, payload["query"])
        if "mutation" in query:
            mutations += 1
            pytest.fail("human thread must not be mutated")
        return json.dumps(
            {
                "data": {
                    "node": {
                        "id": "THREAD",
                        "isResolved": False,
                        "repository": {"nameWithOwner": REPO},
                        "pullRequest": {"number": 7},
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 88,
                                    "body": "Human review finding.",
                                    "author": {"login": ACTOR},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        },
                    }
                }
            }
        )

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    with pytest.raises(review_ledger.LedgerError, match="actor-owned v1"):
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
    assert mutations == 0


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
    content_text = (
        "`tick` $() ${HOME} 'single' \"double\" — Unicode\r\nno-final-newline"
    )
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
            return json.dumps(
                {"id": 123, "body": posted["body"], "user": {"login": ACTOR}}
            )
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


def test_v3_post_finding_proceeds_past_settled_history(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "finding.md"
    content.write_text("New current finding.", encoding="utf-8")
    pseudo_thread = _pseudo_v3_thread()
    pseudo_body = cast(str, pseudo_thread["comments"]["nodes"][0]["body"])
    legacy_body = cast(str, _v1_thread()["comments"]["nodes"][0]["body"])
    rows = [
        {"id": 1, "body": pseudo_body, "user": {"login": ACTOR}},
        {"id": 10, "body": legacy_body, "user": {"login": ACTOR}},
    ]
    posted: dict[str, Any] = {}

    def fake_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
        if args[:2] == ["pr", "view"]:
            return HEAD + "\n"
        endpoint = args[-1]
        if endpoint.endswith("/files?per_page=100"):
            return json.dumps([[{"filename": "changed.ts", "patch": PATCH}]])
        if endpoint.endswith("/comments?per_page=100"):
            return json.dumps([rows])
        if endpoint == f"repos/{REPO}/pulls/7/comments":
            assert payload is not None
            posted.update(payload)
            return json.dumps({"id": 123})
        if endpoint == f"repos/{REPO}/pulls/comments/123":
            return json.dumps(
                {"id": 123, "body": posted["body"], "user": {"login": ACTOR}}
            )
        raise AssertionError(args)

    monkeypatch.setattr(
        review_ledger,
        "_review_threads",
        lambda *_args: [pseudo_thread, _v1_thread()],
    )
    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(_v3_finding_args(content))
    assert "fingerprint=hostile-content" in cast(str, posted["body"])


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
            comments.append(
                {"id": 123, "body": payload["body"], "user": {"login": ACTOR}}
            )
            raise review_ledger.LedgerError("lost response")
        if endpoint == f"repos/{REPO}/pulls/comments/123":
            return json.dumps(comments[0])
        raise AssertionError(args)

    monkeypatch.setattr(review_ledger, "_run_gh", fake_gh)
    review_ledger.main(_v3_finding_args(content))
    assert posts == 1
    assert json.loads(capsys.readouterr().out)["verified"] is True


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
            return json.dumps(
                {"id": 123, "body": posted_body, "user": {"login": ACTOR}}
            )
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
        {
            "id": 88,
            "user": {"login": ACTOR},
            "body": (
                "<!-- local-review:v3 engine=codex round=2 "
                f"head={HEAD} fingerprint=hostile-content occurrence=1 "
                "severity=major lens=security content-sha256="
                f"{'d' * 64} -->\nFinding."
            ),
        }
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
            comments.append(
                {"id": 99, "body": payload["body"], "user": {"login": ACTOR}}
            )
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
                                "repository": {"nameWithOwner": REPO},
                                "pullRequest": {"number": 7},
                                "comments": {
                                    "nodes": [
                                        {"databaseId": row["id"]} for row in comments
                                    ],
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


def test_v3_reopen_occurrence_is_sequential_and_idempotent(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "recurrence.md"
    content.write_text("The same root cause recurred on this head.", encoding="utf-8")
    comments: list[dict[str, Any]] = [
        {
            "id": 88,
            "user": {"login": ACTOR},
            "body": (
                "<!-- local-review:v3 engine=codex round=1 "
                f"head={HEAD} fingerprint=repeat occurrence=1 severity=major "
                f"lens=correctness content-sha256={'a' * 64} -->\nFirst occurrence."
            ),
        }
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
            comments.append(
                {"id": 99, "body": payload["body"], "user": {"login": ACTOR}}
            )
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
                                "repository": {"nameWithOwner": REPO},
                                "pullRequest": {"number": 7},
                                "comments": {
                                    "nodes": [
                                        {"databaseId": row["id"]} for row in comments
                                    ],
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


def test_dispose_rejects_a_root_comment_from_another_fingerprint(
    review_ledger: ModuleType, tmp_path: Path
) -> None:
    content = tmp_path / "disposition.md"
    content.write_text("Fixed and validated.", encoding="utf-8")
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        head=HEAD,
        engine="codex",
        round=2,
        fingerprint="finding-a",
        occurrence=1,
        outcome="fixed",
        comment_id=99,
        thread_id="THREAD-B",
        content_file=str(content),
    )
    rows = [
        {
            "id": 88,
            "user": {"login": ACTOR},
            "body": (
                "<!-- local-review:v3 engine=codex round=2 "
                f"head={HEAD} fingerprint=finding-a occurrence=1 severity=major "
                f"lens=correctness content-sha256={'a' * 64} -->\nFinding."
            ),
        }
    ]
    with pytest.raises(review_ledger.LedgerError, match="fingerprint root"):
        review_ledger._require_finding_occurrence(rows, args)


def test_conflicting_disposition_is_rejected_before_thread_mutation(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "disposition.md"
    content.write_text("New explanation.", encoding="utf-8")
    finding = (
        "<!-- local-review:v3 engine=codex round=2 "
        f"head={HEAD} fingerprint=conflict occurrence=1 severity=major "
        f"lens=correctness content-sha256={'a' * 64} -->\nFinding."
    )
    prior = (
        "<!-- local-review-disposition:v3 engine=codex round=2 "
        f"head={HEAD} fingerprint=conflict occurrence=1 outcome=deferred "
        f"content-sha256={'b' * 64} -->\nPrior disposition."
    )
    rows = [
        {"id": 88, "user": {"login": ACTOR}, "body": finding},
        {
            "id": 89,
            "in_reply_to_id": 88,
            "user": {"login": ACTOR},
            "body": prior,
        },
    ]
    monkeypatch.setattr(review_ledger, "_verify_head", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_review_comments", lambda *_args: rows)
    monkeypatch.setattr(
        review_ledger,
        "_thread_state",
        lambda *_args, **_kwargs: pytest.fail("thread must not be touched"),
    )
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        head=HEAD,
        engine="codex",
        round=2,
        fingerprint="conflict",
        occurrence=1,
        outcome="fixed",
        comment_id=88,
        thread_id="THREAD",
        content_file=str(content),
    )
    with pytest.raises(review_ledger.LedgerError, match="conflicting disposition"):
        review_ledger._dispose(args)


def test_dispose_resumes_landed_reply_on_a_descendant_head(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disposition_head = "b" * 40
    current_head = "c" * 40
    content_text = "Fixed and validated."
    content = tmp_path / "disposition.md"
    content.write_text(content_text, encoding="utf-8")
    prior_body = _v3_disposition_body(
        head=disposition_head,
        fingerprint="resumable",
        content=content_text,
    )
    rows = [
        {
            "id": 88,
            "user": {"login": ACTOR},
            "body": _v3_finding_body(head=HEAD, fingerprint="resumable"),
        },
        {"id": 89, "user": {"login": ACTOR}, "body": prior_body},
    ]
    monkeypatch.setattr(review_ledger, "_verify_head", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_review_comments", lambda *_args: rows)
    monkeypatch.setattr(
        review_ledger,
        "_is_ancestor",
        lambda ancestor, descendant: (ancestor, descendant)
        in {(HEAD, disposition_head), (disposition_head, current_head)},
    )
    monkeypatch.setattr(review_ledger, "_thread_state", lambda *_args: False)
    monkeypatch.setattr(review_ledger, "_set_thread_state", lambda *_args: False)
    monkeypatch.setattr(
        review_ledger,
        "_post_review_comment",
        lambda *_args, **_kwargs: pytest.fail("landed disposition must be reused"),
    )
    monkeypatch.setattr(
        review_ledger,
        "_verify_comment",
        lambda _repo, comment_id, body, actor: (
            comment_id == 89 and body == prior_body and actor == ACTOR
        )
        or pytest.fail("unexpected disposition readback"),
    )
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        head=current_head,
        engine="codex",
        round=2,
        fingerprint="resumable",
        occurrence=1,
        outcome="fixed",
        comment_id=88,
        thread_id="THREAD",
        content_file=str(content),
    )

    review_ledger._dispose(args)

    output = json.loads(capsys.readouterr().out)
    assert output["comment_id"] == 89
    assert output["replayed"] is True
    assert output["resolved"] is True


def test_dispose_rejects_landed_reply_from_an_unrelated_head(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content_text = "Fixed and validated."
    content = tmp_path / "disposition.md"
    content.write_text(content_text, encoding="utf-8")
    rows = [
        {
            "id": 88,
            "user": {"login": ACTOR},
            "body": _v3_finding_body(head=HEAD, fingerprint="unrelated"),
        },
        {
            "id": 89,
            "user": {"login": ACTOR},
            "body": _v3_disposition_body(
                head="b" * 40,
                fingerprint="unrelated",
                content=content_text,
            ),
        },
    ]
    monkeypatch.setattr(review_ledger, "_verify_head", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_review_comments", lambda *_args: rows)
    monkeypatch.setattr(review_ledger, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        review_ledger,
        "_thread_state",
        lambda *_args, **_kwargs: pytest.fail("thread must not be touched"),
    )
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        head="c" * 40,
        engine="codex",
        round=2,
        fingerprint="unrelated",
        occurrence=1,
        outcome="fixed",
        comment_id=88,
        thread_id="THREAD",
        content_file=str(content),
    )

    with pytest.raises(review_ledger.LedgerError, match="conflicting disposition"):
        review_ledger._dispose(args)


def test_dispose_rejects_blocking_deferral_before_thread_mutation(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "disposition.md"
    content.write_text("Deferred to #123.", encoding="utf-8")
    rows = [
        {
            "id": 88,
            "user": {"login": ACTOR},
            "body": _v3_finding_body(
                fingerprint="blocker", severity="blocking"
            ),
        }
    ]
    monkeypatch.setattr(review_ledger, "_verify_head", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_review_comments", lambda *_args: rows)
    monkeypatch.setattr(
        review_ledger,
        "_thread_state",
        lambda *_args, **_kwargs: pytest.fail("thread must not be touched"),
    )
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        head=HEAD,
        engine="codex",
        round=2,
        fingerprint="blocker",
        occurrence=1,
        outcome="deferred",
        comment_id=88,
        thread_id="THREAD",
        content_file=str(content),
    )

    with pytest.raises(review_ledger.LedgerError, match="cannot be deferred"):
        review_ledger._dispose(args)


def test_reconcile_does_not_cross_engine_or_round_identity(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        {
            "id": 88,
            "user": {"login": ACTOR},
            "body": (
                "<!-- local-review:v3 engine=codex round=1 "
                f"head={HEAD} fingerprint=repeat occurrence=1 severity=major "
                f"lens=correctness content-sha256={'a' * 64} -->\nFinding."
            ),
        },
        {
            "id": 89,
            "user": {"login": ACTOR},
            "body": (
                "<!-- local-review-disposition:v3 engine=claude round=4 "
                f"head={HEAD} fingerprint=repeat occurrence=1 outcome=fixed "
                f"content-sha256={'b' * 64} -->\nWrong pass."
            ),
        },
    ]
    monkeypatch.setattr(review_ledger, "_verify_head", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_review_comments", lambda *_args: rows)
    args = argparse.Namespace(repo=REPO, pr=7, head=HEAD, fingerprint="repeat")
    review_ledger._reconcile(args)
    output = json.loads(capsys.readouterr().out)
    assert output["ledgerValid"] is False
    assert output["nextAction"] == "repair-sequence"


def test_complete_ledger_rejects_unstructured_same_actor_reply(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    finding_content = "Finding."
    finding = (
        "<!-- local-review:v3 engine=codex round=1 "
        f"head={HEAD} fingerprint=missing-disposition occurrence=1 severity=major "
        "lens=tests content-sha256="
        f"{hashlib.sha256(finding_content.encode()).hexdigest()} -->\n{finding_content}"
    )
    threads = [
        {
            "id": "THREAD",
            "isResolved": True,
            "repository": {"nameWithOwner": REPO},
            "pullRequest": {"number": 7},
            "comments": {
                "nodes": [
                    {"databaseId": 88, "body": finding, "author": {"login": ACTOR}},
                    {
                        "databaseId": 89,
                        "body": "Looking into this.",
                        "author": {"login": ACTOR},
                    },
                ],
                "pageInfo": {"hasNextPage": False},
            },
        }
    ]
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: threads)
    with pytest.raises(review_ledger.LedgerError, match="matching disposition"):
        review_ledger._verify_complete_v3_threads(REPO, 7, ACTOR)


@pytest.mark.parametrize("case", ["missing-root", "gap", "split-root", "reply-root"])
def test_complete_ledger_rejects_invalid_fingerprint_topology(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    def thread(
        thread_id: str, occurrences: list[int], *, leading_comment: bool = False
    ) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        if leading_comment:
            nodes.append(
                {
                    "databaseId": 1,
                    "body": "Unrelated root.",
                    "author": {"login": "someone-else"},
                }
            )
        for occurrence in occurrences:
            nodes.extend(
                [
                    {
                        "databaseId": 10 + occurrence,
                        "body": _v3_finding_body(
                            fingerprint="topology", occurrence=occurrence
                        ),
                        "author": {"login": ACTOR},
                    },
                    {
                        "databaseId": 20 + occurrence,
                        "body": _v3_disposition_body(
                            fingerprint="topology", occurrence=occurrence
                        ),
                        "author": {"login": ACTOR},
                    },
                ]
            )
        return {
            "id": thread_id,
            "isResolved": True,
            "repository": {"nameWithOwner": REPO},
            "pullRequest": {"number": 7},
            "comments": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False},
            },
        }

    if case == "missing-root":
        threads = [thread("THREAD", [2])]
    elif case == "gap":
        threads = [thread("THREAD", [1, 3])]
    elif case == "split-root":
        threads = [thread("THREAD-A", [1]), thread("THREAD-B", [1])]
    else:
        threads = [thread("THREAD", [1], leading_comment=True)]
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: threads)
    with pytest.raises(review_ledger.LedgerError, match="fingerprint topology"):
        review_ledger._verify_complete_v3_threads(REPO, 7, ACTOR)


def test_changed_minor_result_rejects_empty_fingerprint_set(
    review_ledger: ModuleType, tmp_path: Path
) -> None:
    after = "b" * 40
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(
            {
                "version": 3,
                "status": "changed",
                "engine": "codex",
                "round": 1,
                "baseSha": "c" * 40,
                "beforeSha": HEAD,
                "afterSha": after,
                "classification": "minor",
                "findingFingerprints": [],
                "finalLaneComplete": True,
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        result_file=str(result_file),
        engine="codex",
        round=1,
        base="c" * 40,
        before=HEAD,
        head=after,
    )
    with pytest.raises(
        review_ledger.LedgerError,
        match="changed review result conflicts with the observed pass",
    ):
        review_ledger._validate_result_data(args)


def test_convergence_round_rejects_minor_changed_result(
    review_ledger: ModuleType, tmp_path: Path
) -> None:
    after = "b" * 40
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(
            {
                "version": 3,
                "status": "changed",
                "engine": "codex",
                "round": 3,
                "baseSha": "c" * 40,
                "beforeSha": HEAD,
                "afterSha": after,
                "classification": "minor",
                "findingFingerprints": ["blocking-fix"],
                "finalLaneComplete": True,
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        result_file=str(result_file),
        engine="codex",
        round=3,
        base="c" * 40,
        before=HEAD,
        head=after,
    )
    with pytest.raises(review_ledger.LedgerError, match="changed review result"):
        review_ledger._validate_result_data(args)


def test_actor_scoped_records_ignore_foreign_markers(review_ledger: ModuleType) -> None:
    rows = [
        {"id": 1, "user": {"login": "someone-else"}, "body": "marker"},
        {"id": 2, "user": {"login": ACTOR}, "body": "local marker"},
    ]
    assert review_ledger._actor_rows(rows, ACTOR) == [rows[1]]


def test_attestation_rejects_result_fingerprints_without_ledger_evidence(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_ledger, "_verify_review_base", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_verify_git_transition", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_verify_complete_v3_threads", lambda *_args: [])
    monkeypatch.setattr(
        review_ledger,
        "_load_allowed_heads",
        lambda _args: {HEAD: 0, "b" * 40: 1},
    )
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        head="b" * 40,
        before=HEAD,
        base="c" * 40,
        engine="codex",
        round=1,
        allowed_heads_file="heads.json",
    )
    data = {
        "status": "changed",
        "classification": "material",
        "findingFingerprints": ["missing"],
    }
    with pytest.raises(
        review_ledger.LedgerError, match="complete same-round disposition set"
    ):
        review_ledger._verify_result_evidence(args, data, ACTOR)


def test_complete_ledger_rejects_tampered_malformed_and_orphan_records(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_thread = {
        "id": "THREAD",
        "isResolved": True,
        "repository": {"nameWithOwner": REPO},
        "pullRequest": {"number": 7},
        "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
    }

    tampered = dict(base_thread)
    tampered["comments"] = {
        "nodes": [
            {
                "databaseId": 1,
                "body": _v3_finding_body() + " edited",
                "author": {"login": ACTOR},
            }
        ],
        "pageInfo": {"hasNextPage": False},
    }
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: [tampered])
    with pytest.raises(review_ledger.LedgerError, match="content hash mismatch"):
        review_ledger._verify_complete_v3_threads(REPO, 7, ACTOR)

    malformed = dict(base_thread)
    malformed["comments"] = {
        "nodes": [
            {
                "databaseId": 1,
                "body": "<!-- local-review:v3 malformed -->\nFinding.",
                "author": {"login": ACTOR},
            }
        ],
        "pageInfo": {"hasNextPage": False},
    }
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: [malformed])
    with pytest.raises(review_ledger.LedgerError, match="marker is malformed"):
        review_ledger._verify_complete_v3_threads(REPO, 7, ACTOR)

    for tampered_body in (
        _v3_finding_body().replace("<!-- local-review", "<!--  local-review", 1),
        _v3_finding_body().replace(":v3", ":v4", 1),
    ):
        malformed["comments"] = {
            "nodes": [
                {
                    "databaseId": 1,
                    "body": tampered_body,
                    "author": {"login": ACTOR},
                }
            ],
            "pageInfo": {"hasNextPage": False},
        }
        with pytest.raises(
            review_ledger.LedgerError, match="malformed or unsupported"
        ):
            review_ledger._verify_complete_v3_threads(REPO, 7, ACTOR)

    orphan = dict(base_thread)
    orphan["comments"] = {
        "nodes": [
            {
                "databaseId": 2,
                "body": _v3_disposition_body(),
                "author": {"login": ACTOR},
            }
        ],
        "pageInfo": {"hasNextPage": False},
    }
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: [orphan])
    with pytest.raises(review_ledger.LedgerError, match="without a finding"):
        review_ledger._verify_complete_v3_threads(REPO, 7, ACTOR)

def test_complete_ledger_rejects_blocking_deferred_disposition(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = {
        "id": "THREAD",
        "isResolved": True,
        "repository": {"nameWithOwner": REPO},
        "pullRequest": {"number": 7},
        "comments": {
            "nodes": [
                {
                    "databaseId": 1,
                    "body": _v3_finding_body(severity="blocking"),
                    "author": {"login": ACTOR},
                },
                {
                    "databaseId": 2,
                    "body": _v3_disposition_body(outcome="deferred"),
                    "author": {"login": ACTOR},
                },
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: [thread])

    with pytest.raises(review_ledger.LedgerError, match="cannot be deferred"):
        review_ledger._verify_complete_v3_threads(REPO, 7, ACTOR)


def test_review_threads_rejects_truncated_top_level_pagination(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor"},
                    }
                }
            }
        }
    }
    monkeypatch.setattr(review_ledger, "_json_output", lambda *_args, **_kwargs: [page])
    with pytest.raises(review_ledger.LedgerError, match="pagination is incomplete"):
        review_ledger._review_threads(REPO, 7)


def test_fixed_finding_must_precede_final_head(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    final_head = "b" * 40
    finding = review_ledger.FINDING_V3_RE.search(
        _v3_finding_body(head=final_head, fingerprint="too-late")
    )
    disposition = review_ledger.DISPOSITION_V3_RE.search(
        _v3_disposition_body(head=final_head, fingerprint="too-late")
    )
    assert finding is not None and disposition is not None
    monkeypatch.setattr(review_ledger, "_verify_review_base", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_verify_git_transition", lambda *_args: None)
    monkeypatch.setattr(
        review_ledger,
        "_verify_complete_v3_threads",
        lambda *_args: [(finding, disposition)],
    )
    monkeypatch.setattr(review_ledger, "_is_ancestor", lambda *_args: True)
    monkeypatch.setattr(
        review_ledger,
        "_load_allowed_heads",
        lambda _args: {HEAD: 0, final_head: 1},
    )
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        base="c" * 40,
        before=HEAD,
        head=final_head,
        engine="codex",
        round=2,
        allowed_heads_file="heads.json",
    )
    data = {
        "status": "changed",
        "classification": "material",
        "findingFingerprints": ["too-late"],
    }
    with pytest.raises(review_ledger.LedgerError, match="not posted before"):
        review_ledger._verify_result_evidence(args, data, ACTOR)


def test_fixed_finding_accepts_intermediate_disposition_head(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    finding_head = "b" * 40
    disposition_head = "c" * 40
    final_head = "d" * 40
    finding = review_ledger.FINDING_V3_RE.search(
        _v3_finding_body(head=finding_head, fingerprint="intermediate-fix")
    )
    disposition = review_ledger.DISPOSITION_V3_RE.search(
        _v3_disposition_body(head=disposition_head, fingerprint="intermediate-fix")
    )
    assert finding is not None and disposition is not None
    monkeypatch.setattr(review_ledger, "_verify_review_base", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_verify_git_transition", lambda *_args: None)
    monkeypatch.setattr(
        review_ledger,
        "_verify_complete_v3_threads",
        lambda *_args: [(finding, disposition)],
    )
    ancestry = {
        (HEAD, finding_head),
        (finding_head, disposition_head),
        (disposition_head, final_head),
    }
    monkeypatch.setattr(
        review_ledger, "_is_ancestor", lambda pair, child: (pair, child) in ancestry
    )
    monkeypatch.setattr(
        review_ledger,
        "_load_allowed_heads",
        lambda _args: {
            HEAD: 0,
            finding_head: 1,
            disposition_head: 2,
            final_head: 3,
        },
    )
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        base="e" * 40,
        before=HEAD,
        head=final_head,
        engine="codex",
        round=2,
        allowed_heads_file="heads.json",
    )
    data = {
        "status": "changed",
        "classification": "material",
        "findingFingerprints": ["intermediate-fix"],
    }
    review_ledger._verify_result_evidence(args, data, ACTOR)


def test_review_base_must_match_git_ancestry_and_pr_boundary(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = "c" * 40
    monkeypatch.setattr(review_ledger, "_run_git", lambda *_args: base + "\n")
    monkeypatch.setattr(review_ledger, "_is_ancestor", lambda *_args: True)
    monkeypatch.setattr(review_ledger, "_run_gh", lambda *_args: base + "\n")
    review_ledger._verify_review_base(REPO, 7, base, HEAD)

    monkeypatch.setattr(review_ledger, "_run_gh", lambda *_args: "d" * 40 + "\n")
    with pytest.raises(review_ledger.LedgerError, match="PR base mismatch"):
        review_ledger._verify_review_base(REPO, 7, base, HEAD)


def test_attestation_identity_replays_exact_body_and_rejects_conflicts(
    review_ledger: ModuleType,
) -> None:
    body = (
        f"<!-- local-review-pass:v3 engine=codex round=2 base={'c' * 40} "
        f"head={HEAD} result-sha256={'d' * 64} -->\nClean."
    )
    rows = [{"id": 7, "body": body}]
    assert review_ledger._matching_attestation(rows, "codex", 2, body) == 7
    with pytest.raises(review_ledger.LedgerError, match="conflicts"):
        review_ledger._matching_attestation(
            rows, "codex", 2, body.replace("Clean.", "Different.")
        )


def test_reconcile_reports_unresolved_disposed_thread(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        {"id": 88, "user": {"login": ACTOR}, "body": _v3_finding_body()},
        {"id": 89, "user": {"login": ACTOR}, "body": _v3_disposition_body()},
    ]
    thread = {
        "id": "THREAD",
        "isResolved": False,
        "comments": {
            "nodes": [{"databaseId": 88}, {"databaseId": 89}],
            "pageInfo": {"hasNextPage": False},
        },
    }
    monkeypatch.setattr(review_ledger, "_verify_head", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_review_comments", lambda *_args: rows)
    monkeypatch.setattr(review_ledger, "_review_threads", lambda *_args: [thread])
    args = argparse.Namespace(repo=REPO, pr=7, head=HEAD, fingerprint="finding")
    review_ledger._reconcile(args)
    output = json.loads(capsys.readouterr().out)
    assert output["nextAction"] == "dispose"
    assert output["threadId"] == "THREAD"
    assert output["threadResolved"] is False


def test_post_reconciles_after_comment_readback_failure(
    review_ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "<!-- local-review:v3 deterministic -->"
    body = marker + "\nFinding."
    comments: list[dict[str, Any]] = []

    def fake_json(args: list[str], payload: dict[str, Any] | None = None) -> Any:
        if "-X" in args:
            assert payload == {
                "body": body,
                "commit_id": HEAD,
                "path": "changed.ts",
                "line": 11,
                "side": "RIGHT",
            }
            comments.append({"id": 123, "body": body, "user": {"login": ACTOR}})
            return {"id": 123}
        raise review_ledger.LedgerError("lost comment readback")

    monkeypatch.setattr(review_ledger, "_json_output", fake_json)
    monkeypatch.setattr(review_ledger, "_review_comments", lambda *_args: comments)
    monkeypatch.setattr(review_ledger, "_verify_head", lambda *_args: None)
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        head=HEAD,
        actor=ACTOR,
        path="changed.ts",
        line=11,
        side="RIGHT",
        file_level=False,
    )
    assert review_ledger._post_review_comment(args, marker, body) == (123, False)


def test_attest_content_file_reads_result_once_and_returns_classification(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result_file = tmp_path / "result.json"
    content_file = tmp_path / "attestation.md"
    content = "Custom attestation content with literal `code`."
    content_file.write_text(content, encoding="utf-8")
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
    reads = 0
    original_read = review_ledger._read_result_bytes

    def counted_read(args: argparse.Namespace) -> bytes:
        nonlocal reads
        reads += 1
        return cast(bytes, original_read(args))

    monkeypatch.setattr(review_ledger, "_read_result_bytes", counted_read)
    monkeypatch.setattr(review_ledger, "_verify_result_evidence", lambda *_args: None)
    base_checks = 0

    def verify_base(*_args: Any) -> None:
        nonlocal base_checks
        base_checks += 1

    monkeypatch.setattr(review_ledger, "_verify_review_base", verify_base)
    monkeypatch.setattr(review_ledger, "_verify_head", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_issue_comments", lambda *_args: [])
    monkeypatch.setattr(review_ledger, "_verify_issue_comment", lambda *_args: None)
    posted: dict[str, Any] = {}

    def fake_json(
        _args: list[str], payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert payload is not None
        posted.update(payload)
        return {"id": 42}

    monkeypatch.setattr(review_ledger, "_json_output", fake_json)
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
            "--content-file",
            str(content_file),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert reads == 1
    assert output["status"] == "clean"
    assert output["classification"] is None
    assert posted["body"].endswith(f"\n{content}")
    assert base_checks == 1


@pytest.mark.parametrize(
    ("race", "lost_delete_response"),
    [("base", False), ("head", False), ("base", True)],
)
def test_attest_rolls_back_new_evidence_and_allows_same_round_retry(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    race: str,
    lost_delete_response: bool,
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
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        head=HEAD,
        engine="codex",
        round=2,
        base="c" * 40,
        before=HEAD,
        result_file=str(result_file),
        content_file=None,
    )
    comments: list[dict[str, Any]] = []
    posts = 0
    base_checks = 0
    head_checks = 0

    def post_comment(
        _args: list[str], payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        nonlocal posts
        assert payload is not None
        posts += 1
        comment = {
            "id": 40 + posts,
            "body": payload["body"],
            "user": {"login": ACTOR},
        }
        comments.append(comment)
        return {"id": comment["id"]}

    def verify_comment(
        _repo: str, comment_id: int, body: str, actor: str | None = None
    ) -> None:
        assert actor == ACTOR
        assert any(
            row["id"] == comment_id and row["body"] == body for row in comments
        )

    def verify_base(*_args: Any) -> None:
        nonlocal base_checks
        base_checks += 1
        if race == "base" and base_checks == 1:
            raise review_ledger.LedgerError("base boundary changed")

    def verify_head(*_args: Any) -> None:
        nonlocal head_checks
        head_checks += 1
        if race == "head" and head_checks == 2:
            raise review_ledger.LedgerError("head boundary changed")

    def delete_comment(args: list[str], payload: dict[str, Any] | None = None) -> str:
        assert payload is None
        comment_id = int(args[-1].rsplit("/", 1)[1])
        comments[:] = [row for row in comments if row["id"] != comment_id]
        if lost_delete_response:
            raise review_ledger.LedgerError("lost delete response")
        return ""

    monkeypatch.setattr(review_ledger, "_verify_result_evidence", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_issue_comments", lambda *_args: list(comments))
    monkeypatch.setattr(review_ledger, "_json_output", post_comment)
    monkeypatch.setattr(review_ledger, "_verify_issue_comment", verify_comment)
    monkeypatch.setattr(review_ledger, "_verify_review_base", verify_base)
    monkeypatch.setattr(review_ledger, "_verify_head", verify_head)
    monkeypatch.setattr(review_ledger, "_run_gh", delete_comment)

    with pytest.raises(review_ledger.LedgerError, match="boundary changed"):
        review_ledger._attest(args)
    assert comments == []

    review_ledger._attest(args)
    assert posts == 2
    assert len(comments) == 1


def test_attest_preserves_preexisting_evidence_on_final_boundary_failure(
    review_ledger: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
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
    result_hash = hashlib.sha256(result_file.read_bytes()).hexdigest()
    body = (
        f"<!-- local-review-pass:v3 engine=codex round=2 base={'c' * 40} "
        f"head={HEAD} result-sha256={result_hash} -->\nNo new material findings."
    )
    comments = [{"id": 41, "body": body, "user": {"login": ACTOR}}]
    args = argparse.Namespace(
        repo=REPO,
        pr=7,
        head=HEAD,
        engine="codex",
        round=2,
        base="c" * 40,
        before=HEAD,
        result_file=str(result_file),
        content_file=None,
    )
    monkeypatch.setattr(review_ledger, "_verify_result_evidence", lambda *_args: None)
    monkeypatch.setattr(review_ledger, "_issue_comments", lambda *_args: comments)
    monkeypatch.setattr(review_ledger, "_verify_issue_comment", lambda *_args: None)
    monkeypatch.setattr(
        review_ledger,
        "_verify_review_base",
        lambda *_args: (_ for _ in ()).throw(
            review_ledger.LedgerError("base boundary changed")
        ),
    )
    monkeypatch.setattr(review_ledger, "_verify_head", lambda *_args: None)
    monkeypatch.setattr(
        review_ledger,
        "_run_gh",
        lambda *_args, **_kwargs: pytest.fail("historical comment must not be deleted"),
    )

    with pytest.raises(review_ledger.LedgerError, match="base boundary changed"):
        review_ledger._attest(args)
    assert comments == [{"id": 41, "body": body, "user": {"login": ACTOR}}]


def test_review_skills_define_wrapper_and_standalone_v3_finalization() -> None:
    root = Path(__file__).resolve().parent.parent
    ledger = (root / ".claude/references/local-review-ledger.md").read_text(
        encoding="utf-8"
    )
    deepcritique = (root / ".claude/skills/deepcritique/SKILL.md").read_text(
        encoding="utf-8"
    )
    critique = (root / ".claude/skills/critique/SKILL.md").read_text(encoding="utf-8")
    codex_review = (root / ".claude/skills/codex-review/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Finalize wrapper and standalone results" in ledger
    assert "helper returns `verified: true`" in ledger
    assert "On a skip, finalize a clean v3 result" in deepcritique
    assert "Do not emit `clean` for a cleanup-moved enclosing hook" in deepcritique
    assert "the enclosing review hook did not move" in critique
    for skill in (deepcritique, critique, codex_review):
        assert "wrapper/standalone" in skill
        assert "standalone pass" in skill
