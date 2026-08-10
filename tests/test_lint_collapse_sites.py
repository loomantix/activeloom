"""Unit tests for `scripts/lint-collapse-sites.py`.

The engine has no Markdown knowledge, so this lint is the only thing standing
between a mistaken `collapse_empty_substitutions` entry and a silently deleted
line of literal content in every consumer's rendered file. Its job is to reject
exactly the sites `drop_empty_placeholder_lines` would happily collapse.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType

import pytest


def _check(lint: ModuleType, tmp_path: Path, body: str, keys: list[str] | None = None) -> list[str]:
    source = tmp_path / "template.md"
    source.write_text(body, encoding="utf-8")
    violations: list[str] = lint.check_source(source, ["E"] if keys is None else keys)
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
    assert _check(lint_collapse_sites, tmp_path, "<<E>>\n\n```\n<<F>>\n```\n", ["E"]) == []


def test_accepts_literal_content_with_an_explicit_empty_opt_in_list(
    lint_collapse_sites: ModuleType, tmp_path: Path
) -> None:
    assert _check(lint_collapse_sites, tmp_path, "```\n<<E>>\n```\n", []) == []


@pytest.mark.parametrize(
    "body",
    [
        "```\ntext\n\n<<E>>\n\nmore\n```\n",
        "~~~\n<<E>>\n~~~\n",
        "<pre>\nline\n\n<<E>>\n\nline\n</pre>\n",
        "intro\n\n    <<E>>\n\noutro\n",
        "intro\n\n\t<<E>>\n\noutro\n",
        "---\nnote: |\n  before\n\n  <<E>>\n\n  after\n---\n",
        '+++\nnote = """\nbefore\n\n  <<E>>\n\nafter\n"""\n+++\n',
    ],
)
def test_rejects_a_placeholder_inside_literal_content(
    lint_collapse_sites: ModuleType, tmp_path: Path, body: str
) -> None:
    violations = _check(lint_collapse_sites, tmp_path, body)
    assert len(violations) == 1
    assert "inside literal Markdown content" in violations[0]


@pytest.mark.parametrize(
    "body",
    [
        "```\ncontent\n``` ```\n<<E>>\n```\n",
        "~~~\ncontent\n~~~payload\n<<E>>\n~~~\n",
        "```\ncontent\n ```python\n<<E>>\n```\n",
        "<script>\ntext\n<<E>>\n</script>\n",
        "<style>\nrule\n<<E>>\n</style>\n",
        "<textarea>\ntext\n<<E>>\n</textarea>\n",
        "<!--\ncomment\n<<E>>\n-->\n",
        "<!--\n--!>\n<<E>>\n-->\n",
        "<!-- closed --> <!-- still open\n\n<<E>>\n\n-->\n",
        "<pre></pre><pre>\n\n<<E>>\n\n</pre>\n",
        "<?instruction\n\n<<E>>\n\n?>\n",
        "<!DECLARATION\n\n<<E>>\n\n>\n",
        "<![CDATA[\n\n<<E>>\n\n]]>\n",
        "- ```\n  code\n\n  <<E>>\n\n  more\n  ```\n",
        "```\n> ```\n\n<<E>>\n\n```\n",
        "> ```\n> quoted\n```\n\n<<E>>\n\n```\n",
        "- ```\n  listed\n```\n\n<<E>>\n\n```\n",
        "- ```\n  first\n\noutside\n\n- ```\n\n  <<E>>\n\n  tail\n  ```\n",
        "intro\n\n \t<<E>>\n\noutro\n",
        "intro\n\n   \t<<E>>\n\noutro\n",
    ],
)
def test_rejects_literal_content_the_old_scanner_misclassified(
    lint_collapse_sites: ModuleType, tmp_path: Path, body: str
) -> None:
    violations = _check(lint_collapse_sites, tmp_path, body)
    assert len(violations) == 1
    assert "inside literal Markdown content" in violations[0]


def test_rejects_a_placeholder_sharing_its_line_with_prose(
    lint_collapse_sites: ModuleType, tmp_path: Path
) -> None:
    violations = _check(lint_collapse_sites, tmp_path, "intro\n\n- <<E>>\n\noutro\n")
    assert len(violations) == 1
    assert "shares its line" in violations[0]


@pytest.mark.parametrize(
    "body",
    [
        "intro\n<<E>>\n\noutro\n",
        "intro\n\n<<E>>\noutro\n",
        "intro\n<<E>>\noutro\n",
        "intro\n\n<<E>>\n\u00a0\noutro\n",
    ],
)
def test_rejects_a_placeholder_without_blank_separators(
    lint_collapse_sites: ModuleType, tmp_path: Path, body: str
) -> None:
    violations = _check(lint_collapse_sites, tmp_path, body)
    assert len(violations) == 1
    assert "blank separator" in violations[0]


def test_rejects_a_line_with_a_non_opted_placeholder(
    lint_collapse_sites: ModuleType, tmp_path: Path
) -> None:
    violations = _check(lint_collapse_sites, tmp_path, "intro\n\n<<E>><<F>>\n\noutro\n")
    assert len(violations) == 1
    assert "placeholder that is not opted in" in violations[0]


def test_rejects_an_invalid_placeholder_key(
    lint_collapse_sites: ModuleType, tmp_path: Path
) -> None:
    violations = _check(lint_collapse_sites, tmp_path, "<<1E>>\n", ["1E"])
    assert len(violations) == 1
    assert "not a valid placeholder key" in violations[0]


def test_rejects_an_opted_in_key_with_no_placeholder_occurrence(
    lint_collapse_sites: ModuleType, tmp_path: Path
) -> None:
    violations = _check(lint_collapse_sites, tmp_path, "no placeholders here\n")
    assert len(violations) == 1
    assert "has no placeholder occurrence" in violations[0]


def test_reports_every_violating_site(lint_collapse_sites: ModuleType, tmp_path: Path) -> None:
    violations = _check(lint_collapse_sites, tmp_path, "```\n<<E>>\n```\n\n> <<E>>\n")
    assert len(violations) == 2


def test_closing_fence_ends_literal_content(lint_collapse_sites: ModuleType, tmp_path: Path) -> None:
    assert _check(lint_collapse_sites, tmp_path, "```\ncode\n```\n\n<<E>>\n") == []


def test_canonical_manifest_passes(
    lint_collapse_sites: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    # The rule is only worth enforcing if this repo's own manifest satisfies it.
    assert lint_collapse_sites.main() == 0
    # `main()` also returns 0 when it checked *nothing* — a renamed `targets:`
    # key, a misspelled `collapse_empty_substitutions` field, or the opt-in
    # block being dropped all yield `checked = 0`. Asserting only the exit code
    # would let the lint decay into a no-op while staying green, so pin the
    # count it reports.
    reported = re.search(r"verified in (\d+) source", capsys.readouterr().out)
    assert reported is not None
    assert int(reported.group(1)) == 1


def test_main_rejects_an_unsafe_site(
    lint_collapse_sites: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "template.md").write_text("```\n<<E>>\n```\n", encoding="utf-8")
    (repo / "scripts" / "sync-targets.yml").write_text(
        "targets:\n"
        "  - source: template.md\n"
        "    destination: rendered.md\n"
        "    substitutions: [E]\n"
        "    collapse_empty_substitutions: [E]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lint_collapse_sites, "REPO_ROOT", repo)

    assert lint_collapse_sites.main() == 1
    assert "must be whole-line, prose-only" in capsys.readouterr().err


def test_main_rejects_non_markdown_destinations(
    lint_collapse_sites: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "template.sh").write_text("cat <<EOF\n<<E>>\nEOF\n", encoding="utf-8")
    (repo / "scripts" / "sync-targets.yml").write_text(
        "targets:\n"
        "  - source: template.sh\n"
        "    destination: rendered.sh\n"
        "    substitutions: [E]\n"
        "    collapse_empty_substitutions: [E]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lint_collapse_sites, "REPO_ROOT", repo)

    assert lint_collapse_sites.main() == 1
    assert "targets non-Markdown destination" in capsys.readouterr().err
