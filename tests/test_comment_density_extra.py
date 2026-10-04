"""Audit-only language coverage never supplies a preservation fingerprint."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "prompts/skills/simplify-comments/scripts/comment_audit.py"
SPEC = importlib.util.spec_from_file_location("comment_audit", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


@pytest.mark.parametrize(
    ("language", "text", "expected"),
    [
        ("shell", "#!/bin/sh\n# prose\nprintf ok\n", ["code", "comment", "code"]),
        ("yaml", "# prose\nname: sample\n\n", ["comment", "code", "blank"]),
        ("sql", "-- prose\nSELECT 1;\n", ["comment", "code"]),
        ("css", "/* prose\nmore */\na {}\n", ["comment", "comment", "code"]),
        ("html", "<!-- prose -->\n<p>sample</p>\n", ["comment", "code"]),
        ("prisma", "// prose\nmodel Example {}\n", ["comment", "code"]),
        ("template", "{{/* prose */}}\nplain\n", ["comment", "code"]),
    ],
)
def test_simple_density_estimates(language: str, text: str, expected: list[str]) -> None:
    assert AUDIT.audit_kinds(text, language) == expected
    assert "audit-only" in AUDIT.audit_reason(language)
    assert "grammar-aware verifier" in AUDIT.audit_reason(language)


@pytest.mark.parametrize(
    ("language", "source"),
    [
        ("shell", "cat <<'END'\n# literal body\n\nEND\n# real but undercounted\n"),
        ("shell", 'printf "%s" "multiline\n# literal\n"\n'),
        ("shell", "printf \\\n# argument\n"),
        ("yaml", "example: |\n  # literal body\n\n# real but undercounted\n"),
        ("yaml", "example: >- # folded\n  # literal body\n"),
        ("sql", "SELECT $body$\n-- literal body\n$body$;\n"),
        ("sql", "SELECT 'multiline\n-- literal body\n';\n"),
        ("html", "<script>\n<!-- literal raw text\n</script>\n"),
        ("html", '<a title="multiline\n<!-- attribute text -->\n">\n'),
        ("template", '{{- $value := "literal" -}}\n{{/* cannot count context */}}\n'),
        ("yaml", 'value: {{ "literal" }}\n# templated context\n'),
        ("css", 'a { content: "multiline\n/* literal */\n"; }\n'),
    ],
)
def test_opaque_content_is_conservatively_counted_as_code(language: str, source: str) -> None:
    assert AUDIT.audit_kinds(source, language) == ["code"] * source.count("\n")


def test_mixed_block_line_keeps_code_and_opaque_suffix() -> None:
    assert AUDIT.audit_kinds("/* prose */ a {}\n/* opaque */\n", "css") == [
        "code",
        "code",
    ]
    assert AUDIT.audit_kinds("/* prose\n*/ a {}\n/* opaque */\n", "css") == [
        "comment",
        "code",
        "code",
    ]


def test_empty_and_crlf_input() -> None:
    assert AUDIT.audit_kinds("", "yaml") == []
    assert AUDIT.audit_kinds("# prose\r\n\r\nkey: value\r\n", "yaml") == [
        "comment",
        "blank",
        "code",
    ]


def test_unsupported_language_is_explicit() -> None:
    with pytest.raises(ValueError, match="unsupported audit language"):
        AUDIT.audit_kinds("", "unknown")
