"""Regression tests for full-history signature checks before local review."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


REPO = "example/repository"
HEAD = "c" * 40
SIGNED = "b" * 40
UNSIGNED = "a" * 40


@pytest.fixture
def handoff() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / ".codex/skills/critique/scripts/local-review-handoff.py"
    )
    spec = importlib.util.spec_from_file_location("signature_handoff", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["signature_handoff"] = module
    spec.loader.exec_module(module)
    return module


def commit(sha: str, *, verified: bool, reason: str) -> dict[str, Any]:
    """Build the relevant shape returned by GitHub's PR commits endpoint."""
    return {
        "sha": sha,
        "commit": {"verification": {"verified": verified, "reason": reason}},
    }


def github_responses(
    monkeypatch: pytest.MonkeyPatch,
    handoff: ModuleType,
    commits: list[dict[str, Any]],
    *,
    reported_count: int | None = None,
    live_head: str = HEAD,
    require_signatures: bool = True,
    classic_signatures: bool = False,
    base_ref: str = "main",
) -> None:
    """Stub the GitHub reads used by the signature preflight."""

    def fake_json(args: list[str], payload: dict[str, Any] | None = None) -> Any:
        assert payload is None
        if args == ["api", f"repos/{REPO}/pulls/7"]:
            return {
                "commits": len(commits) if reported_count is None else reported_count,
                "base": {"ref": base_ref},
                "head": {"sha": live_head},
            }
        encoded_base_ref = base_ref.replace("/", "%2F")
        if args == ["api", f"repos/{REPO}/rules/branches/{encoded_base_ref}"]:
            return [{"type": "required_signatures"}] if require_signatures else []
        if args == [
            "api",
            "graphql",
            "-f",
            "query=query($owner:String!,$name:String!,$qualifiedName:String!){repository(owner:$owner,name:$name){ref(qualifiedName:$qualifiedName){branchProtectionRule{requiresCommitSignatures}}}}",
            "-F",
            "owner=example",
            "-F",
            "name=repository",
            "-F",
            f"qualifiedName=refs/heads/{base_ref}",
        ]:
            assert not require_signatures
            rule = {"requiresCommitSignatures": True} if classic_signatures else None
            return {"data": {"repository": {"ref": {"branchProtectionRule": rule}}}}
        if args == [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{REPO}/pulls/7/commits?per_page=100",
        ]:
            return [commits]
        raise AssertionError(args)

    monkeypatch.setattr(handoff, "_json_output", fake_json)


def test_full_history_accepts_only_verified_commits(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_responses(
        monkeypatch,
        handoff,
        [
            commit(SIGNED, verified=True, reason="valid"),
            commit(HEAD, verified=True, reason="valid"),
        ],
    )

    handoff._verify_signed_pr_history(REPO, 7, HEAD)


def test_signed_head_does_not_hide_unsigned_ancestor(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_responses(
        monkeypatch,
        handoff,
        [
            commit(UNSIGNED, verified=False, reason="unsigned"),
            commit(HEAD, verified=True, reason="valid"),
        ],
    )

    with pytest.raises(handoff.HandoffError) as error:
        handoff._verify_signed_pr_history(REPO, 7, HEAD)

    message = str(error.value)
    assert f"{UNSIGNED} (unsigned)" in message
    assert "signed head does not repair an unsigned ancestor" in message
    assert "explicit approval for a lease-protected force-push" in message
    assert "Do not amend, rebase, or force-push without that approval" in message


def test_unsigned_history_is_allowed_when_target_policy_does_not_require_signatures(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_responses(
        monkeypatch,
        handoff,
        [commit(HEAD, verified=False, reason="unsigned")],
        require_signatures=False,
    )

    handoff._verify_signed_pr_history(REPO, 7, HEAD)


def test_classic_branch_protection_requires_full_history(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_responses(
        monkeypatch,
        handoff,
        [
            commit(UNSIGNED, verified=False, reason="unsigned"),
            commit(HEAD, verified=True, reason="valid"),
        ],
        require_signatures=False,
        classic_signatures=True,
    )

    with pytest.raises(handoff.HandoffError) as error:
        handoff._verify_signed_pr_history(REPO, 7, HEAD)

    assert f"{UNSIGNED} (unsigned)" in str(error.value)


def test_branch_without_classic_signature_policy_allows_unsigned_history(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_responses(
        monkeypatch,
        handoff,
        [commit(HEAD, verified=False, reason="unsigned")],
        require_signatures=False,
    )

    handoff._verify_signed_pr_history(REPO, 7, HEAD)


def test_classic_policy_query_fails_closed_on_graphql_errors(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        handoff,
        "_json_output",
        lambda *args, **kwargs: {"errors": [{"message": "denied"}]},
    )

    with pytest.raises(
        handoff.HandoffError, match="could not resolve classic branch protection"
    ):
        handoff._classic_signatures_required(REPO, "main")


def test_base_branch_is_encoded_for_effective_rules_lookup(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_responses(
        monkeypatch,
        handoff,
        [commit(HEAD, verified=True, reason="valid")],
        base_ref="release/next",
    )

    handoff._verify_signed_pr_history(REPO, 7, HEAD)


def test_signature_check_fails_when_github_omits_part_of_history(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_responses(
        monkeypatch,
        handoff,
        [commit(HEAD, verified=True, reason="valid")],
        reported_count=2,
    )

    with pytest.raises(handoff.HandoffError, match="entire PR commit history"):
        handoff._verify_signed_pr_history(REPO, 7, HEAD)


def test_signature_check_rejects_head_change_during_preflight(
    handoff: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_responses(
        monkeypatch,
        handoff,
        [commit(SIGNED, verified=True, reason="valid")],
        live_head=SIGNED,
    )

    with pytest.raises(handoff.HandoffError, match="head mismatch"):
        handoff._verify_signed_pr_history(REPO, 7, HEAD)
