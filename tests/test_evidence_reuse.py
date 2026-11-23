"""Contract for horch's test evidence comparator (`horch/scripts/evidence-reuse.py`).

Reuse only on an exact identity match; anything missing, malformed or
mismatched means `rerun`, so tests at the head stay mandatory.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "prompts/skills/horch/scripts/evidence-reuse.py"

spec = importlib.util.spec_from_file_location("evidence_reuse", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

HEAD = "a" * 40
NEW_HEAD = "e" * 40


def receipt(**overrides: Any) -> dict[str, Any]:
    base = {
        "version": 1,
        "evidence_class": "unit",
        "head": HEAD,
        "tree": "b" * 40,
        "command": "c" * 64,
        "inputs": "d" * 64,
        "toolchain": "f" * 64,
        "environment": "local-fresh-db",
        "outcome": "pass",
        "skipped": [],
    }
    base.update(overrides)
    return base


def expected(**overrides: Any) -> dict[str, Any]:
    base = {key: value for key, value in receipt().items() if key not in ("outcome", "skipped")}
    base.update(head=NEW_HEAD, allowed_skips=[])
    base.update(overrides)
    return base


def decide(old: Any, new: Any) -> dict[str, Any]:
    return module.compare(old, new)


def test_same_identity_at_a_new_commit_is_reuse() -> None:
    result = decide(receipt(), expected())
    assert result == {
        "decision": "reuse",
        "reasons": [],
        "receipt_head": HEAD,
        "expected_head": NEW_HEAD,
    }


def test_sha256_object_ids_are_accepted() -> None:
    assert decide(receipt(tree="9" * 64), expected(tree="9" * 64))["decision"] == "reuse"


MISMATCHES = [
    ("tree", "0" * 40, "tree-mismatch"),
    ("command", "0" * 64, "command-mismatch"),
    ("inputs", "0" * 64, "inputs-mismatch"),
    ("toolchain", "0" * 64, "toolchain-mismatch"),
    ("environment", "ci-linux", "environment-mismatch"),
    ("evidence_class", "integration", "evidence-class-mismatch"),
]


def test_every_identity_field_has_a_mismatch_case() -> None:
    assert {field for field, _, _ in MISMATCHES} == set(module.IDENTITY)


@pytest.mark.parametrize(("field", "value", "reason"), MISMATCHES)
def test_each_identity_mismatch_is_rerun_with_its_reason(field: str, value: str, reason: str) -> None:
    result = decide(receipt(), expected(**{field: value}))
    assert result == {"decision": "rerun", "reasons": [reason]}


def test_application_evidence_never_stands_in_for_integration() -> None:
    old = receipt(evidence_class="unit")
    result = decide(old, expected(evidence_class="integration"))
    assert result["decision"] == "rerun"
    assert "evidence-class-mismatch" in result["reasons"]


@pytest.mark.parametrize("outcome", ["fail", "skipped", "PASS", "passed"])
def test_non_pass_outcome_is_rerun(outcome: str) -> None:
    assert decide(receipt(outcome=outcome), expected())["reasons"] == ["outcome-not-pass"]


def test_skip_needs_current_gate_allowance() -> None:
    old = receipt(skipped=["db-suite"])
    assert decide(old, expected())["reasons"] == ["unapproved-skip"]
    assert decide(old, expected(allowed_skips=["db-suite"]))["decision"] == "reuse"
    newer = receipt(skipped=["db-suite", "new"])
    assert decide(newer, expected(allowed_skips=["db-suite"]))["reasons"] == ["unapproved-skip"]


def test_all_reasons_are_reported() -> None:
    result = decide(receipt(outcome="fail"), expected(tree="0" * 40, command="0" * 64))
    assert result["reasons"] == ["outcome-not-pass", "tree-mismatch", "command-mismatch"]


@pytest.mark.parametrize("field", sorted(module.RECEIPT_FIELDS))
def test_missing_receipt_field_is_rerun(field: str) -> None:
    old = receipt()
    del old[field]
    result = decide(old, expected())
    assert result["decision"] == "rerun"
    assert f"invalid-receipt:{field}" in result["reasons"]


@pytest.mark.parametrize("field", sorted(module.EXPECTED_FIELDS))
def test_missing_expected_field_is_rerun(field: str) -> None:
    new = expected()
    del new[field]
    result = decide(receipt(), new)
    assert result["decision"] == "rerun"
    assert f"invalid-expected:{field}" in result["reasons"]


MALFORMED = [
    ("version", 2),
    ("version", True),
    ("version", "1"),
    ("head", "abc"),
    ("head", "A" * 40),
    ("tree", "b" * 39),
    ("tree", None),
    ("command", "c" * 63),
    ("command", "not-a-digest"),
    ("inputs", ""),
    ("toolchain", 123),
    ("environment", ""),
    ("environment", "Has Spaces"),
    ("evidence_class", ["unit"]),
    ("outcome", None),
    ("skipped", "none"),
    ("skipped", ["x", "x"]),
    ("skipped", [""]),
    ("skipped", [1]),
]


@pytest.mark.parametrize(("field", "value"), MALFORMED)
def test_malformed_receipt_field_is_rerun(field: str, value: Any) -> None:
    result = decide(receipt(**{field: value}), expected())
    assert result == {"decision": "rerun", "reasons": [f"invalid-receipt:{field}"]}


def test_malformed_field_is_rerun_even_when_both_sides_agree() -> None:
    result = decide(receipt(tree="garbage"), expected(tree="garbage"))
    assert result["decision"] == "rerun"


def test_malformed_allowed_skips_is_rerun() -> None:
    result = decide(receipt(), expected(allowed_skips="all"))
    assert result == {"decision": "rerun", "reasons": ["invalid-expected:allowed_skips"]}


def test_unknown_fields_are_rerun() -> None:
    assert decide(receipt(note="x"), expected())["reasons"] == ["unknown-receipt-field:note"]
    assert decide(receipt(), expected(outcome="pass"))["reasons"] == ["unknown-expected-field:outcome"]


@pytest.mark.parametrize("document", [None, [], "receipt", 1])
def test_non_object_documents_are_rerun(document: Any) -> None:
    assert decide(document, expected())["reasons"] == ["invalid-receipt:document"]
    assert decide(receipt(), document)["reasons"] == ["invalid-expected:document"]


def run_cli(helper: Path, *paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(helper), *map(str, paths)],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_cli_exit_codes(tmp_path: Path) -> None:
    old, new, bad = tmp_path / "r.json", tmp_path / "e.json", tmp_path / "bad.json"
    old.write_text(json.dumps(receipt()))
    new.write_text(json.dumps(expected()))
    bad.write_text("{not json")

    reuse = run_cli(SCRIPT, old, new)
    assert reuse.returncode == 0, reuse.stderr
    assert json.loads(reuse.stdout)["decision"] == "reuse"

    new.write_text(json.dumps(expected(tree="0" * 40)))
    rerun = run_cli(SCRIPT, old, new)
    assert rerun.returncode == 3
    assert json.loads(rerun.stdout) == {"decision": "rerun", "reasons": ["tree-mismatch"]}

    for args in ((bad, new), (tmp_path / "missing.json", new)):
        unreadable = run_cli(SCRIPT, *args)
        assert unreadable.returncode == 2
        assert json.loads(unreadable.stdout) == {"decision": "rerun", "reasons": ["unreadable-input"]}


@pytest.mark.parametrize("harness", ["claude", "codex", "agents"])
def test_rendered_helper_matches_source_and_is_synced(harness: str) -> None:
    targets = yaml.safe_load((ROOT / "scripts/sync-targets.yml").read_text())["harnesses"]
    rows = targets["gemini" if harness == "agents" else harness]["targets"]
    for relative in ("scripts/evidence-reuse.py", "references/evidence-reuse.md"):
        path = f".{harness}/skills/horch/{relative}"
        assert (ROOT / path).is_file()
        assert any(row.get("source") == path and row.get("destination") == path for row in rows)
    rendered = ROOT / f".{harness}/skills/horch/scripts/evidence-reuse.py"
    assert rendered.read_bytes() == SCRIPT.read_bytes()


@pytest.mark.parametrize("skill", ["hloop", "horch"])
def test_ship_steps_cite_the_comparator_and_keep_tests_mandatory(skill: str) -> None:
    text = (ROOT / f"prompts/skills/{skill}/SKILL.md").read_text()
    assert "evidence-reuse.md" in text
    assert "`rerun`" in text
    assert "the lane runs" in text
