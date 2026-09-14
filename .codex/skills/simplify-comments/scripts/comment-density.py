#!/usr/bin/env python3
"""Comment density audit and comment-only-edit verifier.

Audit (default) classifies every line of each source file as code, comment,
or blank and reports density = comment / (code + comment). A line holding any
code is code; a line holding only comment text, or lying inside a block
comment or docstring, is comment; anything else is blank.

Verify (--verify-against REF) compares each file's code fingerprint with the
same path at a git revision. The fingerprint is the token stream with comments
and layout removed (for Python, the AST without docstrings or `pass`), plus
the text of directive comments such as `@ts-expect-error` or `# type: ignore`,
whose removal changes what tools do. Exit status 1 means code changed.

Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import fnmatch
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CODE, COMMENT, BLANK = "code", "comment", "blank"


@dataclass(frozen=True)
class Dialect:
    """Lexical features of a C-family language that decide where comments are."""

    name: str
    nested_blocks: bool = False
    char_quotes: bool = True
    template_literals: bool = False
    regex_literals: bool = False
    raw_backticks: bool = False
    rust_literals: bool = False
    triple_quotes: bool = False


PYTHON = "python"
TYPESCRIPT = Dialect("typescript", template_literals=True, regex_literals=True)
JAVASCRIPT = Dialect("javascript", template_literals=True, regex_literals=True)
GO = Dialect("go", raw_backticks=True)
RUST = Dialect("rust", nested_blocks=True, char_quotes=False, rust_literals=True)
JAVA = Dialect("java", triple_quotes=True)
KOTLIN = Dialect("kotlin", nested_blocks=True, triple_quotes=True)
SWIFT = Dialect("swift", nested_blocks=True, char_quotes=False, triple_quotes=True)
CSHARP = Dialect("csharp", triple_quotes=True)

LANGUAGES: dict[str, Dialect | str] = {
    ".ts": TYPESCRIPT,
    ".tsx": TYPESCRIPT,
    ".mts": TYPESCRIPT,
    ".cts": TYPESCRIPT,
    ".js": JAVASCRIPT,
    ".jsx": JAVASCRIPT,
    ".mjs": JAVASCRIPT,
    ".cjs": JAVASCRIPT,
    ".py": PYTHON,
    ".go": GO,
    ".rs": RUST,
    ".java": JAVA,
    ".kt": KOTLIN,
    ".kts": KOTLIN,
    ".swift": SWIFT,
    ".cs": CSHARP,
}

DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "bower_components",
        "dist",
        "build",
        "vendor",
        "third_party",
        "target",
        ".venv",
        "venv",
        "__pycache__",
        ".next",
        ".nuxt",
        ".turbo",
        ".gradle",
        "coverage",
        "Pods",
        "DerivedData",
    }
)
GENERATED_NAMES = (
    "*.min.js",
    "*.min.mjs",
    "*.min.cjs",
    "*.bundle.js",
    "*.d.ts",
    "*.d.mts",
    "*.d.cts",
    "*.generated.*",
    "*.gen.*",
    "*_generated.*",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.pb.gw.go",
)
GENERATED_HEADER = re.compile(r"@generated\b|\bDO NOT EDIT\b|(?i:\bauto-?generated\b)")
GENERATED_HEADER_CHARS = 600

# Comments that change what a compiler, linter, bundler, or test runner does.
# Their text is part of the fingerprint, so removing or editing one fails
# verification even though no code token moved.
_DIRECTIVE_START = re.compile(
    r"@ts-(?:expect-error|ignore|nocheck|check)"
    r"|eslint-(?:disable|enable|env)|eslint\s+[\w@/-]+\s*:"
    r"|prettier-ignore|biome-ignore|oxlint-|deno-lint-ignore"
    r"|(?:istanbul|c8|v8) ignore|__(?:PURE|NO_SIDE_EFFECTS|INLINE|NOINLINE|KEY)__"
    r"|webpack[A-Z]|@vite-ignore|<reference\b|<amd-|go:[a-z]|\+build\b"
    r"|nolint|lint:(?:file-)?ignore|NOLINT|sourceMappingURL=|SAFETY:"
    r"|swiftlint:|ktlint-disable"
    r"|type:\s*ignore|noqa\b|pragma:|pylint:|mypy:|pyright:|ruff:"
    r"|fmt:\s*(?:off|on|skip)|isort:|nosec\b"
)
_DIRECTIVE_TAG = re.compile(
    r"@(?:license|preserve|jsx|jsxImportSource|jsxRuntime|jsxFrag|flow|noflow|format"
    r"|generated|internal|deprecated|public|alpha|beta|experimental"
    r"|type|typedef|satisfies|template|callback|overload|import)\b"
    r"|@(?:param|returns?|throws|property|prop)\s*\{"
    r"|\bcoding[:=]|\btype:\s*ignore\b|\bnoqa\b"
)

_REGEX_PREFIX_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "yield",
        "await",
    }
)
_REGEX_AFTER = frozenset("(,=:[!&|?{};+-*%>~^")
_RUST_RAW_STRING = re.compile(r'b?r(#*)"')
_RUST_CHAR = re.compile(r"'(?:\\(?:x[0-9A-Fa-f]{2}|u\{[0-9A-Fa-f]{1,6}\}|.)|[^\\'\n])'")


@dataclass
class Scan:
    kinds: list[str]
    fingerprint: str
    approximate: bool = False


def _is_word(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch in "_$")


def _directive_lines(raw: str) -> list[str]:
    """Whitespace-normalized directive text found in one raw comment."""
    if raw.startswith("/*!"):
        return [" ".join(raw.split())]
    found = []
    for line in raw.split("\n"):
        text = line.strip().lstrip("/*!#").rstrip("*/").strip()
        if text and (_DIRECTIVE_START.match(text) or _DIRECTIVE_TAG.search(text)):
            found.append(" ".join(text.split()))
    return found


def _split_lines(text: str) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


class _CFamilyScanner:
    """Single-pass lexer for `//` and `/* */` languages.

    Literals are lexed only far enough to know that comment markers inside
    them are not comments; their contents enter the fingerprint verbatim.
    Whitespace between code tokens is kept only where it separates two word
    characters, a trailing comma before a closing bracket is dropped, and so
    are parentheses wrapping a whole `return` or `throw` expression, so a
    formatter reflowing lines after a comment deletion does not register as a
    code change.
    """

    def __init__(self, text: str, dialect: Dialect) -> None:
        self.text = text
        self.dialect = dialect
        self.kinds: list[str] = []
        self.tokens: list[str] = []
        self.line_code = False
        self.line_comment = False
        self.pending_space = False
        self.last_sig = ""
        self.word = ""
        # Open parens as (token index, directly follows `return`/`throw`), and
        # the (open, close) indices of such a pair awaiting its next token.
        self.parens: list[tuple[int, bool]] = []
        self.unwrap: tuple[int, int] | None = None

    def scan(self) -> Scan:
        self._code(0, interpolation=False)
        if self.text and not self.text.endswith("\n"):
            self._end_line()
        return Scan(self.kinds, "".join(self.tokens))

    def _end_line(self) -> None:
        if self.line_code:
            self.kinds.append(CODE)
        elif self.line_comment:
            self.kinds.append(COMMENT)
        else:
            self.kinds.append(BLANK)
        self.line_code = self.line_comment = False

    def _emit_code(self, ch: str) -> None:
        tokens = self.tokens
        unwrap, self.unwrap = self.unwrap, None
        if unwrap and ch in ";}":
            opening, closing = unwrap
            del tokens[closing]
            if _is_word(tokens[opening + 1][:1] if opening + 1 < len(tokens) else ""):
                tokens[opening] = " "
            else:
                del tokens[opening]
        if self.pending_space and tokens and _is_word(tokens[-1][-1:]) and _is_word(ch):
            tokens.append(" ")
        if (
            ch in ")]}"
            and tokens
            and tokens[-1] == ","
            and (len(tokens) < 2 or tokens[-2] not in (",", "[", "(", "{"))
        ):
            tokens.pop()
        if ch == "(":
            after_keyword = _is_word(self.last_sig) and self.word in ("return", "throw")
            self.parens.append((len(tokens), after_keyword))
        if _is_word(ch):
            joined = _is_word(self.last_sig) and not self.pending_space
            self.word = self.word + ch if joined else ch
        else:
            self.word = ""
        tokens.append(ch)
        if ch == ")" and self.parens:
            opening, after_keyword = self.parens.pop()
            if after_keyword:
                self.unwrap = (opening, len(tokens) - 1)
        self.pending_space = False
        self.line_code = True
        self.last_sig = ch

    def _emit_literal(self, text: str) -> None:
        self.tokens.append(text)
        self.unwrap = None
        self.pending_space = False
        self.line_code = True

    def _after_value(self) -> None:
        self.last_sig = ")"
        self.word = ""

    def _newline_in_literal(self) -> None:
        self.line_code = True
        self._end_line()
        self.tokens.append("\n")

    def _code(self, i: int, *, interpolation: bool) -> int:
        """Lex code from `i`. Inside `${...}`, return at the unmatched `}`."""
        text, d = self.text, self.dialect
        n = len(text)
        depth = 0
        while i < n:
            ch = text[i]
            if ch == "\n":
                self._end_line()
                self.pending_space = True
                i += 1
                continue
            if ch.isspace():
                self.pending_space = True
                i += 1
                continue
            nxt = text[i + 1 : i + 2]
            if ch == "/" and nxt == "/":
                i = self._line_comment(i)
            elif ch == "/" and nxt == "*":
                i = self._block_comment(i)
            elif d.triple_quotes and text.startswith('"""', i):
                i = self._literal(i, '"""', '"""', escapes=True, multiline=True)
            elif (
                d.rust_literals
                and ch in "rb"
                and not (i and _is_word(text[i - 1]))
                and (raw := _RUST_RAW_STRING.match(text, i))
            ):
                i = self._literal(i, raw.group(0), '"' + raw.group(1), escapes=False, multiline=True)
            elif ch == '"':
                i = self._literal(i, '"', '"', escapes=True, multiline=d.rust_literals)
            elif ch == "'" and d.rust_literals:
                char = _RUST_CHAR.match(text, i)
                if char:
                    self._emit_literal(char.group(0))
                    self._after_value()
                    i = char.end()
                else:
                    self._emit_code(ch)
                    i += 1
            elif ch == "'" and d.char_quotes:
                i = self._literal(i, "'", "'", escapes=True, multiline=False)
            elif ch == "`" and d.template_literals:
                i = self._template(i)
            elif ch == "`" and d.raw_backticks:
                i = self._literal(i, "`", "`", escapes=False, multiline=True)
            elif ch == "/" and d.regex_literals and self._regex_allowed():
                i = self._regex(i)
            else:
                if interpolation:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        if depth == 0:
                            return i
                        depth -= 1
                self._emit_code(ch)
                i += 1
        return i

    def _literal(self, i: int, opener: str, closer: str, *, escapes: bool, multiline: bool) -> int:
        text = self.text
        n = len(text)
        self._emit_literal(opener)
        i += len(opener)
        while i < n:
            if text.startswith(closer, i):
                self._emit_literal(closer)
                self._after_value()
                return i + len(closer)
            ch = text[i]
            if ch == "\n":
                if not multiline:
                    self._after_value()
                    return i
                self._newline_in_literal()
                i += 1
            elif escapes and ch == "\\" and i + 1 < n:
                if text[i + 1] == "\n":
                    self._emit_literal("\\")
                    self._newline_in_literal()
                else:
                    self._emit_literal(text[i : i + 2])
                i += 2
            else:
                self._emit_literal(ch)
                i += 1
        return i

    def _template(self, i: int) -> int:
        text = self.text
        n = len(text)
        self._emit_literal("`")
        i += 1
        while i < n:
            ch = text[i]
            if ch == "`":
                self._emit_literal("`")
                self._after_value()
                return i + 1
            if ch == "\\" and i + 1 < n:
                if text[i + 1] == "\n":
                    self._emit_literal("\\")
                    self._newline_in_literal()
                else:
                    self._emit_literal(text[i : i + 2])
                i += 2
            elif text.startswith("${", i):
                self._emit_literal("${")
                self.last_sig, self.word = "{", ""
                i = self._code(i + 2, interpolation=True)
                if i < n:
                    self._emit_literal("}")
                    i += 1
            elif ch == "\n":
                self._newline_in_literal()
                i += 1
            else:
                self._emit_literal(ch)
                i += 1
        return i

    def _regex_allowed(self) -> bool:
        if not self.last_sig:
            return True
        if _is_word(self.last_sig):
            return self.word in _REGEX_PREFIX_KEYWORDS
        return self.last_sig in _REGEX_AFTER

    def _regex(self, i: int) -> int:
        text = self.text
        n = len(text)
        self._emit_literal("/")
        i += 1
        in_class = False
        while i < n and text[i] != "\n":
            ch = text[i]
            if ch == "\\" and i + 1 < n and text[i + 1] != "\n":
                self._emit_literal(text[i : i + 2])
                i += 2
                continue
            self._emit_literal(ch)
            i += 1
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                break
        self._after_value()
        return i

    def _line_comment(self, i: int) -> int:
        end = self.text.find("\n", i)
        if end < 0:
            end = len(self.text)
        self._comment(self.text[i:end])
        return end

    def _block_comment(self, i: int) -> int:
        text = self.text
        n = len(text)
        if not self.dialect.nested_blocks:
            close = text.find("*/", i + 2)
            end = n if close < 0 else close + 2
        else:
            end, depth = i + 2, 1
            while depth:
                close = text.find("*/", end)
                if close < 0:
                    end = n
                    break
                opening = text.find("/*", end)
                if 0 <= opening < close:
                    depth += 1
                    end = opening + 2
                else:
                    depth -= 1
                    end = close + 2
        raw = text[i:end]
        self.line_comment = True
        for _ in range(raw.count("\n")):
            self._end_line()
            self.line_comment = True
        self._comment(raw)
        return end

    def _comment(self, raw: str) -> None:
        self.line_comment = True
        for directive in _directive_lines(raw):
            self.tokens.append(f"«{directive}»")
        self.pending_space = True


_PY_LAYOUT = frozenset(
    {
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
)


def _is_string_statement(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _char_col(lines: list[str], row: int, byte_col: int) -> int:
    line = lines[row - 1] if 0 < row <= len(lines) else ""
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))


class _PythonNormalizer(ast.NodeTransformer):
    """Drop statements that cannot change behaviour: docstrings, `pass`, bare `...`.

    A docstring holding a doctest stays, and so does every docstring in a file
    that reads `__doc__`.
    """

    def __init__(self, keep_docstrings: bool) -> None:
        self.keep_docstrings = keep_docstrings

    def _inert(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Pass):
            return True
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
            return False
        value = node.value.value
        if value is Ellipsis:
            return True
        return isinstance(value, str) and not self.keep_docstrings and ">>>" not in value

    def generic_visit(self, node: ast.AST) -> ast.AST:
        super().generic_visit(node)
        for name in ("body", "orelse", "finalbody"):
            statements = getattr(node, name, None)
            if isinstance(statements, list):
                setattr(node, name, [s for s in statements if not self._inert(s)])
        return node


def _approximate_python(lines: list[str]) -> Scan:
    kinds = [
        BLANK if not line.strip() else COMMENT if line.lstrip().startswith("#") else CODE
        for line in lines
    ]
    return Scan(kinds, "", approximate=True)


def scan_python(text: str) -> Scan:
    lines = _split_lines(text)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return _approximate_python(lines)
    try:
        tree: ast.Module | None = ast.parse(text)
    except (SyntaxError, ValueError):
        tree = None

    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    if tree is not None:
        for node in ast.walk(tree):
            if _is_string_statement(node) and isinstance(node, ast.Expr):
                end_row = node.end_lineno or node.lineno
                end_col = node.end_col_offset or 0
                spans.append(
                    (
                        (node.lineno, _char_col(lines, node.lineno, node.col_offset)),
                        (end_row, _char_col(lines, end_row, end_col)),
                    )
                )
        spans.sort()
    starts = [start for start, _ in spans]

    def in_docstring(start: tuple[int, int], end: tuple[int, int]) -> bool:
        index = bisect.bisect_right(starts, start) - 1
        return index >= 0 and end <= spans[index][1]

    code: set[int] = set()
    comment: set[int] = set()
    directives: list[str] = []
    for tok in tokens:
        start_row, end_row = tok.start[0], tok.end[0]
        if tok.type == tokenize.COMMENT:
            comment.add(start_row)
            if start_row == 1 and tok.string.startswith("#!"):
                directives.append(tok.string.strip())
            else:
                directives.extend(_directive_lines(tok.string))
        elif tok.type in _PY_LAYOUT or not tok.string.strip():
            continue
        elif tok.type == tokenize.STRING and in_docstring(tok.start, tok.end):
            comment.update(range(start_row, end_row + 1))
        else:
            code.update(range(start_row, end_row + 1))

    kinds = []
    for number, line in enumerate(lines, start=1):
        if number in code:
            kinds.append(CODE)
        elif number in comment:
            kinds.append(COMMENT)
        else:
            kinds.append(CODE if line.strip() else BLANK)
    if tree is None:
        return Scan(kinds, "", approximate=True)
    normalized = _PythonNormalizer(keep_docstrings="__doc__" in text).visit(tree)
    fingerprint = ast.dump(normalized) + "".join(f"«{d}»" for d in directives)
    return Scan(kinds, fingerprint)


def scan_text(text: str, language: Dialect | str) -> Scan:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    if isinstance(language, Dialect):
        return _CFamilyScanner(text, language).scan()
    return scan_python(text)


def language_name(language: Dialect | str) -> str:
    return language.name if isinstance(language, Dialect) else language


def density(code: int, comment: int) -> float:
    return round(comment * 100 / (code + comment), 2) if code + comment else 0.0


@dataclass
class Source:
    path: Path
    display: str
    language: Dialect | str
    text: str


@dataclass
class FileStats:
    path: str
    language: str
    code: int
    comment: int
    blank: int
    approximate: bool

    @property
    def total(self) -> int:
        return self.code + self.comment + self.blank

    @property
    def density(self) -> float:
        return density(self.code, self.comment)

    def as_json(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "path": self.path,
            "language": self.language,
            "code": self.code,
            "comment": self.comment,
            "blank": self.blank,
            "total": self.total,
            "density": self.density,
        }
        if self.approximate:
            record["approximate"] = True
        return record


def measure(source: Source) -> FileStats:
    scan = scan_text(source.text, source.language)
    counts = Counter(scan.kinds)
    return FileStats(
        source.display,
        language_name(source.language),
        counts[CODE],
        counts[COMMENT],
        counts[BLANK],
        scan.approximate,
    )


def display_path(path: Path) -> str:
    try:
        relative = os.path.relpath(path)
    except ValueError:
        relative = str(path)
    return relative.replace(os.sep, "/")


def _matches(display: str, patterns: list[str]) -> bool:
    name = display.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(display, p) or fnmatch.fnmatch(name, p) for p in patterns)


def _walk(root: Path, excludes: list[str], default_excludes: bool) -> Iterator[Path]:
    for current, dirs, files in os.walk(root):
        base = Path(current)
        dirs[:] = sorted(
            d
            for d in dirs
            if not (default_excludes and d in DEFAULT_EXCLUDED_DIRS)
            and not _matches(display_path(base / d), excludes)
        )
        for name in sorted(files):
            yield base / name


def iter_sources(
    paths: list[str],
    extensions: set[str],
    globs: list[str],
    excludes: list[str],
    default_excludes: bool,
    skipped: Counter[str],
) -> Iterator[Source]:
    """Yield readable source files; count why selected-extension files were skipped.

    A file named directly on the command line bypasses every filter except
    the language check.
    """
    seen: set[Path] = set()
    for raw in paths:
        root = Path(raw)
        explicit = root.is_file()
        candidates = [root] if explicit else _walk(root, excludes, default_excludes)
        for path in candidates:
            language = LANGUAGES.get(path.suffix.lower())
            if explicit and language is None:
                skipped["unsupported"] += 1
                continue
            if not explicit and (language is None or path.suffix.lower() not in extensions):
                continue
            assert language is not None
            display = display_path(path)
            if not explicit:
                if globs and not _matches(display, globs):
                    continue
                if _matches(display, excludes):
                    skipped["excluded"] += 1
                    continue
                if path.is_symlink():
                    skipped["symlink"] += 1
                    continue
                if default_excludes and _matches(display, list(GENERATED_NAMES)):
                    skipped["generated"] += 1
                    continue
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            try:
                data = path.read_bytes()
            except OSError:
                skipped["unreadable"] += 1
                continue
            if b"\0" in data:
                skipped["binary"] += 1
                continue
            text = data.decode("utf-8", errors="replace")
            if not explicit and default_excludes:
                if GENERATED_HEADER.search(text[:GENERATED_HEADER_CHARS]):
                    skipped["generated"] += 1
                    continue
                if len(text) > 2000 and len(text) / (text.count("\n") + 1) > 300:
                    skipped["minified"] += 1
                    continue
            yield Source(path, display, language, text)


def _summary(stats: list[FileStats]) -> dict[str, Any]:
    code = sum(s.code for s in stats)
    comment = sum(s.comment for s in stats)
    blank = sum(s.blank for s in stats)
    return {
        "files": len(stats),
        "code": code,
        "comment": comment,
        "blank": blank,
        "total": code + comment + blank,
        "density": density(code, comment),
    }


SORT_KEYS = {
    "comments": lambda s: (s.comment, s.density),
    "density": lambda s: (s.density, s.comment),
    "total": lambda s: (s.total, s.comment),
}


def run_audit(args: argparse.Namespace, sources: Iterator[Source], skipped: Counter[str]) -> int:
    stats = [measure(source) for source in sources]
    by_language: dict[str, list[FileStats]] = {}
    for s in stats:
        by_language.setdefault(s.language, []).append(s)
    matching = [s for s in stats if s.density >= args.min_density and s.total >= args.min_lines]
    matching.sort(key=SORT_KEYS[args.sort], reverse=True)
    listed = matching[: args.top] if args.top else matching
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "audit",
        "totals": _summary(stats),
        "by_language": {name: _summary(group) for name, group in sorted(by_language.items())},
        "skipped": dict(sorted(skipped.items())),
        "filters": {
            "min_density": args.min_density,
            "min_lines": args.min_lines,
            "sort": args.sort,
            "top": args.top,
        },
        "matched": len(matching),
        "files": [s.as_json() for s in listed],
    }
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    color = _use_color(args)
    totals = report["totals"]
    print(
        f"Comment density {totals['density']:.1f}% across {totals['files']:,} files: "
        f"{totals['code']:,} code, {totals['comment']:,} comment, {totals['blank']:,} blank lines"
    )
    for name, summary in report["by_language"].items():
        print(f"  {name:<11} {summary['files']:>6,} files  {summary['density']:>5.1f}%")
    if skipped:
        print("Skipped: " + ", ".join(f"{count:,} {reason}" for reason, count in report["skipped"].items()))
    if not listed:
        print("\nNo files matched.")
        return 0
    print(f"\n  {'DENSITY':>8}  {'COMMENT':>8}  {'CODE':>8}  {'TOTAL':>8}  PATH")
    for s in listed:
        hot = s.density >= args.highlight
        row = (
            f"{'*' if hot else ' '} {s.density:>7.1f}%  {s.comment:>8,}  {s.code:>8,}  "
            f"{s.total:>8,}  {s.path}{'  (approximate)' if s.approximate else ''}"
        )
        print(f"\033[33m{row}\033[0m" if hot and color else row)
    print(
        f"\nShowing {len(listed):,} of {len(matching):,} matching files, sorted by {args.sort}. "
        f"* marks density >= {args.highlight:g}%."
    )
    return 0


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )


def _baseline(path: Path, ref: str) -> tuple[str | None, str | None]:
    """(text, error) for `path` at `ref`; both None when the path is absent there."""
    proc = _git(["show", f"{ref}:./{path.name}"], path.parent)
    if proc.returncode == 0:
        return proc.stdout.decode("utf-8", errors="replace"), None
    message = proc.stderr.decode("utf-8", errors="replace").strip()
    if "does not exist in" in message or "exists on disk, but not in" in message:
        return None, None
    return None, (message.splitlines()[0] if message else f"git exited {proc.returncode}")


def _divergence(before: str, after: str, width: int = 40) -> dict[str, str]:
    index = len(os.path.commonprefix([before, after]))
    start = max(0, index - width)

    def excerpt(text: str) -> str:
        return text[start : index + width].replace("\n", "\\n")

    return {"before": excerpt(before), "after": excerpt(after)}


def _counts(scan: Scan) -> dict[str, Any]:
    counts = Counter(scan.kinds)
    return {
        "total": len(scan.kinds),
        "comment": counts[COMMENT],
        "density": density(counts[CODE], counts[COMMENT]),
    }


def verify_source(source: Source, ref: str) -> dict[str, Any]:
    result: dict[str, Any] = {"path": source.display}
    after = scan_text(source.text, source.language)
    baseline, error = _baseline(source.path, ref)
    if error:
        return {**result, "status": "error", "detail": error}
    if baseline is None:
        return {**result, "status": "added"}
    before = scan_text(baseline, source.language)
    result.update(before=_counts(before), after=_counts(after))
    if before.approximate or after.approximate:
        return {**result, "status": "error", "detail": "could not parse"}
    if before.fingerprint == after.fingerprint:
        return {**result, "status": "unchanged"}
    return {**result, "status": "changed", "divergence": _divergence(before.fingerprint, after.fingerprint)}


def run_verify(args: argparse.Namespace, sources: Iterator[Source], skipped: Counter[str]) -> int:
    first = Path(args.paths[0])
    probe = _git(
        ["rev-parse", "--verify", "--quiet", f"{args.verify_against}^{{commit}}"],
        first if first.is_dir() else first.parent,
    )
    if probe.returncode != 0:
        print(f"comment-density.py: unknown git revision: {args.verify_against}", file=sys.stderr)
        return 2
    results = [verify_source(source, args.verify_against) for source in sources]
    statuses = Counter(r["status"] for r in results)
    failed = sum(count for status, count in statuses.items() if status != "unchanged")
    if args.json:
        report = {
            "schema_version": SCHEMA_VERSION,
            "mode": "verify",
            "ref": args.verify_against,
            "skipped": dict(sorted(skipped.items())),
            "statuses": dict(sorted(statuses.items())),
            "files": results,
        }
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        summary = ", ".join(f"{count} {status}" for status, count in sorted(statuses.items()))
        print(f"Verified {len(results)} files against {args.verify_against}: {summary or 'none'}.")
        for r in results:
            metrics = ""
            if "before" in r:
                removed = r["before"]["total"] - r["after"]["total"]
                metrics = f"{r['before']['density']:>5.1f}% -> {r['after']['density']:>5.1f}%  {-removed:+,} lines  "
            print(f"  {r['status']:<9}  {metrics}{r['path']}")
            if "divergence" in r:
                print(f"             before: …{r['divergence']['before']}…")
                print(f"             after:  …{r['divergence']['after']}…")
            if "detail" in r:
                print(f"             {r['detail']}")
    return 1 if failed else 0


def _use_color(args: argparse.Namespace) -> bool:
    return not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="comment-density.py",
        description="Measure comment density, or verify that an edit changed only comments.",
    )
    parser.add_argument("paths", nargs="*", default=["."], help="files or directories (default: .)")
    parser.add_argument(
        "--ext",
        action="append",
        default=[],
        metavar="EXT",
        help=f"extensions to scan, comma-separated or repeated (default: all of {', '.join(LANGUAGES)})",
    )
    parser.add_argument(
        "--glob",
        action="append",
        default=[],
        metavar="PATTERN",
        help="only scan files whose displayed path or name matches (fnmatch; * crosses directories)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="skip files and directories whose displayed path or name matches",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="also scan dependency, build, vendored, generated, and minified files",
    )
    parser.add_argument("--min-density", type=float, default=0.0, metavar="PCT", help="list only files at or above PCT")
    parser.add_argument("--min-lines", type=int, default=0, metavar="N", help="list only files with at least N lines")
    parser.add_argument("--sort", choices=sorted(SORT_KEYS), default="comments", help="ranking (default: comments)")
    parser.add_argument("--top", type=int, default=25, metavar="N", help="list at most N files; 0 lists all (default: 25)")
    parser.add_argument("--highlight", type=float, default=25.0, metavar="PCT", help="mark files at or above PCT (default: 25)")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")
    parser.add_argument(
        "--verify-against",
        metavar="REF",
        help="compare each file's code fingerprint with REF; exit 1 if any code changed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    extensions = {
        (ext if ext.startswith(".") else f".{ext}").lower()
        for value in args.ext
        for ext in value.split(",")
        if ext.strip()
    } or set(LANGUAGES)
    unsupported = sorted(extensions - set(LANGUAGES))
    if unsupported:
        parser.error(f"unsupported extension(s): {', '.join(unsupported)}")
    if args.verify_against is not None and (not args.verify_against or args.verify_against.startswith("-")):
        parser.error("--verify-against needs a revision that does not start with '-'")
    for raw in args.paths:
        if not os.path.exists(raw):
            parser.error(f"no such file or directory: {raw}")
    skipped: Counter[str] = Counter()
    sources = iter_sources(
        args.paths, extensions, args.glob, args.exclude, not args.no_default_excludes, skipped
    )
    if args.verify_against is not None:
        return run_verify(args, sources, skipped)
    return run_audit(args, sources, skipped)


if __name__ == "__main__":
    sys.exit(main())
