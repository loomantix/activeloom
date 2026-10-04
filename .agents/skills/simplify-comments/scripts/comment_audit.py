"""Conservative density estimates for languages without certified verification.

These counts are advisory, never a code-preservation fingerprint. Once an
opaque construct is encountered, the remaining lines count as code. This
deliberately undercounts comments rather than treating embedded text as proof
that an edit is safe. Grammar-aware certification remains unavailable.
"""

from __future__ import annotations

import re

AUDIT_LANGUAGES = {
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sql": "sql",
    ".css": "css",
    ".html": "html",
    ".htm": "html",
    ".prisma": "prisma",
    ".tpl": "template",
    ".gotmpl": "template",
}

_LINE_MARKERS = {"shell": "#", "yaml": "#", "sql": "--", "prisma": "//"}
_BLOCK_MARKERS = {
    "sql": ("/*", "*/"),
    "css": ("/*", "*/"),
    "html": ("<!--", "-->"),
    "template": ("{{/*", "*/}}"),
}
_LIMITATIONS = {
    "shell": "heredocs, substitutions and shell dialects require a grammar-aware verifier",
    "yaml": "scalar styles, indentation and embedded languages require a grammar-aware verifier",
    "sql": "SQL dialects, quoted bodies and executable comments require a grammar-aware verifier",
    "css": "escapes, token boundaries and directives require a grammar-aware verifier",
    "html": "raw text, embedded languages and significant whitespace require a grammar-aware verifier",
    "prisma": "schema syntax and documentation directives require a grammar-aware verifier",
    "template": "template actions, whitespace trimming and embedded languages require a grammar-aware verifier",
}


def audit_reason(language: str) -> str:
    """Explain why this language cannot certify comment-only edits."""
    return f"audit-only language: {_LIMITATIONS[language]}"


def _opaque(line: str, language: str) -> bool:
    """Recognize contexts where counting subsequent lines needs a real parser."""
    if "{{" in line:
        return True
    if language == "shell" and ("<<" in line or line.rstrip().endswith("\\")):
        return True
    if language == "yaml" and re.search(r"[|>][0-9+-]*\s*(?:#.*)?$", line):
        return True
    if language == "sql" and re.search(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", line):
        return True
    if language == "html" and re.search(r"<(?:script|style|textarea|title)\b", line, re.I):
        return True
    # Escapes and multiline quotes have language-specific rules. Balanced
    # ordinary quotes are useful for density, but are not certification.
    quote = ""
    escaped = False
    for char in line:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
    return bool(quote) or escaped


def audit_kinds(text: str, language: str) -> list[str]:
    """Estimate line kinds; opaque suffixes count entirely as code.

    Full-line comments and blocks starting on a comment-only line are counted.
    Inline block openings are intentionally not followed. Counts may undercount
    comments after quoted or embedded syntax; callers must mark them approximate.
    """
    if language not in _LIMITATIONS:
        raise ValueError(f"unsupported audit language: {language}")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    kinds: list[str] = []
    block_end = ""
    opaque = False
    for line in lines:
        stripped = line.strip()
        if opaque:
            kinds.append("code")
            continue
        if block_end:
            before, closed, after = stripped.partition(block_end)
            kinds.append("code" if closed and after.strip() else "comment")
            if closed:
                block_end = ""
                opaque = bool(after.strip())
            continue
        if not stripped:
            kinds.append("blank")
            continue
        marker = _LINE_MARKERS.get(language)
        if marker and stripped.startswith(marker):
            kinds.append("code" if language == "shell" and stripped.startswith("#!") else "comment")
            continue
        block = _BLOCK_MARKERS.get(language)
        if block and stripped.startswith(block[0]):
            _, closed, after = stripped[len(block[0]) :].partition(block[1])
            kinds.append("code" if closed and after.strip() else "comment")
            if not closed:
                block_end = block[1]
            elif after.strip():
                opaque = True
            continue
        kinds.append("code")
        opaque = _opaque(line, language)
    return kinds
