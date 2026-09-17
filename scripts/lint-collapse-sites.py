#!/usr/bin/env python3
"""Check every `collapse_empty_substitutions` key against its template sites.

Ensures opted-in placeholders occur alone on their lines outside literal
Markdown content (code blocks, raw HTML tags, comments), preventing
`sync-engine.py` from deleting lines from literal content in rendered files.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
PLACEHOLDER_RE = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")
FENCE_RE = re.compile(r"(`{3,}|~{3,})")
LIST_ITEM_FENCE_PREFIX_RE = re.compile(r"(?:^|[ \t>])(?:[-+*]|\d{1,9}[.)])[ \t]+\Z")
RAW_TAG_EVENT_RE = re.compile(
    r"</(?P<close>pre|script|style|textarea)\s*>|<(?P<open>pre|script|style|textarea)\b",
    re.IGNORECASE,
)
HTML_COMMENT_OPEN = "<!--"
HTML_COMMENT_CLOSE = "-->"
PROCESSING_INSTRUCTION_OPEN = "<?"
PROCESSING_INSTRUCTION_CLOSE = "?>"
CDATA_OPEN = "<![CDATA["
CDATA_CLOSE = "]]>"


def front_matter_delimiter(lines: list[str]) -> str | None:
    """Return the delimiter line 0 opens a front-matter block with, if any."""
    if not lines:
        return None
    first = lines[0].lstrip("\ufeff").strip(" \t\r")
    return first if first in {"---", "+++"} else None


def literal_content_lines(lines: list[str]) -> list[bool]:
    """Mark each line that a Markdown reader would treat as literal content.

    Scans under all interpretations of ambiguous fence and front-matter rules,
    marking a line literal if any interpretation considers it literal.
    """
    scans = [
        _scan_literal_lines(lines, None, strict_fence_prefix=True),
        _scan_literal_lines(lines, None, strict_fence_prefix=False),
    ]
    delimiter = front_matter_delimiter(lines)
    if delimiter is not None:
        scans.append(_scan_literal_lines(lines, delimiter, strict_fence_prefix=True))
        scans.append(_scan_literal_lines(lines, delimiter, strict_fence_prefix=False))
    return [any(marks) for marks in zip(*scans)]


def _scan_literal_lines(
    lines: list[str], delimiter: str | None, *, strict_fence_prefix: bool
) -> list[bool]:
    """Scan literal lines under one reading of fence and front-matter rules."""
    literal = [False] * len(lines)
    fence: tuple[str, int, str] | None = None
    raw_tag: str | None = None
    in_comment = False
    in_processing_instruction = False
    in_declaration = False
    in_cdata = False
    for index, line in enumerate(lines):
        if delimiter is not None:
            literal[index] = True
            # Closing delimiter must be at column 0 to prevent indented markers in block scalars ending the block.
            if index > 0 and line.rstrip(" \t\r") == delimiter:
                delimiter = None
            continue
        fence_match = FENCE_RE.search(line)
        if fence_match is not None and any(
            char not in " \t>-+*0123456789.)" for char in line[: fence_match.start()]
        ):
            fence_match = None
        if fence is None and fence_match is not None:
            fence = (
                fence_match.group(1)[0],
                len(fence_match.group(1)),
                line[: fence_match.start()],
            )
            literal[index] = True
            continue
        if fence is not None:
            literal[index] = True
            char, length, opener_prefix = fence
            if (
                fence_match is not None
                and fence_match.group(1)[0] == char
                and len(fence_match.group(1)) >= length
                and (
                    not strict_fence_prefix
                    or (
                        line[: fence_match.start()] == opener_prefix
                        # A repeated list prefix starts another item rather than closing the fence.
                        and LIST_ITEM_FENCE_PREFIX_RE.search(opener_prefix) is None
                    )
                )
                and not line[fence_match.end() :].strip(" \t\r")
            ):
                fence = None
            continue
        raw_literal = raw_tag is not None
        for event in RAW_TAG_EVENT_RE.finditer(line):
            opening = event.group("open")
            closing = event.group("close")
            if raw_tag is None and opening is not None:
                raw_tag = opening.lower()
                raw_literal = True
            elif raw_tag is not None and closing is not None and closing.lower() == raw_tag:
                raw_tag = None

        comment_open = line.rfind(HTML_COMMENT_OPEN)
        comment_close = line.rfind(HTML_COMMENT_CLOSE)
        if in_comment or comment_open >= 0:
            raw_literal = True
            in_comment = (
                comment_close < 0 if in_comment and comment_open < 0 else comment_open > comment_close
            )

        processing_open = line.rfind(PROCESSING_INSTRUCTION_OPEN)
        processing_close = line.rfind(PROCESSING_INSTRUCTION_CLOSE)
        if in_processing_instruction or processing_open >= 0:
            raw_literal = True
            in_processing_instruction = (
                processing_close < 0
                if in_processing_instruction and processing_open < 0
                else processing_open > processing_close
            )

        cdata_open = line.rfind(CDATA_OPEN)
        cdata_close = line.rfind(CDATA_CLOSE)
        if in_cdata or cdata_open >= 0:
            raw_literal = True
            in_cdata = cdata_close < 0 if in_cdata and cdata_open < 0 else cdata_open > cdata_close

        declaration_opens = list(re.finditer(r"<![A-Z]", line))
        declaration_open = declaration_opens[-1].start() if declaration_opens else -1
        declaration_close = line.rfind(">")
        if in_declaration or declaration_open >= 0:
            raw_literal = True
            in_declaration = (
                declaration_close < 0
                if in_declaration and declaration_open < 0
                else declaration_open > declaration_close
            )

        if raw_literal:
            literal[index] = True
            continue

        # Expand tabs to 4 columns to recognize mixed-indentation code blocks.
        columns = 0
        for char in line:
            if char == " ":
                columns += 1
            elif char == "\t":
                columns += 4 - (columns % 4)
            else:
                break
        literal[index] = columns >= 4
    return literal


def check_source(source: Path, collapse_keys: list[str]) -> list[str]:
    """Return one message per violating (key, line) pair in `source`."""
    lines = source.read_text(encoding="utf-8").split("\n")
    literal = literal_content_lines(lines)
    violations: list[str] = []
    collapse_set = set(collapse_keys)
    source_keys: set[str] = set()
    for key in sorted(collapse_set):
        if PLACEHOLDER_NAME_RE.fullmatch(key) is None:
            violations.append(f"{source}: `{key}` is not a valid placeholder key")
    for index, line in enumerate(lines):
        line_keys = set(PLACEHOLDER_RE.findall(line))
        source_keys.update(line_keys)
        present = line_keys & collapse_set
        if not present:
            continue
        for key in sorted(present):
            if literal[index]:
                reason = "occurs inside literal Markdown content"
            elif line_keys - collapse_set:
                reason = "shares its line with a placeholder that is not opted in"
            elif PLACEHOLDER_RE.sub("", line).strip(" \t\r"):
                reason = "shares its line with non-placeholder text"
            elif index > 0 and lines[index - 1].strip(" \t\r"):
                reason = "is not preceded by a blank separator"
            elif index + 1 < len(lines) and lines[index + 1].strip(" \t\r"):
                reason = "is not followed by a blank separator"
            else:
                continue
            violations.append(f"{source}:{index + 1}: `{key}` {reason}")
    for key in sorted(collapse_set - source_keys):
        if PLACEHOLDER_NAME_RE.fullmatch(key) is not None:
            violations.append(f"{source}: `{key}` has no placeholder occurrence")
    return violations


def manifest_targets(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every target in the manifest across all harnesses and the shared set."""
    collected: list[dict[str, Any]] = []
    for harness in (manifest.get("harnesses") or {}).values():
        collected.extend((harness or {}).get("targets") or [])
    collected.extend((manifest.get("shared") or {}).get("targets") or [])
    if not collected:
        raise ValueError(
            f"manifest yielded no targets: expected `harnesses:` and/or `shared:` "
            f"target lists, got top-level keys {sorted(manifest)!r}"
        )
    return collected


def main() -> int:
    manifest_path = REPO_ROOT / "scripts" / "sync-targets.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    violations: list[str] = []
    checked = 0
    for target in manifest_targets(manifest):
        collapse_keys = target.get("collapse_empty_substitutions") or []
        if target.get("delete") or not collapse_keys:
            continue
        checked += 1
        source = REPO_ROOT / target["source"]
        destination = str(target.get("destination") or "")
        if not destination.lower().endswith((".md", ".markdown")):
            for key in sorted(set(collapse_keys)):
                violations.append(
                    f"{source}: `{key}` targets non-Markdown destination {destination!r}"
                )
            continue
        violations.extend(check_source(source, collapse_keys))

    if violations:
        sys.stderr.write("collapse_empty_substitutions keys must be whole-line, prose-only:\n")
        for violation in violations:
            sys.stderr.write(f"  ❌ {violation}\n")
        return 1
    if not checked:
        sys.stderr.write(
            "no collapse_empty_substitutions opt-ins found in the manifest; "
            "this lint has nothing to verify and would pass vacuously\n"
        )
        return 1
    print(f"OK: collapse opt-ins verified in {checked} source(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
