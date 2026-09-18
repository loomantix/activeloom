#!/usr/bin/env python3
"""Sync canonical files from an upstream repo into a consumer repo.

Reads `scripts/sync-targets.yml` from the upstream checkout to learn which
files belong to which destinations and which placeholders need substitution.
The manifest emits one target set per harness plus a harness-independent
`shared:` set; a consumer receives the shared set plus the sets of the
harnesses it declares.

Reads `.activeloom-config.yml` from the consumer to learn which harnesses it
runs, to resolve placeholders, and to read the gates (`skip_targets`,
`allowed_destinations`, `allow_sensitive_writes`) that bound what may be
written. Writes substituted files into the consumer working directory.

A target with `delete: true` instead causes the engine to *unlink* the
destination on the consumer (idempotent; no-op if already absent), then
prune empty parent directories up to the consumer root. Use this to
retire files that were previously synced — without it, deprecated stubs
linger forever as dead bytes on consumer disks.

Run from the consumer repo's CI (via `sync-from-upstream.yml.template`) or
locally for testing:

    python3 /tmp/upstream/scripts/sync-engine.py \\
        --upstream-repo /tmp/upstream \\
        --consumer-dir .

Exit codes:
    0  success (changes may or may not have been written; check `git diff`)
    1  config or input error (missing required placeholder, malformed YAML, etc.)
    2  invocation error (bad arguments, missing files)
"""
from __future__ import annotations

import argparse
import errno
import os
import re
import stat
import sys
import tempfile
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Required, TypedDict

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "PyYAML is required. Install with `pip install pyyaml` or "
        "ensure the consumer's sync workflow does so before invoking.\n"
    )
    sys.exit(2)


class Target(TypedDict, total=False):
    """One entry in `scripts/sync-targets.yml`.

    Either a copy target (requires `source` + `destination`) or a delete
    target (requires `destination` + `delete: True`). `substitutions`,
    `collapse_empty_substitutions`, and `mode` apply to copy targets only;
    every `collapse_empty_substitutions` key must also appear in
    `substitutions`.

    `create_if_missing: True` on a copy target makes the engine bootstrap
    the destination on first sync and then leave it alone — preserving any
    consumer customization on subsequent syncs. Mutually exclusive with
    `delete`.

    The schema is documented here for readers; the engine still validates
    each field at runtime since YAML provides no type guarantees.
    """

    source: str
    destination: Required[str]
    substitutions: list[str]
    collapse_empty_substitutions: list[str]
    mode: str | int
    delete: bool
    create_if_missing: bool


class HarnessConfig(TypedDict, total=False):
    """One entry under a consumer config's `harnesses:` mapping.

    Every key is optional. An empty (or null) harness entry means "sync this
    harness under the config's top-level gates", which is the shape the
    onboarding wizard writes for a fresh consumer.
    """

    substitutions: dict[str, object]
    skip_targets: list[str]
    allowed_destinations: list[str]
    allow_sensitive_writes: list[str]


class ConsumerConfig(TypedDict, total=False):
    """Top-level shape of a consumer's `.activeloom-config.yml`.

    `harnesses` accepts either form:

        harnesses: [claude, codex]          # no per-harness overrides
        harnesses:                          # per-harness gates
          claude:
            allowed_destinations: [.claude/**]
          codex: {}

    The remaining keys are the harness-independent defaults. `substitutions`,
    `skip_targets`, and `allow_sensitive_writes` compose *with* a harness's
    own values; `allowed_destinations` is *overridden* by a harness that
    declares one. See `resolve_scopes` for why those differ.
    """

    harnesses: list[str] | dict[str, object]
    substitutions: dict[str, object]
    skip_targets: list[str]
    allowed_destinations: list[str]
    allow_sensitive_writes: list[str]
    telemetry: dict[str, object]


CANONICAL_CONFIG_NAME: Final[str] = ".activeloom-config.yml"

# Pre-sync-v2 per-harness config filenames. The engine no longer reads them.
# They survive here for two reasons: a consumer that still carries one gets an
# error naming the file it has instead of a bare "file not found", and they
# stay unwritable destinations so an upstream manifest cannot author one.
RETIRED_CONFIG_NAMES: Final[tuple[str, ...]] = (
    ".platform-config.yml",
    ".codex-platform-config.yml",
    ".gemini-platform-config.yml",
)

# Substitution keys computed and injected by the engine.
RESERVED_SUBSTITUTION_KEYS: Final[frozenset[str]] = frozenset({"REVIEW_TELEMETRY_ENV"})

TELEMETRY_GATES: Final[dict[str, str]] = {
    "emit": "LOOM_REVIEW_TELEMETRY",
    "extract": "LOOM_REVIEW_TELEMETRY_EXTRACT",
}

TELEMETRY_VALUES: Final[frozenset[str]] = frozenset({"on", "off"})

KNOWN_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(ConsumerConfig.__annotations__)
KNOWN_HARNESS_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(HarnessConfig.__annotations__)


@dataclass(frozen=True)
class Scope:
    """The gates and values that govern one slice of the sync plan.

    One scope per declared harness, plus one for the manifest's `shared:`
    set. Holding them per slice rather than per run is what lets a consumer
    grant `.codex/**` to the Codex harness without also granting it to the
    Claude one — the property the three separate config files used to provide
    by being three separate sync runs.
    """

    label: str
    values: dict[str, object]
    skip: set[str]
    # None indicates fail-open when no allowed_destinations are configured.
    allowed_patterns: list[re.Pattern[str]] | None
    sensitive_write_allowlist: frozenset[str]
    # Harness name and its un-hoisted direct sensitive-write grants for error refusal formatting.
    harness_name: str | None
    own_sensitive_write_allowlist: frozenset[str]


PLACEHOLDER_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
PLACEHOLDER_RE = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")

KNOWN_TARGET_FIELDS: Final[frozenset[str]] = frozenset(Target.__annotations__)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a gitignore-flavored glob pattern to an anchored regex.

    Cases handled:
        `**/`  (start or mid-pattern) → zero or more leading/intermediate
               path segments. So `**/foo.md` matches `foo.md`, `a/foo.md`,
               and `a/b/c/foo.md`; `a/**/b` matches `a/b` and `a/x/y/b`.
        trailing `**` → matches the rest of the path (any characters,
               including `/`). So `.claude/skills/**` matches
               `.claude/skills/critique/SKILL.md` but NOT `.claude/skills`
               itself (the trailing `/` in the pattern is required).
        bare `**` without an adjacent `/` → treated as `.*` (any-chars).
        `*` → any run of characters except `/`.
        `?` → a single character except `/`.

    Everything else is escaped as literal — regex metachars like `.` and
    `+` are not honored. The returned pattern is anchored at both ends —
    partial matches do not count. Used by both the consumer-side
    `allowed_destinations` allowlist and the engine-level
    `SENSITIVE_DELETE_PATTERNS` constant.
    """
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            # **/ matches zero or more segments; bare ** matches any characters.
            if i + 2 < len(pattern) and pattern[i + 2] == "/":
                parts.append("(?:.*/)?")
                i += 3
            else:
                parts.append(".*")
                i += 2
        elif c == "*":
            parts.append("[^/]*")
            i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return re.compile(r"\A" + "".join(parts) + r"\Z")


def path_matches_any(path: str, compiled_patterns: Sequence[re.Pattern[str]]) -> bool:
    """Return True if `path` matches any of the pre-compiled glob patterns."""
    return any(p.match(path) is not None for p in compiled_patterns)


# Sensitive paths the engine refuses to delete from a consumer tree.
SENSITIVE_DELETE_PATTERNS: Final[tuple[str, ...]] = (
    ".github/workflows/**",
    ".github/actions/**",
    "**/CODEOWNERS",
    "**/package.json",
    "**/pnpm-lock.yaml",
    "**/prisma/schema.prisma",
    "**/Dockerfile",
    "**/Dockerfile.*",
)
def _compile_case_insensitive(pattern: str) -> re.Pattern[str]:
    """Recompile a glob pattern with `re.IGNORECASE`.

    Sensitive paths must be blocked even on case-insensitive filesystems
    (macOS APFS, NTFS) where `dockerfile` resolves to the same on-disk
    file as `Dockerfile`. Fleet sync runs on `ubuntu-latest` (case-
    sensitive ext4), so this is defense-in-depth for self-hosted or
    macos-/windows-latest runners.
    """
    return re.compile(glob_to_regex(pattern).pattern, re.IGNORECASE)


SENSITIVE_DELETE_REGEXES: Final[tuple[re.Pattern[str], ...]] = tuple(
    _compile_case_insensitive(p) for p in SENSITIVE_DELETE_PATTERNS
)

# Paths requiring explicit consumer opt-in via allow_sensitive_writes.
SENSITIVE_WRITE_PATTERNS: Final[tuple[str, ...]] = SENSITIVE_DELETE_PATTERNS

SENSITIVE_WRITE_REGEXES: Final[tuple[re.Pattern[str], ...]] = tuple(
    _compile_case_insensitive(p) for p in SENSITIVE_WRITE_PATTERNS
)

# Engine prompt surface directories carved out of sensitive path checks.
ENGINE_SURFACE_PATTERNS: Final[tuple[str, ...]] = (
    ".claude/**",
    ".codex/**",
    ".agents/**",
)

ENGINE_SURFACE_REGEXES: Final[tuple[re.Pattern[str], ...]] = tuple(
    _compile_case_insensitive(p) for p in ENGINE_SURFACE_PATTERNS
)


def is_engine_surface(dest_rel_canonical: str) -> bool:
    """Whether a canonical destination lives in an engine's prompt surface."""
    return path_matches_any(dest_rel_canonical, ENGINE_SURFACE_REGEXES)


def is_sensitive_write_dest(dest_rel_canonical: str) -> bool:
    """Whether writing this destination needs an `allow_sensitive_writes` grant."""
    return path_matches_any(
        dest_rel_canonical, SENSITIVE_WRITE_REGEXES
    ) and not is_engine_surface(dest_rel_canonical)


def is_sensitive_delete_dest(dest_rel_canonical: str) -> bool:
    """Whether deleting this destination is refused outright."""
    return path_matches_any(
        dest_rel_canonical, SENSITIVE_DELETE_REGEXES
    ) and not is_engine_surface(dest_rel_canonical)


def load_yaml(path: Path) -> object:
    """Parse a YAML file, or exit when it is missing.

    Returns `object`, not `dict[str, Any]`: `yaml.safe_load` parses a
    top-level list or bare scalar just as happily as a mapping, and typing
    the return as a dict would let that shape reach the first `.get()` as a
    crash instead of a config error. Callers narrow with `isinstance` and
    report the path in their own words.
    """
    if not path.is_file():
        sys.stderr.write(f"missing required file: {path}\n")
        sys.exit(2)
    with path.open() as fp:
        loaded: object = yaml.safe_load(fp)
    return {} if loaded is None else loaded


def read_utf8(path: Path) -> str:
    """Read UTF-8 text without universal-newline translation."""
    with path.open(encoding="utf-8", newline="") as file:
        return file.read()


def write_utf8(path: Path, content: str, mode: int | None = None) -> None:
    """Atomically write UTF-8 text without platform newline translation.

    Written to a sibling temp file and moved into place with `os.replace`,
    so a crash mid-write (job kill, full disk) leaves either the old
    destination or the new one — never a truncated half-file for the next
    step to commit. The temp file lives in `path.parent` because
    `os.replace` is only atomic within one filesystem.

    `mode` is applied to the temp file before the swap, so content and
    permission bits land in one atomic step. When omitted, the destination
    keeps the temp file's owner-only 0o600 — a caller syncing a readable
    destination must pass the bits it wants.
    """
    # Resolve leaf symlinks to update target content atomically in-place.
    destination_path = path.resolve() if path.is_symlink() else path
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=destination_path.parent,
            prefix=f".{destination_path.name}.",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(content)
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, destination_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def drop_empty_placeholder_lines(
    text: str, rendered_values: dict[str, str], collapse_keys: set[str], source: str
) -> str:
    """Delete opted-in placeholder-only lines whose values render empty.

    Collapsing is opt-in per key rather than inferred, because only the
    substitution step knows which blank lines it caused; see `docs/sync.md`
    ("Template half") for why the engine has no Markdown parser and what makes
    a key safe to opt in.

    The contract:

      - a line qualifies only if it contains at least one placeholder, every
        placeholder on it is listed in `collapse_keys`, every one of those
        values renders to the empty string, and nothing outside the
        placeholders remains but spaces, tabs, and carriage returns (see the
        CRLF note on the qualification test below);
      - a qualifying line is dropped, plus one adjacent blank line when keeping
        it would leave a blank-line run. Start and end of file count as blank
        for that test, so a placeholder at either edge does not leave a leading
        or trailing blank;
      - the following blank is preferred so the preceding section keeps its own
        spacing.

    An opted-in key that renders empty but whose line never qualifies is a
    misconfiguration that would otherwise leave exactly the blank-line churn the
    opt-in exists to prevent, so it warns rather than passing silently.

    Every other byte passes through untouched, and an empty `collapse_keys`
    (the default) is byte-identity.
    """
    if not collapse_keys:
        return text
    trailing_newline = text.endswith("\n")
    lines = (text[:-1] if trailing_newline else text).split("\n")
    keep = [True] * len(lines)

    def is_blank(index: int) -> bool:
        # Out of range behaves like a blank line boundary.
        if not 0 <= index < len(lines):
            return True
        return not lines[index].strip(" \t\r")

    def previous_kept(index: int) -> int:
        # Look past dropped lines to avoid extra separator consumption.
        probe = index - 1
        while probe >= 0 and not keep[probe]:
            probe -= 1
        return probe

    collapsed: set[str] = set()
    for i, line in enumerate(lines):
        matches = list(PLACEHOLDER_RE.finditer(line))
        if not matches or PLACEHOLDER_RE.sub("", line).strip(" \t\r"):
            continue
        keys = [match.group(1) for match in matches]
        if any(key not in collapse_keys or rendered_values.get(key) != "" for key in keys):
            continue
        collapsed.update(keys)
        keep[i] = False
        previous = previous_kept(i)
        if not (is_blank(previous) and is_blank(i + 1)):
            continue
        # Prefer dropping following blank line; fall back to preceding line at EOF.
        if i + 1 < len(lines):
            keep[i + 1] = False
        elif previous >= 0:
            keep[previous] = False

    present = set(PLACEHOLDER_RE.findall(text))
    unmatched = sorted(
        key
        for key in collapse_keys
        if key in present and rendered_values.get(key) == "" and key not in collapsed
    )
    if unmatched:
        sys.stderr.write(
            f"::warning file={source}::collapse_empty_substitutions keys rendered empty "
            f"in {source} but no line qualified (not a whole-line placeholder?): "
            f"{', '.join(unmatched)}\n"
        )

    if all(keep):
        # Preserve byte-identity when no lines were dropped.
        return text
    out = "\n".join(line for line, kept in zip(lines, keep) if kept)
    # Entirely emptied document remains empty without trailing newline.
    if trailing_newline and out:
        return out + "\n"
    if out.endswith("\r") and not keep[-1]:
        # Drop orphaned carriage return when last line of non-newline CRLF source was dropped.
        return out[:-1]
    return out


def substitute(
    text: str,
    values: dict[str, object],
    target_keys: list[str],
    source: str,
    collapse_empty_substitutions: Sequence[str] = (),
) -> str:
    """Replace `<<KEY>>` tokens in text with values from `values`.

    Only keys listed in `target_keys` are substituted — unknown placeholders
    in the source are left intact (and a warning is printed) so that a
    template change doesn't silently swallow content the consumer hadn't
    configured for yet.

    `collapse_empty_substitutions` must be a subset of `target_keys` (`main`
    exits 1 otherwise). Those keys additionally have their placeholder-only
    template line removed when the value renders empty — see
    `drop_empty_placeholder_lines` for the exact contract.
    """
    seen = set(PLACEHOLDER_RE.findall(text))
    declared = set(target_keys)

    missing_in_source = declared - seen
    if missing_in_source:
        sys.stderr.write(
            f"  ⚠️  declared substitutions not found in {source}: "
            f"{', '.join(sorted(missing_in_source))}\n"
        )

    undeclared_in_source = seen - declared
    if undeclared_in_source:
        sys.stderr.write(
            f"  ⚠️  placeholders in {source} not declared in sync-targets.yml: "
            f"{', '.join(sorted(undeclared_in_source))} (left intact)\n"
        )

    missing_in_config = declared - set(values.keys())
    if missing_in_config:
        sys.stderr.write(
            f"  ❌ {source} requires placeholders missing from the consumer config: "
            f"{', '.join(sorted(missing_in_config))}\n"
        )
        sys.exit(1)

    # None values render as empty string to support blank section values.
    rendered_values = {
        key: "" if values[key] is None else str(values[key]).rstrip("\r\n")
        for key in declared
    }
    collapse_keys = set(collapse_empty_substitutions)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in declared:
            return rendered_values[key]
        return match.group(0)

    prepared = drop_empty_placeholder_lines(text, rendered_values, collapse_keys, source)
    return PLACEHOLDER_RE.sub(replace, prepared)


def write_if_changed(path: Path, content: str, mode: int | None) -> bool:
    """Write content to path only if it differs. Return True if a write happened."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_utf8(path) if path.is_file() else None
    changed = existing != content
    if changed:
        if mode is not None:
            desired_mode = mode
        elif existing is not None:
            desired_mode = stat.S_IMODE(path.stat().st_mode)
        else:
            desired_mode = 0o644
        write_utf8(path, content, desired_mode)
    if mode is not None:
        # Compare full 12-bit permission set to avoid redundant chmod on every run.
        current = stat.S_IMODE(path.stat().st_mode)
        if current != mode:
            path.chmod(mode)
            changed = True
    return changed


def resolve_under(parent: Path, child_rel: str) -> Path | None:
    """Compute parent/child_rel and return it only if it lies under parent.

    Returns None if `child_rel` would escape `parent` via `..` segments or
    an absolute path. Uses lexical normalization (`os.path.normpath`) — not
    `Path.resolve()` — so that legitimate symlinks at the destination,
    including dangling ones, are not mis-flagged as escaping the parent.

    Limitation: lexical-only check does NOT prevent traversal via a
    symlink in an intermediate directory (e.g., a consumer-side symlink
    that points outside the consumer tree). The threat model assumes an
    upstream-controlled manifest and consumer trees free of malicious
    symlinks; defense against an attacker who can plant symlinks in the
    consumer working tree is out of scope.
    """
    candidate = Path(os.path.normpath(parent / child_rel))
    if candidate == parent:
        return None
    try:
        candidate.relative_to(parent)
    except ValueError:
        return None
    return candidate


def prune_empty_parents(file_path: Path, root: Path) -> None:
    """Walk up from file_path's parent toward root, removing empty dirs.

    Stops at root (does not remove root itself) and at the first non-empty
    directory. ENOTEMPTY and ENOENT (concurrent remove) are benign stop
    conditions handled silently; other OSErrors are logged to stderr and
    also stop the walk. Pruning is best-effort — failures are surfaced for
    visibility but do not propagate, since the file unlink has already
    succeeded by the time the parent walk runs.
    """
    parent = file_path.parent.resolve()
    root = root.resolve()
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError as e:
            if e.errno not in (errno.ENOTEMPTY, errno.ENOENT):
                sys.stderr.write(f"  ⚠️  could not prune {parent}: {e}\n")
            return
        parent = parent.parent


def parse_mode(value: object) -> int | None:
    """Coerce a `mode` field from sync-targets.yml into a permission int.

    Accepts both a quoted string (`"0755"` — interpreted as octal) and an
    unquoted YAML int (`0755` — already parsed octal in YAML 1.1, decimal in 1.2).
    Returning `None` means "leave the file's current mode alone."
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # `bool` is a subclass of `int`; reject explicitly so a stringly-
        # typed `true` / `false` doesn't become `mode: 1` / `mode: 0`.
        raise TypeError(f"mode must be int or str, got bool: {value!r}")
    if isinstance(value, int):
        mode_int = value
    elif isinstance(value, str):
        mode_int = int(value, 8)
    else:
        raise TypeError(f"mode must be int, str, or None; got {type(value).__name__}")
    if not 0 <= mode_int <= 0o7777:
        raise ValueError(f"mode out of range [0, 0o7777]: {value!r}")
    return mode_int


def parse_str_list(target: dict[str, Any], key: str) -> list[str] | None:
    """Read an optional list-of-strings field off one sync-targets.yml entry.

    Returns `[]` when the key is absent or null, the list itself when it is
    well-formed, and `None` after writing the error when it is not — the caller
    turns that into a non-zero exit.
    """
    raw = target.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        sys.stderr.write(f"  ❌ `{key}` must be a list of strings, got {raw!r}: {target!r}\n")
        return None
    return raw


def parse_sensitive_write_allowlist(
    config_doc: dict[str, Any], config_path: Path, consumer_dir: Path
) -> frozenset[str] | None:
    """Parse `allow_sensitive_writes` into a set of exact destination paths.

    Returns the (possibly empty) set of sensitive paths this consumer has
    opted into, or None if the key is malformed — the error is already on
    stderr, matching `parse_str_list`'s contract.

    Two semantics differ deliberately from `allowed_destinations`:

    * **Absent means deny.** There is no fail-open migration phase here.
      A consumer that never opts in gets a hard failure naming the exact
      line to add, not a warning buried in a green job. Failing closed is
      cheap for this gate specifically: nothing is written when it trips,
      so the blast radius is a red sync run against an untouched tree.
    * **Literal paths only.** `allowed_destinations` takes globs because
      it describes surfaces. This takes exact destinations because the
      whole point is per-file consent — `.github/workflows/**` here would
      rebuild the hole the gate exists to close.

    Entries that aren't sensitive paths are rejected rather than ignored.
    A typo (`.github/workflow/dco.yml`) would otherwise parse fine and
    leave the real destination unauthorized, so the consumer would see
    their config naming the path *and* a refusal to write it. The cost is
    that narrowing `SENSITIVE_WRITE_PATTERNS` later turns a stale entry
    into a config error; widening it, the direction this set actually
    moves, is safe.
    """
    if "allow_sensitive_writes" not in config_doc:
        return frozenset()

    raw = config_doc["allow_sensitive_writes"]
    if raw is None:
        sys.stderr.write(
            f"{config_path}: `allow_sensitive_writes:` is present but null. "
            f"Remove the key, or use `[]` to say explicitly that no sensitive "
            f"path may be written.\n"
        )
        return None
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        sys.stderr.write(
            f"{config_path}: `allow_sensitive_writes` must be a list of strings\n"
        )
        return None

    entries: set[str] = set()
    for entry in raw:
        if "*" in entry or "?" in entry:
            sys.stderr.write(
                f"{config_path}: `allow_sensitive_writes` entries must be literal "
                f"paths, not globs — a directory pattern here would re-open the "
                f"gap this gate closes. Name each file explicitly: {entry!r}\n"
            )
            return None
        resolved = resolve_under(consumer_dir, entry)
        if resolved is None or resolved.relative_to(consumer_dir).as_posix() != entry:
            sys.stderr.write(
                f"{config_path}: `allow_sensitive_writes` entries must be canonical "
                f"repo-relative posix paths (no `./`, `//`, `..`, or leading `/`): "
                f"{entry!r}\n"
            )
            return None
        if not path_matches_any(entry, SENSITIVE_WRITE_REGEXES):
            sys.stderr.write(
                f"{config_path}: `allow_sensitive_writes` lists {entry!r}, which is "
                f"not a sensitive path. Ordinary destinations are governed by "
                f"`allowed_destinations`; drop this entry so the list stays a "
                f"readable inventory of the exceptions this consumer really made.\n"
            )
            return None
        entries.add(entry)
    return frozenset(entries)


def sensitive_write_refusal(
    destinations: Sequence[str],
    config_name: str,
    allowlist: frozenset[str],
    harness: str | None = None,
    own_allowlist: frozenset[str] | None = None,
) -> str:
    """The refusal text for unconsented writes to sensitive destinations.

    Emits the complete `allow_sensitive_writes:` mapping the consumer should
    end up with — every denied destination *and* every grant they already
    hold — exactly once, as a replacement for the key rather than as
    something to append.

    Both halves close a different route to the same duplicate-key defect.
    One block per denied path would look helpful and paste badly: two blocks
    are two occurrences of the same YAML key and `yaml.safe_load` keeps only
    the last. Listing only the *newly* denied paths reaches that same end
    state one run later — as soon as a consumer holds a partial grant, an
    appended block is a second occurrence of a key their config already has,
    so the paste silently discards the grants they had and the next run
    refuses a path their config visibly names. Pasting again does not
    converge: each run refuses whichever half the previous paste dropped.
    Carrying the existing allowlist into the block makes the instruction
    safe from any starting state, and the `set` union also collapses two
    manifest targets that write one destination.

    Shared by the pre-pass and the in-loop gate so a consumer sees the same
    message and the same copy-pasteable fix whichever one refuses; the loop
    passes a single-element sequence.

    The grant block is emitted at column zero, set off by a blank line, while
    the rest of the message stays indented with the engine's other output.
    The inconsistency is deliberate: a consumer config keeps its
    top-level keys at column zero, so an indented mapping pasted into one
    does not survive. Depending on what precedes it that is either a
    `yaml.parser.ParserError` or — worse, because it is silent — a mapping
    nested under the preceding key, which parses cleanly and grants nothing.
    """
    plural = "s" if len(destinations) > 1 else ""
    listed = "".join(f"       - {dest}\n" for dest in destinations)
    # Scope remediation to harness when the consumer declared harness-level grants.
    if harness is not None and own_allowlist:
        scoped = own_allowlist
        granted = "".join(f"      - {dest}\n" for dest in sorted(set(destinations) | scoped))
        return (
            f"  \u274c refusing to write sensitive path{plural} without an explicit "
            f"opt-in (engine-level block, applies regardless of "
            f"allowed_destinations):\n"
            f"{listed}"
            f"     Content written here controls what runs in this repo, "
            f"who has to review it, or what the build installs. These targets "
            f"belong to the {harness} harness, so grant them under that "
            f"harness in {config_name} — replace its `allow_sensitive_writes` "
            f"block with the following, keeping the indentation:\n"
            f"\n"
            f"harnesses:\n"
            f"  {harness}:\n"
            f"    allow_sensitive_writes:\n"
            f"{granted}"
            f"\n"
            f"     Do not paste this list at the top level: a top-level grant "
            f"covers the shared target set and every other harness, which is "
            f"wider than the consent these targets need.\n"
        )
    granted = "".join(f"  - {dest}\n" for dest in sorted(set(destinations) | allowlist))
    instruction = (
        f"replace the existing `allow_sensitive_writes` block in "
        f"{config_name} with the following, which keeps the grants already "
        f"there"
        if allowlist
        else f"paste the following into {config_name} exactly as shown"
    )
    return (
        f"  ❌ refusing to write sensitive path{plural} without an explicit "
        f"opt-in (engine-level block, applies regardless of "
        f"allowed_destinations):\n"
        f"{listed}"
        f"     Content written here controls what runs in this repo, "
        f"who has to review it, or what the build installs. To allow "
        f"it, {instruction}:\n"
        f"\n"
        f"allow_sensitive_writes:\n"
        f"{granted}"
    )


def config_destination_refusal(destinations: Sequence[str], config_name: str) -> str:
    """The refusal text for a manifest target that writes the consumer's config."""
    plural = "s" if len(destinations) > 1 else ""
    listed = "".join(f"       - {dest}\n" for dest in destinations)
    return (
        f"  ❌ refusing to write the consumer's own sync config{plural} "
        f"(engine-level block, no opt-in exists):\n"
        f"{listed}"
        f"     {config_name} records `allow_sensitive_writes` and "
        f"`allowed_destinations`. A manifest that can rewrite it can grant "
        f"itself every permission this engine checks — including consent for "
        f"the sensitive paths it gates — so this destination is refused "
        f"unconditionally rather than being made opt-in.\n"
    )


def config_write_targets(
    targets: list[Any], consumer_dir: Path, config_paths: Collection[Path]
) -> list[str]:
    """Canonical destinations that would overwrite the consumer's own config.

    Admission control, run alongside `unconsented_sensitive_writes` and for
    the same reason: the refusal has to land before anything is written.

    The config file is the consent store, so it cannot be governed by the
    consent it stores. Adding it to `SENSITIVE_WRITE_PATTERNS` would be the
    wrong shape — that makes it grantable, and a manifest that can write the
    config can write its own grant, which rebuilds the same hole one level
    down. It is refused outright instead.

    Matched by resolved path rather than by filename, so an explicit
    `--config` elsewhere is covered while a config file vendored in the
    consumer tree as an example or a test fixture stays an ordinary
    destination. Takes every input and automatically selectable config path:
    even a currently absent file could grant permissions on the next run.

    Deletion is also refused: removing a config can select a different,
    more permissive surviving config on the next run.
    """
    offenders: list[str] = []
    config_path_set = set(config_paths)
    for target in targets:
        if not isinstance(target, dict):
            continue
        dest_rel = target.get("destination")
        if not isinstance(dest_rel, str) or not dest_rel:
            continue
        if not isinstance(target.get("delete"), (bool, type(None))):
            continue
        dest_path = resolve_under(consumer_dir, dest_rel)
        if dest_path is None or dest_path.resolve() not in config_path_set:
            continue
        offenders.append(dest_path.relative_to(consumer_dir).as_posix())
    return offenders


def announce_sensitive_write(dest_rel_canonical: str) -> None:
    """Surface a permitted sensitive write in the job log.

    Called at the write itself rather than at the consent check, so the line
    means what it says: a reviewer of the sync PR should be able to see that
    this run rewrote a workflow without reading the patch.
    """
    print(
        f"  ⚠️  sensitive destination {dest_rel_canonical} "
        f"(opted in via `allow_sensitive_writes`)"
    )


def unconsented_sensitive_writes(
    targets: list[Any],
    skip: set[str],
    consumer_dir: Path,
    allowlist: frozenset[str],
    allowed_patterns: list[re.Pattern[str]] | None,
) -> list[str]:
    """Canonical destinations that the sync would write without consent.

    Admission control, run before the mutating loop so a refusal really does
    leave the tree untouched. Without it the gate fires from inside the loop
    and every target ahead of the offending one has already been written —
    and the canonical manifest puts `.github/workflows/dco.yml` last, which
    is the worst possible ordering for that.

    Deliberately permissive about everything that is not its own question:
    escaping and non-canonical destinations, a non-mapping target, and a
    non-boolean `delete` are skipped here and left to the main loop, which
    reports them far better than a pre-pass could. This function only ever
    answers "would this target write a sensitive path the consumer has not
    named?", so a target it cannot classify is not its business.

    That permissiveness does not extend to every malformed shape, and the
    difference is visible to consumers. A bad `mode`, an unknown manifest
    field, or a non-boolean `create_if_missing` has no check here, so such a
    target reaches the consent test and this pass returns first — the
    consumer is told they are missing consent when their real problem is a
    malformed manifest entry. Reordering that would mean giving the pre-pass
    the loop's validation, which is the coupling the note below exists to
    avoid; the cheaper correction is to keep this list honest.

    The main loop keeps its own copy of this check. That duplication is the
    point: this pass exists for *ordering*, and a security gate should not
    depend on a pre-pass staying in perfect sync with the loop's
    reachability rules. If the two ever drift, the loop still refuses — it
    just refuses less atomically.
    """
    denied: list[str] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        dest_rel = target.get("destination")
        source_rel = target.get("source")
        if not isinstance(dest_rel, str) or not dest_rel:
            continue
        # Only writes are gated; skip malformed non-boolean delete flags.
        if not isinstance(target.get("delete"), (bool, type(None))):
            continue
        if bool(target.get("delete")):
            continue
        if (isinstance(source_rel, str) and source_rel in skip) or dest_rel in skip:
            continue
        dest_path = resolve_under(consumer_dir, dest_rel)
        if dest_path is None:
            continue
        dest_rel_canonical = dest_path.relative_to(consumer_dir).as_posix()
        if dest_rel_canonical != dest_rel:
            continue
        # Destinations outside allowed_destinations are handled by the allowlist check.
        if allowed_patterns is not None and not path_matches_any(
            dest_rel_canonical, allowed_patterns
        ):
            continue
        if not is_sensitive_write_dest(dest_rel_canonical):
            continue
        # Existing non-directory destinations for create_if_missing require no write.
        cim_raw = target.get("create_if_missing")
        if isinstance(cim_raw, bool) and cim_raw:
            is_real_dir = dest_path.is_dir() and not dest_path.is_symlink()
            if not is_real_dir and (dest_path.exists() or dest_path.is_symlink()):
                continue
        if dest_rel_canonical not in allowlist:
            denied.append(dest_rel_canonical)
    return denied


def unallowed_destinations(
    targets: Sequence[Any],
    skip: Collection[str],
    consumer_dir: Path,
    allowed_patterns: Sequence[Any] | None,
) -> list[str]:
    """Destinations this scope would touch that its `allowed_destinations` bars.

    The companion to `unconsented_sensitive_writes`, and it exists for the same
    reason: so the whole run is refused before anything is written. Without it
    the primary write-surface gate was enforced only inside the main loop, which
    returns on the first offending target — after every earlier target in plan
    order had already been written, deleted, and had its parents pruned. That
    left the invariant the admission block claims ("nothing is written when the
    gate trips") true of the sensitive-write gate but false of this one, and
    sync-v2 widened the gap: the plan is now multi-scope, so a too-narrow
    allowlist on the *second* harness aborts only after the first harness's
    entire tree has landed.

    Gates copy, delete, and `create_if_missing` alike, mirroring the loop: all
    three touch the destination, and unlike the sensitive-write pass there is no
    exemption for a `create_if_missing` whose destination already exists — the
    loop checks the allowlist before it decides to preserve.

    Reachability rules match the loop's order (skip, then resolve, then
    canonical form). Targets the loop rejects outright for other reasons —
    escaping the consumer root, or a non-canonical destination — are left to it,
    so the consumer gets that specific error rather than an allowlist complaint
    about a path that was malformed to begin with.
    """
    if allowed_patterns is None:
        return []
    denied: list[str] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        dest_rel = target.get("destination")
        source_rel = target.get("source")
        if not isinstance(dest_rel, str) or not dest_rel:
            continue
        if (isinstance(source_rel, str) and source_rel in skip) or dest_rel in skip:
            continue
        dest_path = resolve_under(consumer_dir, dest_rel)
        if dest_path is None:
            continue
        dest_rel_canonical = dest_path.relative_to(consumer_dir).as_posix()
        if dest_rel_canonical != dest_rel:
            continue
        if not path_matches_any(dest_rel_canonical, allowed_patterns):
            denied.append(dest_rel_canonical)
    return denied


def consumer_symlink_destinations(
    targets: Sequence[Any], skip: Collection[str], consumer_dir: Path
) -> list[str]:
    """Selected destinations containing a consumer-side symlink component.

    The regular workflow sync preserves the engine's documented trusted-tree
    behavior. The local onboarding CLI opts into this stricter preflight so a
    checkout-controlled link cannot turn a repository write into a host write.
    """
    denied: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            continue
        dest_rel = target.get("destination")
        source_rel = target.get("source")
        if not isinstance(dest_rel, str) or not dest_rel:
            continue
        if (isinstance(source_rel, str) and source_rel in skip) or dest_rel in skip:
            continue
        dest_path = resolve_under(consumer_dir, dest_rel)
        if dest_path is None:
            continue
        dest_rel_canonical = dest_path.relative_to(consumer_dir).as_posix()
        if dest_rel_canonical != dest_rel:
            continue
        probe = consumer_dir
        for part in dest_path.relative_to(consumer_dir).parts:
            probe /= part
            if probe.is_symlink():
                denied.add(dest_rel_canonical)
                break
    return sorted(denied)


def render_telemetry_env(raw: object, config_path: Path) -> str | None:
    """Render the `telemetry:` block as a one-line JSON object literal.

    The value is substituted into `.claude/settings.json`, whose `env` block
    is how a repository-level gate reaches the harness. Only gates the
    consumer actually declared are rendered: an env block that named every
    gate would have to pick a value for the ones the consumer left alone, and
    settings-declared environment beats the ambient shell — so a defaulted
    `off` would quietly override a developer who had exported `on`.

    Single-line on purpose. A multi-line substitution value renders LF into a
    CRLF template (#124), and this value is written into a consumer's
    repository rather than read by a person.
    """
    if raw is None:
        return "{}"
    if not isinstance(raw, dict):
        sys.stderr.write(f"{config_path}: `telemetry` must be a mapping\n")
        return None

    unknown = sorted(str(key) for key in raw if key not in TELEMETRY_GATES)
    if unknown:
        sys.stderr.write(
            f"{config_path}: unknown `telemetry` key(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(TELEMETRY_GATES))})\n"
        )
        return None

    rendered: list[str] = []
    # Keep rendering deterministic by following TELEMETRY_GATES order.
    for key, variable in TELEMETRY_GATES.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool):
            text = "on" if value else "off"
        elif isinstance(value, str) and value.strip().lower() in TELEMETRY_VALUES:
            text = value.strip().lower()
        else:
            sys.stderr.write(
                f"{config_path}: `telemetry.{key}` must be `on` or `off`, "
                f"got {value!r}\n"
            )
            return None
        rendered.append(f'"{variable}": "{text}"')

    if not rendered:
        return "{}"
    return "{ " + ", ".join(rendered) + " }"


def parse_manifest(
    targets_doc: dict[str, Any], targets_path: Path
) -> tuple[dict[str, dict[str, Any]], list[Any]] | None:
    """Split the manifest into ordered harness specs and the shared target set.

    Returns None after writing the error when the manifest is malformed.

    A pre-sync-v2 manifest — one flat top-level `targets:` list — is rejected
    by name rather than read as an empty harness set. The engine and the
    manifest ship from the same upstream checkout, so the only way to see one
    is to point `--upstream-repo` at a checkout of the frozen `sync-v1` tag,
    and a consumer that does that deserves the reason rather than a silent
    zero-target run reporting success.
    """
    if "targets" in targets_doc:
        sys.stderr.write(
            f"{targets_path}: found a top-level `targets:` list. That is the "
            f"pre-sync-v2 manifest format; this engine reads `harnesses:` and "
            f"`shared:`. Check out the upstream at the `sync-v2` tag or later.\n"
        )
        return None

    unknown = sorted(str(key) for key in targets_doc if key not in {"harnesses", "shared"})
    if unknown:
        sys.stderr.write(
            f"{targets_path}: unknown top-level key(s): {', '.join(unknown)} "
            f"(known: harnesses, shared)\n"
        )
        return None

    harnesses_raw = targets_doc.get("harnesses")
    if not isinstance(harnesses_raw, dict) or not harnesses_raw:
        sys.stderr.write(f"{targets_path}: `harnesses` must be a non-empty mapping\n")
        return None

    specs: dict[str, dict[str, Any]] = {}
    for name, spec in harnesses_raw.items():
        if not isinstance(name, str) or not name:
            sys.stderr.write(f"{targets_path}: harness names must be strings, got {name!r}\n")
            return None
        if not isinstance(spec, dict):
            sys.stderr.write(f"{targets_path}: harness `{name}` must be a mapping\n")
            return None
        root = spec.get("root")
        if not isinstance(root, str) or not root:
            sys.stderr.write(
                f"{targets_path}: harness `{name}` needs a non-empty string `root`\n"
            )
            return None
        if not isinstance(spec.get("targets"), list):
            sys.stderr.write(f"{targets_path}: harness `{name}` needs a `targets` list\n")
            return None
        specs[name] = spec

    shared_raw = targets_doc.get("shared") or {}
    if not isinstance(shared_raw, dict):
        sys.stderr.write(f"{targets_path}: `shared` must be a mapping\n")
        return None
    shared_targets = shared_raw.get("targets") or []
    if not isinstance(shared_targets, list):
        sys.stderr.write(f"{targets_path}: `shared.targets` must be a list\n")
        return None

    return specs, shared_targets


def parse_allowed_destinations(
    doc: dict[str, Any], config_path: Path, where: str
) -> tuple[bool, list[str]] | None:
    """Read one `allowed_destinations` list. Returns (declared, patterns)."""
    if "allowed_destinations" not in doc:
        return False, []
    raw = doc["allowed_destinations"]
    if raw is None:
        sys.stderr.write(
            f"{config_path}: `allowed_destinations:` is present but null "
            f"({where}). Use `[]` to deny everything, or remove the key to opt "
            f"into phase-1 fail-open behavior.\n"
        )
        return None
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        sys.stderr.write(
            f"{config_path}: `allowed_destinations` must be a list of strings ({where})\n"
        )
        return None
    return True, raw


def resolve_config(
    explicit: Path | None, consumer_dir: Path
) -> tuple[dict[str, Any], Path, list[Path]] | None:
    """Load the consumer config.

    Returns the config document, the path to name in errors, and every input
    config path, or None after writing the error. Exits 2 when the consumer
    carries no config this engine can read.
    """
    if explicit is not None:
        path = explicit.resolve()
        loaded = load_yaml(path)
        if not isinstance(loaded, dict):
            sys.stderr.write(f"{path}: top-level YAML document must be a mapping\n")
            return None
        return loaded, path, [path]

    canonical = (consumer_dir / CANONICAL_CONFIG_NAME).resolve()
    if canonical.is_file():
        loaded = load_yaml(canonical)
        if not isinstance(loaded, dict):
            sys.stderr.write(f"{canonical}: top-level YAML document must be a mapping\n")
            return None
        return loaded, canonical, [canonical]

    retired = [name for name in RETIRED_CONFIG_NAMES if (consumer_dir / name).is_file()]
    if retired:
        found = ", ".join(retired)
        sys.stderr.write(
            f"missing required file: {canonical} — this repository still "
            f"carries the pre-sync-v2 per-harness config {found}, which this "
            f"engine no longer reads. Write a single {CANONICAL_CONFIG_NAME} "
            f"declaring which harnesses this repository runs, then delete "
            f"{found}.\n"
        )
    else:
        sys.stderr.write(
            f"missing required file: {canonical} — a consumer needs one config "
            f"declaring which harnesses it runs.\n"
        )
    sys.exit(2)


def _harness_blocks(
    config_doc: dict[str, Any], config_path: Path, specs: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]] | None:
    """Normalize `harnesses:` (list or mapping form) into per-harness blocks.

    Order follows the manifest, not the consumer's file: the manifest is what
    documents that the first declared harness owns a shared
    `create_if_missing` destination, so consumers must not be able to reorder
    that by editing their own config.
    """
    raw = config_doc.get("harnesses")
    if raw is None:
        sys.stderr.write(
            f"{config_path}: `harnesses` is required — list the harnesses this "
            f"repository runs, e.g. `harnesses: [{', '.join(list(specs)[:2])}]`\n"
        )
        return None

    if isinstance(raw, list):
        if not all(isinstance(name, str) for name in raw):
            sys.stderr.write(f"{config_path}: `harnesses` list entries must be strings\n")
            return None
        declared: dict[str, dict[str, Any]] = {name: {} for name in raw}
    elif isinstance(raw, dict):
        declared = {}
        for name, block in raw.items():
            if not isinstance(name, str):
                sys.stderr.write(f"{config_path}: harness names must be strings, got {name!r}\n")
                return None
            if block is None:
                block = {}
            if not isinstance(block, dict):
                sys.stderr.write(
                    f"{config_path}: `harnesses.{name}` must be a mapping or empty\n"
                )
                return None
            unknown = sorted(str(key) for key in block if key not in KNOWN_HARNESS_CONFIG_FIELDS)
            if unknown:
                sys.stderr.write(
                    f"{config_path}: unknown key(s) under `harnesses.{name}`: "
                    f"{', '.join(unknown)} "
                    f"(known: {', '.join(sorted(KNOWN_HARNESS_CONFIG_FIELDS))})\n"
                )
                return None
            declared[name] = block
    else:
        sys.stderr.write(
            f"{config_path}: `harnesses` must be a list of names or a mapping of them\n"
        )
        return None

    if not declared:
        sys.stderr.write(
            f"{config_path}: `harnesses` is empty. A consumer that wants no "
            f"harness surface should remove its sync workflow rather than run "
            f"the engine with nothing to deliver.\n"
        )
        return None

    unknown_harnesses = sorted(set(declared) - set(specs))
    if unknown_harnesses:
        sys.stderr.write(
            f"{config_path}: `harnesses` names {', '.join(unknown_harnesses)}, "
            f"which the upstream manifest does not define "
            f"(known: {', '.join(sorted(specs))})\n"
        )
        return None

    return {name: declared[name] for name in specs if name in declared}


def resolve_scopes(
    config_doc: dict[str, Any],
    config_path: Path,
    consumer_dir: Path,
    specs: dict[str, dict[str, Any]],
) -> tuple[Scope, dict[str, Scope]] | None:
    """Build the shared scope and one scope per declared harness.

    Returns `(shared_scope, {harness: scope})`, or None after writing the
    error.

    Three composition rules, and they are not the same rule:

    * `substitutions` — top-level values, then the harness's own on top. A
      harness override is the narrower statement, so it wins.
    * `skip_targets` and `allow_sensitive_writes` — union. Both are opt-outs
      and opt-ins the consumer wrote down; taking both is the conservative
      reading of "I meant this".
    * `allowed_destinations` — the harness's list *replaces* the top-level
      one when it declares one. This is the gate that bounds the write
      surface, and unioning it would hand every harness every other
      harness's surface, which is precisely the separation the per-harness
      config files provided before sync-v2.
    """
    unknown = sorted(str(key) for key in config_doc if key not in KNOWN_CONFIG_FIELDS)
    if unknown:
        sys.stderr.write(
            f"{config_path}: unknown key(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(KNOWN_CONFIG_FIELDS))})\n"
        )
        return None

    blocks = _harness_blocks(config_doc, config_path, specs)
    if blocks is None:
        return None

    telemetry_env = render_telemetry_env(config_doc.get("telemetry"), config_path)
    if telemetry_env is None:
        return None

    base_values = config_doc.get("substitutions") or {}
    if not isinstance(base_values, dict):
        sys.stderr.write(f"{config_path}: `substitutions` must be a mapping\n")
        return None

    base_skip_raw = config_doc.get("skip_targets") or []
    if not isinstance(base_skip_raw, list) or not all(
        isinstance(p, str) for p in base_skip_raw
    ):
        sys.stderr.write(f"{config_path}: `skip_targets` must be a list of strings\n")
        return None

    base_allowed = parse_allowed_destinations(config_doc, config_path, "the top level")
    if base_allowed is None:
        return None
    base_allowed_declared, base_allowed_globs = base_allowed
    # Precompile base allowed patterns once for reuse across scopes.
    base_allowed_patterns = [glob_to_regex(p) for p in base_allowed_globs]

    base_sensitive = parse_sensitive_write_allowlist(config_doc, config_path, consumer_dir)
    if base_sensitive is None:
        return None

    def build(
        label: str, block: dict[str, Any], where: str, *,
        harness_name: str | None = None,
    ) -> Scope | None:
        values = dict(base_values)
        own_values = block.get("substitutions") or {}
        if not isinstance(own_values, dict):
            sys.stderr.write(f"{config_path}: `substitutions` must be a mapping ({where})\n")
            return None
        values.update(own_values)

        reserved = sorted(RESERVED_SUBSTITUTION_KEYS & set(values))
        if reserved:
            sys.stderr.write(
                f"{config_path}: `substitutions` may not declare "
                f"{', '.join(reserved)} — the engine computes "
                f"{'that key' if len(reserved) == 1 else 'those keys'} from "
                f"elsewhere in this file.\n"
            )
            return None
        values["REVIEW_TELEMETRY_ENV"] = telemetry_env

        own_skip = block.get("skip_targets") or []
        if not isinstance(own_skip, list) or not all(isinstance(p, str) for p in own_skip):
            sys.stderr.write(
                f"{config_path}: `skip_targets` must be a list of strings ({where})\n"
            )
            return None

        own_allowed = parse_allowed_destinations(block, config_path, where)
        if own_allowed is None:
            return None
        own_allowed_declared, own_allowed_globs = own_allowed

        if own_allowed_declared:
            patterns: list[re.Pattern[str]] | None = [
                glob_to_regex(p) for p in own_allowed_globs
            ]
        elif base_allowed_declared:
            patterns = base_allowed_patterns
        else:
            sys.stdout.write(
                f"::warning file={config_path}::`allowed_destinations` not set "
                f"for {where}. Upstream sync-targets are currently trusted to "
                f"write anywhere in the consumer tree. Add an "
                f"`allowed_destinations:` list to enforce the gate before the "
                f"engine flips fail-closed.\n"
            )
            patterns = None

        own_sensitive = parse_sensitive_write_allowlist(block, config_path, consumer_dir)
        if own_sensitive is None:
            return None

        return Scope(
            label=label,
            values=values,
            skip=set(base_skip_raw) | set(own_skip),
            allowed_patterns=patterns,
            sensitive_write_allowlist=base_sensitive | own_sensitive,
            harness_name=harness_name,
            own_sensitive_write_allowlist=own_sensitive,
        )

    shared_scope = build("shared", {}, "the shared target set")
    if shared_scope is None:
        return None

    harness_scopes: dict[str, Scope] = {}
    for name, block in blocks.items():
        scope = build(
            f"harness {name}", block, f"`harnesses.{name}`", harness_name=name
        )
        if scope is None:
            return None
        harness_scopes[name] = scope

    return shared_scope, harness_scopes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upstream-repo", required=True, type=Path, help="path to a checkout of the upstream repo")
    parser.add_argument("--consumer-dir", required=True, type=Path, help="path to the consumer repo (dest)")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            f"path to the consumer config (default: <consumer-dir>/"
            f"{CANONICAL_CONFIG_NAME})"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="don't write files; report what would change")
    parser.add_argument(
        "--reject-consumer-symlinks",
        action="store_true",
        help="refuse selected destinations containing consumer-side symlinks",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    upstream_repo = args.upstream_repo.resolve()
    consumer_dir = args.consumer_dir.resolve()

    targets_path = upstream_repo / "scripts" / "sync-targets.yml"
    targets_doc = load_yaml(targets_path)
    if not isinstance(targets_doc, dict):
        sys.stderr.write(f"{targets_path}: top-level YAML document must be a mapping\n")
        return 1

    manifest = parse_manifest(targets_doc, targets_path)
    if manifest is None:
        return 1
    specs, shared_targets = manifest

    resolved = resolve_config(args.config, consumer_dir)
    if resolved is None:
        return 1
    config_doc, config_path, config_sources = resolved
    # Protect selectable config paths to prevent upstream manifests from overriding config.
    selectable_config_paths = frozenset({
        *config_sources,
        (consumer_dir / CANONICAL_CONFIG_NAME).resolve(),
        *((consumer_dir / name).resolve() for name in RETIRED_CONFIG_NAMES),
    })

    resolved_scopes = resolve_scopes(config_doc, config_path, consumer_dir, specs)
    if resolved_scopes is None:
        return 1
    shared_scope, harness_scopes = resolved_scopes

    # Group targets by scope for admission control and execution ordering.
    plan: list[tuple[Scope, list[Any]]] = [
        (scope, list(specs[name]["targets"])) for name, scope in harness_scopes.items()
    ]
    plan.append((shared_scope, list(shared_targets)))

    print(
        "Harnesses: "
        + ", ".join(f"{name} ({specs[name]['root']})" for name in harness_scopes)
    )

    # Check config writes before consent gates so an upstream manifest cannot
    # grant itself permissions by overwriting consumer configuration.
    config_writes = config_write_targets(
        [target for _, targets in plan for target in targets],
        consumer_dir,
        selectable_config_paths,
    )
    if config_writes:
        sys.stderr.write(config_destination_refusal(config_writes, config_path.name))
        return 1

    # Admission control: validate destinations across scopes before modifying files.
    for scope, scope_targets in plan:
        denied_allowed = unallowed_destinations(
            scope_targets, scope.skip, consumer_dir, scope.allowed_patterns
        )
        if denied_allowed:
            listed = "".join(f"       - {dest}\n" for dest in denied_allowed)
            sys.stderr.write(
                f"  \u274c destination not in consumer's `allowed_destinations` "
                f"({scope.label}):\n{listed}"
                f"     Add them to `allowed_destinations` in {config_path.name}, "
                f"or opt out of those targets with `skip_targets`.\n"
            )
            return 1
        denied_sensitive = unconsented_sensitive_writes(
            scope_targets,
            scope.skip,
            consumer_dir,
            scope.sensitive_write_allowlist,
            scope.allowed_patterns,
        )
        if denied_sensitive:
            sys.stderr.write(
                sensitive_write_refusal(
                    denied_sensitive,
                    config_path.name,
                    scope.sensitive_write_allowlist,
                    scope.harness_name,
                    scope.own_sensitive_write_allowlist,
                )
            )
            return 1
        if args.reject_consumer_symlinks:
            denied_symlinks = consumer_symlink_destinations(
                scope_targets, scope.skip, consumer_dir
            )
            if denied_symlinks:
                listed = "".join(f"       - {dest}\n" for dest in denied_symlinks)
                sys.stderr.write(
                    "  ❌ refusing selected destination(s) containing a "
                    f"consumer-side symlink:\n{listed}"
                )
                return 1

    print(f"Syncing from {upstream_repo} → {consumer_dir}")
    if args.dry_run:
        print("(dry run — no files will be written)")

    written = 0
    removed = 0
    skipped = 0
    unchanged = 0
    sensitive = 0

    for scope, targets in plan:
        for target in targets:
            if not isinstance(target, dict):
                sys.stderr.write(f"  ❌ malformed target entry: expected a mapping, got {target!r}\n")
                return 1
            unknown_fields = sorted(str(key) for key in target if key not in KNOWN_TARGET_FIELDS)
            if unknown_fields:
                sys.stderr.write(
                    f"  ❌ unknown field(s) in target entry: {', '.join(unknown_fields)} "
                    f"(known: {', '.join(sorted(KNOWN_TARGET_FIELDS))}): {target!r}\n"
                )
                return 1
            source_rel = target.get("source")
            dest_rel = target.get("destination")
            subs = parse_str_list(target, "substitutions")
            if subs is None:
                return 1
            collapse_empty_substitutions = parse_str_list(target, "collapse_empty_substitutions")
            if collapse_empty_substitutions is None:
                return 1
            for field, keys in (
                ("substitutions", subs),
                ("collapse_empty_substitutions", collapse_empty_substitutions),
            ):
                invalid_keys = sorted(key for key in set(keys) if PLACEHOLDER_NAME_RE.fullmatch(key) is None)
                if invalid_keys:
                    sys.stderr.write(
                        f"  ❌ `{field}` contains invalid placeholder keys: "
                        f"{', '.join(invalid_keys)}\n"
                        )
                    return 1
            undeclared_collapse_keys = set(collapse_empty_substitutions) - set(subs)
            if undeclared_collapse_keys:
                sys.stderr.write(
                    f"  ❌ `collapse_empty_substitutions` keys must also appear in "
                    f"`substitutions`: {', '.join(sorted(undeclared_collapse_keys))}\n"
                )
                return 1

            delete_raw = target.get("delete")
            if delete_raw is not None and not isinstance(delete_raw, bool):
                sys.stderr.write(
                    f"  ❌ `delete` must be a boolean (true/false), got {delete_raw!r}: {target!r}\n"
                )
                return 1
            delete_flag = bool(delete_raw)

            cim_raw = target.get("create_if_missing")
            if cim_raw is not None and not isinstance(cim_raw, bool):
                sys.stderr.write(
                    f"  ❌ `create_if_missing` must be a boolean (true/false), got {cim_raw!r}: {target!r}\n"
                )
                return 1
            create_if_missing_flag = bool(cim_raw)

            if delete_flag and create_if_missing_flag:
                sys.stderr.write(
                    f"  ❌ `delete` and `create_if_missing` are mutually exclusive: {target!r}\n"
                )
                return 1

            if dest_rel is not None and (
                not isinstance(dest_rel, str)
                or not dest_rel
                or dest_rel in (".", "..")
                or not dest_rel.isprintable()
            ):
                sys.stderr.write(
                    f"  ❌ `destination` must be a non-empty printable path string, "
                    f"got {dest_rel!r}: {target!r}\n"
                )
                return 1
            if source_rel is not None and (
                not isinstance(source_rel, str)
                or not source_rel
                or source_rel in (".", "..")
                or not source_rel.isprintable()
            ):
                sys.stderr.write(
                    f"  ❌ `source` must be a non-empty printable path string, "
                    f"got {source_rel!r}: {target!r}\n"
                )
                return 1

            if not dest_rel or (not delete_flag and not source_rel):
                sys.stderr.write(f"  ❌ malformed entry: {target!r}\n")
                return 1

            if delete_flag:
                if target.get("mode") is not None:
                    sys.stderr.write(f"  ❌ `mode` is not valid on a delete target: {target!r}\n")
                    return 1
                mode = None
            else:
                try:
                    mode = parse_mode(target.get("mode"))
                except (ValueError, TypeError) as e:
                    sys.stderr.write(f"  ❌ invalid `mode` ({e}): {target!r}\n")
                    return 1

            if (source_rel and source_rel in scope.skip) or dest_rel in scope.skip:
                label = source_rel or dest_rel
                print(f"  ⏭️  skip {label} (opted out via {config_path.name})")
                skipped += 1
                continue

            dest_path = resolve_under(consumer_dir, dest_rel)
            if dest_path is None:
                sys.stderr.write(f"  ❌ destination escapes consumer root: {dest_rel}\n")
                return 1

            dest_rel_canonical = dest_path.relative_to(consumer_dir).as_posix()
            if dest_rel_canonical != dest_rel:
                sys.stderr.write(
                    f"  ❌ destination must be in canonical posix form (no `./`, "
                    f"`//`, or `..` segments): got {dest_rel!r}, normalized form "
                    f"is {dest_rel_canonical!r}\n"
                )
                return 1

            if scope.allowed_patterns is not None and not path_matches_any(
                dest_rel_canonical, scope.allowed_patterns
            ):
                sys.stderr.write(
                    f"  ❌ destination not in consumer's `allowed_destinations` "
                    f"({scope.label}): {dest_rel_canonical}\n"
                )
                return 1

            if dest_path.resolve() in selectable_config_paths:
                sys.stderr.write(config_destination_refusal([dest_rel_canonical], config_path.name))
                return 1

            if delete_flag:
                if is_sensitive_delete_dest(dest_rel_canonical):
                    sys.stderr.write(
                        f"  ❌ refusing to delete sensitive path (engine-level "
                        f"block, applies regardless of allowed_destinations): "
                        f"{dest_rel_canonical}\n"
                    )
                    return 1
                if dest_path.is_dir() and not dest_path.is_symlink():
                    sys.stderr.write(
                        f"  ❌ destination is a directory, refusing to unlink: {dest_rel}\n"
                    )
                    return 1
                # Check existence including broken symlinks.
                existed = dest_path.exists() or dest_path.is_symlink()
                if args.dry_run:
                    if existed:
                        print(f"  🗑️  would remove {dest_rel}")
                        removed += 1
                    else:
                        print(f"  ✓  already absent {dest_rel}")
                        unchanged += 1
                    continue
                if not existed:
                    print(f"  ✓  already absent {dest_rel}")
                    unchanged += 1
                    continue
                dest_path.unlink(missing_ok=True)
                prune_empty_parents(dest_path, consumer_dir)
                print(f"  🗑️  removed {dest_rel}")
                removed += 1
                continue

            # Bootstrap create_if_missing destinations on first sync and preserve thereafter.
            if create_if_missing_flag:
                if dest_path.is_dir() and not dest_path.is_symlink():
                    sys.stderr.write(
                        f"  ❌ destination is a directory, refusing to bootstrap a file there: {dest_rel}\n"
                    )
                    return 1
                if dest_path.exists() or dest_path.is_symlink():
                    print(f"  ✓  preserved {dest_rel} (create_if_missing)")
                    unchanged += 1
                    continue

            # Authoritative check for unconsented sensitive writes.
            is_sensitive_write = is_sensitive_write_dest(dest_rel_canonical)
            if is_sensitive_write and dest_rel_canonical not in scope.sensitive_write_allowlist:
                sys.stderr.write(
                    sensitive_write_refusal(
                        [dest_rel_canonical],
                        config_path.name,
                        scope.sensitive_write_allowlist,
                        scope.harness_name,
                        scope.own_sensitive_write_allowlist,
                    )
                )
                return 1

            if source_rel is None:
                sys.stderr.write(
                    f"  ❌ internal invariant violated: copy target reached "
                    f"source-resolve with `source` unset: {target!r}\n"
                )
                return 1
            source_path = resolve_under(upstream_repo, source_rel)
            if source_path is None:
                sys.stderr.write(f"  ❌ source escapes upstream repo: {source_rel}\n")
                return 1

            if not source_path.is_file():
                sys.stderr.write(f"  ❌ source missing in upstream: {source_rel}\n")
                return 1

            text = read_utf8(source_path)
            substituted = substitute(
                text, scope.values, subs, source_rel, collapse_empty_substitutions
            )

            if args.dry_run:
                existing = read_utf8(dest_path) if dest_path.is_file() else None
                current_mode = stat.S_IMODE(dest_path.stat().st_mode) if dest_path.is_file() else None
                content_diverged = existing != substituted
                mode_diverged = mode is not None and current_mode is not None and current_mode != mode
                if content_diverged or mode_diverged:
                    reason = "content" if content_diverged else "mode"
                    if is_sensitive_write:
                        sensitive += 1
                        announce_sensitive_write(dest_rel_canonical)
                    print(f"  📝 would write {dest_rel} ({reason})")
                    written += 1
                else:
                    unchanged += 1
                continue

            if write_if_changed(dest_path, substituted, mode):
                if is_sensitive_write:
                    sensitive += 1
                    announce_sensitive_write(dest_rel_canonical)
                print(f"  ✅ wrote {dest_rel}")
                written += 1
            else:
                unchanged += 1

    summary = (
        f"\nDone: {written} written, {removed} removed, "
        f"{unchanged} unchanged, {skipped} skipped."
    )
    if sensitive:
        summary += f" {sensitive} sensitive destination(s) permitted by `allow_sensitive_writes`."
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
