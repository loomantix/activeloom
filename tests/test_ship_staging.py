"""Synthetic GitHub CLI responses exercise the staging CI gate without network access."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELATIVE = "skills/ship-staging/scripts/check-pr-checks.py"
HEAD = "a" * 40
IDENTITY = {
    "headRefOid": HEAD, "headRefName": "feature/example",
    "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
}
INVALID_FIELD_VALUES: tuple[object, ...] = (None, "", False, [])


@pytest.fixture
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ship_checks", ROOT / ".codex" / RELATIVE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(data: object, code: int = 0, error: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], code, json.dumps(data), error)


def check(bucket: str) -> list[dict[str, str]]:
    return [{"bucket": bucket, "name": "unit tests", "state": "SUCCESS"}]


def no_required() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["gh"], 1, "", "no required checks reported on the 'feature/example' branch\n",
    )


def mock_gh(
    monkeypatch: pytest.MonkeyPatch, gate: ModuleType,
    replies: list[subprocess.CompletedProcess[str]],
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    remaining = iter(replies)

    def invoke(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return next(remaining)

    monkeypatch.setattr(gate, "gh", invoke)
    return calls


@pytest.mark.parametrize("required", [no_required(), response([]), response(check("pass"))])
def test_passing_ci_with_or_without_required_checks(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch, required: subprocess.CompletedProcess[str],
) -> None:
    calls = mock_gh(monkeypatch, gate, [
        response(IDENTITY), required, response(check("pass") + check("skipping")), response(IDENTITY),
    ])
    result = gate.verify("example/project", "42", HEAD)
    assert result["available_count"] == 2
    assert result["required_count"] == (1 if required.stdout != "[]" and required.returncode == 0 else 0)
    assert "--required" in calls[1] and "--required" not in calls[2]
    assert all("example/project" in call for call in calls)


@pytest.mark.parametrize("bucket", ["fail", "pending", "cancel", "unknown"])
def test_required_checks_must_pass(gate: ModuleType, monkeypatch: pytest.MonkeyPatch, bucket: str) -> None:
    mock_gh(monkeypatch, gate, [response(IDENTITY), response(check(bucket))])
    with pytest.raises(ValueError):
        gate.verify("example/project", "42", HEAD)


@pytest.mark.parametrize("bucket", ["fail", "pending", "cancel", "unknown"])
def test_optional_checks_still_block(gate: ModuleType, monkeypatch: pytest.MonkeyPatch, bucket: str) -> None:
    mock_gh(monkeypatch, gate, [response(IDENTITY), no_required(), response(check(bucket))])
    with pytest.raises(ValueError):
        gate.verify("example/project", "42", HEAD)


@pytest.mark.parametrize("bad", [
    subprocess.CompletedProcess(["gh"], 1, "", "HTTP 401: authentication required"),
    subprocess.CompletedProcess(["gh"], 1, "", "network unavailable"),
    subprocess.CompletedProcess(["gh"], 1, "", "no required checks reported on the 'other' branch"),
    subprocess.CompletedProcess(["gh"], 1, "", no_required().stderr + "HTTP 500"),
    subprocess.CompletedProcess(["gh"], 0, "not JSON", ""),
    response(None), response({}), response([{}]), response([{"bucket": "pass"}]),
    response([], 8), response(check("fail"), 1),
])
def test_lookup_errors_are_not_empty_requirements(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch, bad: subprocess.CompletedProcess[str],
) -> None:
    calls = mock_gh(monkeypatch, gate, [response(IDENTITY), bad])
    with pytest.raises(ValueError):
        gate.verify("example/project", "42", HEAD)
    assert len(calls) == 2


@pytest.mark.parametrize("available", [response([]), subprocess.CompletedProcess(
    ["gh"], 1, "", "no checks reported on the 'feature/example' branch",
)])
def test_no_ci_evidence_refuses(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch, available: subprocess.CompletedProcess[str],
) -> None:
    mock_gh(monkeypatch, gate, [response(IDENTITY), no_required(), available])
    with pytest.raises(ValueError):
        gate.verify("example/project", "42", HEAD)


def test_changed_head_refuses(gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_gh(monkeypatch, gate, [response(IDENTITY)])
    with pytest.raises(ValueError, match="head changed"):
        gate.verify("example/project", "42", "b" * 40)
    mock_gh(monkeypatch, gate, [
        response(IDENTITY), no_required(), response(check("pass")),
        response({**IDENTITY, "headRefOid": "b" * 40}),
    ])
    with pytest.raises(ValueError, match="head changed"):
        gate.verify("example/project", "42", HEAD)


@pytest.mark.parametrize("state", ["CLEAN", "HAS_HOOKS"])
def test_required_skip_uses_fresh_merge_state(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    calls = mock_gh(monkeypatch, gate, [
        response({**IDENTITY, "mergeStateStatus": "UNKNOWN", "mergeable": "UNKNOWN"}),
        response(check("skipping")), response(check("skipping")),
        response({**IDENTITY, "mergeStateStatus": state}),
    ])
    assert gate.verify("example/project", "42", HEAD)["required_count"] == 1
    assert len(calls) == 4
    assert "mergeable" in calls[-1][-1] and "mergeStateStatus" in calls[-1][-1]


@pytest.mark.parametrize("bucket", ["pass", "skipping"])
@pytest.mark.parametrize("update", [
    {"mergeStateStatus": state}
    for state in ("BLOCKED", "BEHIND", "DIRTY", "UNSTABLE", "UNKNOWN", "DRAFT", "unexpected")
] + [{"mergeable": state} for state in ("CONFLICTING", "UNKNOWN", "unexpected")])
def test_fresh_nonmergeable_state_refuses(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch, bucket: str, update: dict[str, str],
) -> None:
    mock_gh(monkeypatch, gate, [
        response(IDENTITY), response(check(bucket)), response(check(bucket)),
        response({**IDENTITY, **update}),
    ])
    with pytest.raises(ValueError, match="not mergeable"):
        gate.verify("example/project", "42", HEAD)


@pytest.mark.parametrize("update", [{"headRefOid": "b" * 40}, {"headRefName": "other"}])
def test_skipped_required_head_race_refuses(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch, update: dict[str, str],
) -> None:
    mock_gh(monkeypatch, gate, [
        response(IDENTITY), response(check("skipping")), response(check("skipping")),
        response({**IDENTITY, **update}),
    ])
    with pytest.raises(ValueError, match="head changed"):
        gate.verify("example/project", "42", HEAD)


@pytest.mark.parametrize("final", [False, True])
@pytest.mark.parametrize("bad", [
    response(None), response([]), response({}),
    subprocess.CompletedProcess(["gh"], 0, "not JSON", ""),
    subprocess.CompletedProcess(["gh"], 1, "", "network unavailable"),
    *[response({**IDENTITY, key: value}) for key in IDENTITY for value in INVALID_FIELD_VALUES],
])
def test_mergeability_read_errors_refuse(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch,
    final: bool, bad: subprocess.CompletedProcess[str],
) -> None:
    replies = [response(IDENTITY), response(check("skipping")), response(check("skipping"))] if final else []
    mock_gh(monkeypatch, gate, [*replies, bad])
    with pytest.raises(ValueError):
        gate.verify("example/project", "42", HEAD)


def test_helpers_ship_to_both_harnesses() -> None:
    assert (ROOT / ".codex" / RELATIVE).read_bytes() == (ROOT / ".agents" / RELATIVE).read_bytes()
    manifest = yaml.safe_load((ROOT / "scripts/sync-targets.yml").read_text())
    # Check the distribution contract, not only the files in this checkout.
    for engine, harness in (("codex", ".codex"), ("gemini", ".agents")):
        destination = f"{harness}/{RELATIVE}"
        targets = manifest["harnesses"][engine]["targets"]
        entry = next(target for target in targets if target["destination"] == destination)
        assert (ROOT / entry["source"]).read_bytes() == (ROOT / destination).read_bytes()
