"""Unit tests for `scripts/lint-collapse-sites.py`.

The engine has no Markdown knowledge, so this lint is the only thing standing
between a mistaken `collapse_empty_substitutions` entry and a silently deleted
line of literal content in every consumer's rendered file. Its job is to reject
exactly the sites `drop_empty_placeholder_lines` would happily collapse.
"""
from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest


def _check(lint: ModuleType, tmp_path: Path, body: str, keys: list[str] | None = None) -> list[str]:
    source = tmp_path / "template.md"
    source.write_text(body, encoding="utf-8")
    violations: list[str] = lint.check_source(source, keys or ["E"])
    return violations


def test_accepts_a_whole_line_prose_placeholder(lint_collapse_sites: ModuleType, tmp_path: Path) -> None:
    assert _check(lint_collapse_sites, tmp_path, "intro\n\n<<E>>\n\noutro\n") == []


def test_accepts_a_line_of_only_placeholders(lint_collapse_sites: ModuleType, tmp_path: Path) -> None:
    assert _check(lint_collapse_sites, tmp_path, "intro\n\n<<E>> <<F>>\n\noutro\n", ["E", "F"]) == []


def test_accepts_an_unopted_key_inside_literal_content(
    lint_collapse_sites: ModuleType, tmp_path: Path
) -> None:
    # `F` is not opted in, so its position is irrelevant — the engine will
    # never collapse its line.
    assert _check(lint_collapse_sites, tmp_path, "```\n<<F>>\n```\n", ["E"]) == []


@pytest.mark.parametrize(
    "body",
    [
        "```\ntext\n\n<<E>>\n\nmore\n```\n",
        "~~~\n<<E>>\n~~~\n",
        "<pre>\nline\n\n<<E>>\n\nline\n</pre>\n",
        "intro\n\n    <<E>>\n\noutro\n",
        "intro\n\n\t<<E>>\n\noutro\n",
    ],
)
def test_rejects_a_placeholder_inside_literal_content(
    lint_collapse_sites: ModuleType, tmp_path: Path, body: str
) -> None:
    violations = _check(lint_collapse_sites, tmp_path, body)
    assert len(violations) == 1
    assert "inside literal content" in violations[0]


def test_rejects_a_placeholder_sharing_its_line_with_prose(
    lint_collapse_sites: ModuleType, tmp_path: Path
) -> None:
    violations = _check(lint_collapse_sites, tmp_path, "intro\n\n- <<E>>\n\noutro\n")
    assert len(violations) == 1
    assert "shares its line" in violations[0]


def test_reports_every_violating_site(lint_collapse_sites: ModuleType, tmp_path: Path) -> None:
    violations = _check(lint_collapse_sites, tmp_path, "```\n<<E>>\n```\n\n> <<E>>\n")
    assert len(violations) == 2


def test_closing_fence_ends_literal_content(lint_collapse_sites: ModuleType, tmp_path: Path) -> None:
    assert _check(lint_collapse_sites, tmp_path, "```\ncode\n```\n\n<<E>>\n") == []


def test_canonical_manifest_passes(lint_collapse_sites: ModuleType) -> None:
    # The rule is only worth enforcing if this repo's own manifest satisfies it.
    assert lint_collapse_sites.main() == 0
