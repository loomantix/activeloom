#!/usr/bin/env python3
"""Check every `collapse_empty_substitutions` key against its template sites.

`drop_empty_placeholder_lines` in `sync-engine.py` deletes a qualifying line
plus one adjacent blank, and it decides purely from the line's own bytes — the
engine carries no Markdown knowledge, by design, because a whole-file normalizer
cannot tell an author-written blank line from a placeholder-produced one without
re-parsing the document. That leaves one gap: a key opted into collapsing whose
template occurrence sits inside a fenced block, a raw `<pre>`, or a four-space
indented block would silently delete a line of literal content from every
consumer's rendered file.

Close it here rather than in the engine. Reading the template is cheap at lint
time, a violation is always an authoring mistake in this repo's own manifest,
and keeping the check out of the engine preserves the property the fix depends
on: rendering is byte-faithful and knows nothing about Markdown.

A key passes when every one of its `<<KEY>>` occurrences is alone on its line
(ignoring other placeholders and horizontal whitespace) and outside literal
content. Exits 1 listing every violation. Paths resolve against the repo root,
so the working directory does not matter.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_RE = re.compile(r"<<([A-Z0-9_]+)>>")
FENCE_RE = re.compile(r"\A {0,3}(`{3,}|~{3,})")
PRE_OPEN_RE = re.compile(r"<pre\b", re.IGNORECASE)
PRE_CLOSE_RE = re.compile(r"</pre\s*>", re.IGNORECASE)


def literal_content_lines(lines: list[str]) -> list[bool]:
    """Mark each line that a Markdown reader would treat as literal content.

    Deliberately over-marks: an indented line is flagged whether or not a
    preceding blank line makes it a real CommonMark indented code block, and a
    fence is closed by any fence of the same character. A lint that is too
    strict costs one manifest comment; one that is too loose costs a consumer a
    deleted line.
    """
    literal = [False] * len(lines)
    fence: tuple[str, int] | None = None
    in_pre = False
    for index, line in enumerate(lines):
        fence_match = FENCE_RE.match(line)
        if fence is None and fence_match is not None:
            fence = (fence_match.group(1)[0], len(fence_match.group(1)))
            literal[index] = True
            continue
        if fence is not None:
            literal[index] = True
            char, length = fence
            if fence_match is not None and fence_match.group(1)[0] == char and len(fence_match.group(1)) >= length:
                fence = None
            continue
        if in_pre:
            literal[index] = True
            if PRE_CLOSE_RE.search(line):
                in_pre = False
            continue
        if PRE_OPEN_RE.search(line):
            literal[index] = True
            in_pre = not PRE_CLOSE_RE.search(line)
            continue
        literal[index] = line.startswith("    ") or line.startswith("\t")
    return literal


def check_source(source: Path, collapse_keys: list[str]) -> list[str]:
    """Return one message per violating (key, line) pair in `source`."""
    lines = source.read_text(encoding="utf-8").split("\n")
    literal = literal_content_lines(lines)
    violations: list[str] = []
    for index, line in enumerate(lines):
        present = set(PLACEHOLDER_RE.findall(line)) & set(collapse_keys)
        if not present:
            continue
        for key in sorted(present):
            if literal[index]:
                reason = "occurs inside literal content (fence, <pre>, or indented block)"
            elif PLACEHOLDER_RE.sub("", line).strip(" \t\r"):
                reason = "shares its line with non-placeholder text"
            else:
                continue
            violations.append(f"{source}:{index + 1}: `{key}` {reason}")
    return violations


def main() -> int:
    manifest_path = REPO_ROOT / "scripts" / "sync-targets.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    violations: list[str] = []
    checked = 0
    for target in manifest.get("targets") or []:
        collapse_keys = target.get("collapse_empty_substitutions") or []
        if target.get("delete") or not collapse_keys:
            continue
        checked += 1
        violations.extend(check_source(REPO_ROOT / target["source"], collapse_keys))

    if violations:
        sys.stderr.write("collapse_empty_substitutions keys must be whole-line, prose-only:\n")
        for violation in violations:
            sys.stderr.write(f"  ❌ {violation}\n")
        return 1
    print(f"OK: collapse opt-ins verified in {checked} source(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
