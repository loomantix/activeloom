#!/usr/bin/env python3
"""Hold the *unrendered* shared skills to account for their divergence.

`render-prompts.py` single-sources the skills that differ only in harness
vocabulary. Everything else is a hand-maintained copy per prompt root, and a
copy is only honest while somebody can say *why* it differs. This lint reads
every skill that exists in more than one prompt root and is not rendered,
normalizes away the harness vocabulary, diffs what is left, and requires a
disposition for every skill with anything left over:

  - **recorded** — deliberate per-harness divergence, citing a record in
    `docs/decisions/`. Permanent; the residual may move freely.
  - **held** — accidental drift that has not been reconciled yet, citing a
    tracking issue and a residual `ceiling`. The ceiling is a ratchet: drift
    may shrink, never grow, and the recorded number must stay true.

A skill with residuals and no entry fails. That is the whole enforcement
mechanism, and it is why the allowlist must not become a dumping ground: an
entry is a claim someone made in writing, backed by a document that outlives
the session that wrote it.

A skill at **zero** residuals is single-sourceable today, so a live allowlist
entry naming it is stale and fails — either the skill is promoted into the
rendered roster or the entry is wrong. An unlisted skill at zero residuals is
not a failure; it is reported as a promotion candidate (and written to
`$GITHUB_STEP_SUMMARY` when CI sets one), because rendering a skill is a change
to `prompts/` and belongs in its own review, not bolted onto whichever PR
happened to finish the reconciliation.

## Normalizing the vocabulary

The vocabulary is not defined here. It is read from `prompts/profiles/*.yml` —
the same profiles the renderer substitutes — so the lint cannot disagree with
the renderer about what counts as "the same word in two dialects". Each root's
own values are replaced with a neutral token before the diff, plus one
synthetic `ROOT` key for the prompt root itself (`.claude` / `.codex` /
`.agents`), which no profile declares because the renderer gets it from
`root:`.

Values are matched with `\\s+` *between* words, so a value Prettier re-wrapped
across a line break still normalizes; a leading or trailing run is matched
horizontally instead, or a value ending in a space would eat the line break
behind it and hide whatever the diff would have found there. Single-token
values match on a word boundary, so `claude` inside `.claude/skills` is not
rewritten a second time.

A root's rules are built from its own values plus any *nested* dialect — where
one harness's value for a key contains another's, the shorter form is
recognized on both. `AGENT_DOC` is `AGENTS.md or CLAUDE.md` on Claude and
`AGENTS.md` elsewhere, and without this the identical sentence normalizes on
two roots and not the third, which reports a residual for text that does not
differ and puts zero permanently out of reach. Only a substring is borrowed,
so no engine name is ever rewritten to another engine's token.

Two deliberate distortions, both of which make the diff *more* honest:

  - `ENGINE_ID` and `ENGINE_CLI` normalize to one `<<ENGINE>>` token. They
    collide on the Claude profile (both are `claude`) and would otherwise
    label the same word differently per root, manufacturing a residual out of
    an exact match.
  - A key that is empty on *any* profile is deleted rather than tagged. A
    value present on one harness and empty on another is the same slot with
    nothing in it; tagging one side and not the other would report the
    presence of the slot as a difference.

`INVOKE` is neither: it is `/` on Claude and empty elsewhere, and deleting
every `/` would be absurd. It is normalized structurally instead — a leading
slash is stripped from a known skill name, so `/critique` and `critique` read
alike while `.claude/skills/critique` (already `<<SKILLS_ROOT>>/critique` by
then) is left alone.

Usage:
    python3 scripts/lint-prompt-parity.py            # what CI runs
    python3 scripts/lint-prompt-parity.py --report   # residual table, never fails
    python3 scripts/lint-prompt-parity.py --diff <skill>   # the normalized diff

Exit codes: 0 clean, 1 violations, 2 usage/config error.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Protocol

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
RENDERED_SRC = PROMPTS_DIR / "skills"
DECISIONS_DIR = REPO_ROOT / "docs" / "decisions"
ALLOWLIST_PATH = DECISIONS_DIR / "parity-allowlist.yml"

# Prompt roots in comparison order. Adjacent pairs are what get diffed
# (`.claude`↔`.codex`, `.codex`↔`.agents`), which is enough to prove a
# three-way match: agreement is transitive, so two pairs settle three roots.
#
# The residual *count* is a pair-sum and is not deduplicated. A divergence in
# the middle root is unmatched in both pairs and is charged to both, so the
# magnitude depends on where in this tuple a root sits — reordering the tuple
# re-baselines every ceiling in the allowlist. That is over-counting, never
# under-counting, so no divergence escapes on it; it is a reason to treat a
# ceiling as a ratchet against itself rather than as a portable measurement.
#
# Pinned here rather than derived from the profiles, and the distinction
# matters: a comparator that takes its subject list from the same declaration
# it is checking cannot detect an error *in that declaration*. This is the
# authority argument `render-prompts.py` makes for `SUPPORTED_PROFILE_ROOTS`,
# and a scope-derivation module is the wrong shape to borrow here — its job is
# that scope can never silently shrink, this one's job is that a difference can
# never silently go unexamined.
#
# `check_root_coverage` is the other half of that trade: pinned as the
# authority, then asserted against what the profiles actually declare, so a
# root added to the profiles and not to this tuple fails loudly instead of
# quietly going uncompared.
ROOT_ORDER = (".claude", ".codex", ".agents")

# Build artifacts that live inside a skill directory and are not part of it.
# Same exclusions the renderer applies to its sources, for the same reason:
# they are gitignored, so a reviewer never sees them, so the tool has to.
IGNORED_DIR_NAMES = frozenset({"__pycache__"})
IGNORED_SUFFIXES = (".pyc", ".pyo")

# Vocabulary keys that name the engine from different angles and must collapse
# to one token. See the module docstring.
ALIASES = {"ENGINE_ID": "ENGINE", "ENGINE_CLI": "ENGINE"}

# Matched without regard to case: these are proper nouns that appear as
# `claude`, `Claude`, and `CLAUDE` in the same paragraph.
CASE_INSENSITIVE_KEYS = frozenset({"ENGINE_ID", "ENGINE_CLI"})

# Normalized structurally rather than by value — see the module docstring.
STRUCTURAL_KEYS = frozenset({"INVOKE"})

# A value made only of these characters is a single token and gets word
# boundaries; anything else (a path, a phrase) is matched as written.
TOKEN_RE = re.compile(r"[\w.-]+\Z")

# Zero residuals says the copies agree. It does not say the copies are the only
# thing that has to move. A per-line lint suppression keyed to a hand-maintained
# path stops matching the moment that path becomes generated output, and the
# failure lands on a file nobody edited.
PROMOTION_CAVEAT = (
    "Zero residuals means single-sourceable, not automatically free to promote: "
    "promotion turns one hand-maintained path per root into one source plus three "
    "generated outputs, so check anything else keyed to those paths resolves to "
    "the source before moving the skill."
)


class ParityError(Exception):
    """A malformed input: a bad allowlist, an unreadable profile, a bad flag."""


class ProfileLike(Protocol):
    """The slice of `render-prompts.py`'s `Profile` this lint reads.

    Declared structurally so the tests can hand `build_rules` a two-field stub
    instead of a YAML file, and so the dependency on the renderer stays visibly
    narrow: a root, and the values it substitutes.
    """

    root: str
    values: dict[str, object]


# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------


def _load_render_prompts() -> ModuleType:
    """Import `scripts/render-prompts.py` for its profile loader.

    The lint does not parse the profiles itself. Two readers of one config
    drift, and the renderer's loader is the one that already rejects a
    malformed profile, an unsupported root, and a non-string value — so a
    profile this lint accepts is exactly a profile the renderer accepts.
    """
    path = SCRIPT_DIR / "render-prompts.py"
    spec = importlib.util.spec_from_file_location("render_prompts", path)
    if spec is None or spec.loader is None:
        raise ParityError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_prompts"] = module
    spec.loader.exec_module(module)
    return module


def check_root_coverage(profiles: Iterable[ProfileLike]) -> None:
    """Require the pinned comparison order and the declared roots to agree.

    Both directions are errors, for different reasons. A declared root missing
    from `ROOT_ORDER` is a whole harness nobody is comparing — the renderer
    would happily write a skill roster into it while this lint reported that
    every copy agreed. A pinned root that no profile declares has no vocabulary
    to normalize with, so the comparison would either crash or, worse, diff two
    roots in different dialects and call the difference divergence.
    """
    declared = {profile.root for profile in profiles}
    pinned = set(ROOT_ORDER)
    if undeclared := sorted(declared - pinned):
        raise ParityError(
            f"prompt root(s) declared by a profile but absent from ROOT_ORDER: "
            f"{undeclared}. Nothing is comparing them; add them to "
            f"scripts/lint-prompt-parity.py in the order they should be diffed."
        )
    if unprofiled := sorted(pinned - declared):
        raise ParityError(
            f"prompt root(s) in ROOT_ORDER that no profile declares: "
            f"{unprofiled}. There is no vocabulary to normalize them with, so "
            f"they cannot be compared."
        )


def check_root_trees(repo_root: Path = REPO_ROOT) -> None:
    """Require every pinned root to actually have a skills tree on disk.

    `check_root_coverage` checks the *declarations* agree. This checks the
    declaration is true, and the two failures are not the same: `_skills_in`
    returns an empty set for a directory that is not there, so a root whose
    `skills/` tree is renamed or relocated contributes no skills, is never
    missing from any comparison, and drops out of scope in silence. Every copy
    that harness holds would then go unexamined while the gate reported that
    every divergence was accounted for.

    A root with a vocabulary but no skills is also just not a state this
    repository has. Failing on it costs a real harness nothing and turns the
    quietest way to disable the gate into a startup error.
    """
    missing = sorted(
        root for root in ROOT_ORDER if not (repo_root / root / "skills").is_dir()
    )
    if missing:
        raise ParityError(
            f"prompt root(s) with no skills directory: {missing}. Every copy in "
            f"them would go uncompared with nothing reported; restore the tree, "
            f"or remove the root from ROOT_ORDER and its profile."
        )


def _value_pattern(value: str) -> re.Pattern[str]:
    """Compile a value into a whitespace-tolerant, boundary-aware pattern.

    Whitespace *interior* to a value becomes `\\s+`: the Markdown these values
    were rendered into is re-wrapped by Prettier, so a two-word value may
    straddle a line break in one root and not in another.

    A leading or trailing run is matched horizontally instead. It still has to
    be consumed — a decoration value like `'❓ '` carries a deliberate trailing
    space, or the harness that has no decoration is left one space short of
    matching — but `\\s+` at the edge is greedy across line breaks, so `'❓ '`
    against `"❓ \\n\\n  nested"` swallowed the marker, the blank line and the
    indent and merged three lines into one. Anything the diff would have seen
    in that whitespace disappeared with it, which is the fail-open direction.
    """
    # `re.split` brackets the value with empty strings when it starts or ends
    # on whitespace, so the edges have to be found after those are dropped.
    splits = [part for part in re.split(r"(\s+)", value) if part]
    edges = {0, len(splits) - 1}
    parts = [
        (r"[^\S\n]+" if index in edges else r"\s+") if part.isspace() else re.escape(part)
        for index, part in enumerate(splits)
    ]
    if not parts:
        raise ParityError("cannot build a pattern for an empty value")
    body = "".join(parts)
    if TOKEN_RE.match(value) is not None:
        # Asymmetric on purpose. The lookbehind excludes `.` and `-` so a bare
        # `claude` rule cannot rewrite the tail of `.claude/MODEL_NOTES.md`
        # after the longer path rules have had their chance at it. The
        # lookahead must NOT exclude `.`, or the same rule would miss every
        # occurrence that ends a sentence.
        body = rf"(?<![\w.-]){body}(?![\w-])"
    return re.compile(body)


@dataclass(frozen=True)
class Rule:
    """One vocabulary substitution: `pattern` → `replacement`."""

    key: str
    pattern: re.Pattern[str]
    replacement: str
    length: int


def _values_for(
    key: str, value: str, by_root: dict[str, dict[str, str]]
) -> list[str]:
    """This root's value for `key`, plus any shorter dialect nested inside it.

    Rules are otherwise built from one root's own values, which makes the
    normalization asymmetric wherever one harness's value contains another's.
    `AGENT_DOC` is `AGENTS.md or CLAUDE.md` on Claude and `AGENTS.md`
    elsewhere, so the identical sentence `Read AGENTS.md first` was tagged on
    two roots and left alone on the third — a residual line for text that does
    not differ, and one that puts zero out of reach for every skill naming the
    file.

    Only a *substring* is borrowed, which is what keeps this from erasing real
    divergence. `claude` is not inside `codex`, so no engine name is ever
    rewritten to another engine's token; the borrow fires exactly where two
    dialects already spell the same slot the same way.
    """
    nested = {
        found
        for other in by_root.values()
        if (found := other.get(key)) is not None
        and found != value
        and found.strip()
        and found in value
    }
    return [value, *sorted(nested)]


def build_rules(
    profiles: Iterable[ProfileLike], skill_names: Iterable[str]
) -> dict[str, list[Rule]]:
    """Per prompt root, the ordered substitutions that neutralize its dialect.

    Longest value first, so `.claude/skills` is consumed before `claude` gets
    a look at it. Ties break on the key name to keep the order deterministic
    between runs and between machines.
    """
    by_root: dict[str, dict[str, str]] = {}
    for profile in profiles:
        root = profile.root
        declared: dict[str, object] = dict(profile.values)
        # The renderer takes the prompt root from `root:` rather than from a
        # value, so no profile declares it — but it is the single most common
        # harness-specific string in the prose, and a parity diff that cannot
        # see through `.claude/MODEL_NOTES.md` vs `.agents/GEMINI_NOTES.md`
        # is not measuring divergence, it is measuring spelling.
        declared["ROOT"] = root
        # A `None` is legal in a profile (the renderer's collapse-key contract)
        # and has no text to reverse-substitute, so it is dropped here rather
        # than crashing the lint on a valid profile.
        by_root[root] = {k: v for k, v in declared.items() if isinstance(v, str)}

    # A key empty anywhere is deleted everywhere; see the module docstring.
    empty_anywhere = {
        key
        for values in by_root.values()
        for key, value in values.items()
        if not value.strip()
    }

    rules: dict[str, list[Rule]] = {}
    for root, values in by_root.items():
        built: list[Rule] = []
        for key, value in values.items():
            if key in STRUCTURAL_KEYS or not value.strip():
                continue
            token = ALIASES.get(key, key)
            replacement = "" if key in empty_anywhere else f"<<{token}>>"
            for text in _values_for(key, value, by_root):
                pattern = _value_pattern(text)
                if key in CASE_INSENSITIVE_KEYS:
                    pattern = re.compile(pattern.pattern, re.IGNORECASE)
                built.append(Rule(key, pattern, replacement, len(text)))
        built.sort(key=lambda rule: (-rule.length, rule.key))
        rules[root] = built

    invoke = _invoke_pattern(skill_names)
    for root in by_root:
        if invoke is not None:
            rules[root].append(Rule("INVOKE", invoke, r"\1", 0))
    return rules


def _invoke_pattern(skill_names: Iterable[str]) -> re.Pattern[str] | None:
    """Strip a leading `/` from a skill reference, leaving the bare name.

    Normalizing *towards* the bare name rather than towards a placeholder is
    what makes this symmetric: the harnesses that address a skill bare need no
    rule at all, and neither side gains a token the other lacks.

    The lookbehind is what keeps it off real paths. By the time this runs the
    path-valued keys have already consumed `<<SKILLS_ROOT>>/critique`, so the
    character before the slash is `>`; a hand-written `skills/critique` ends
    in a word character. Both are excluded.
    """
    names = sorted({name for name in skill_names if name}, key=len, reverse=True)
    if not names:
        return None
    alternation = "|".join(re.escape(name) for name in names)
    return re.compile(rf"(?<![\w/>.\-])/({alternation})(?![\w-])")


def normalize(text: str, rules: list[Rule]) -> str:
    for rule in rules:
        text = rule.pattern.sub(rule.replacement, text)
    return text


# --------------------------------------------------------------------------
# the skill trees
# --------------------------------------------------------------------------


def rendered_roster() -> set[str]:
    """Skills the renderer owns. Their roots are generated, not copies."""
    if not RENDERED_SRC.is_dir():
        return set()
    return {entry.name for entry in RENDERED_SRC.iterdir() if entry.is_dir()}


def _skills_in(root: str, repo_root: Path = REPO_ROOT) -> set[str]:
    directory = repo_root / root / "skills"
    if not directory.is_dir():
        return set()
    return {entry.name for entry in directory.iterdir() if entry.is_dir()}


def shared_skills(repo_root: Path = REPO_ROOT) -> dict[str, list[str]]:
    """Unrendered skills present in more than one root, mapped to those roots.

    A skill in exactly one root has nothing to be in parity *with* — that is
    what a per-harness skill looks like, and it needs no justification.
    """
    per_root = {root: _skills_in(root, repo_root) for root in ROOT_ORDER}
    rendered = rendered_roster()
    shared: dict[str, list[str]] = {}
    for name in sorted(set().union(*per_root.values()) if per_root else set()):
        if name in rendered:
            continue
        roots = [root for root in ROOT_ORDER if name in per_root[root]]
        if len(roots) > 1:
            shared[name] = roots
    return shared


def _skill_files(root: str, skill: str, repo_root: Path = REPO_ROOT) -> dict[str, Path]:
    """Relative-path → absolute-path for every real file in one copy."""
    base = repo_root / root / "skills" / skill
    if base.is_symlink():
        raise ParityError(f"{base}: skill must not be a symlink")
    found: dict[str, Path] = {}
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if IGNORED_DIR_NAMES.intersection(relative.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        if path.is_symlink():
            raise ParityError(f"{path}: skill asset must not be a symlink")
        if not path.is_file():
            continue
        found[relative.as_posix()] = path
    return found


def _read(path: Path) -> str | None:
    """Decoded text, or `None` for a file this lint cannot diff line-wise."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


@dataclass
class PairResult:
    left: str
    right: str
    lines: int = 0
    only_left: list[str] = field(default_factory=list)
    only_right: list[str] = field(default_factory=list)
    diff: list[str] = field(default_factory=list)


@dataclass
class SkillResult:
    skill: str
    roots: list[str]
    pairs: list[PairResult]

    @property
    def residual_lines(self) -> int:
        return sum(pair.lines for pair in self.pairs)

    @property
    def only_in(self) -> list[str]:
        seen: list[str] = []
        for pair in self.pairs:
            for root, names in ((pair.left, pair.only_left), (pair.right, pair.only_right)):
                for name in names:
                    entry = f"{root}:{name}"
                    if entry not in seen:
                        seen.append(entry)
        return sorted(seen)


def compare_pair(
    skill: str,
    left: str,
    right: str,
    rules: dict[str, list[Rule]],
    repo_root: Path = REPO_ROOT,
) -> PairResult:
    """Diff two copies of one skill after normalizing each root's dialect.

    A file present in only one root counts every one of its lines. It is the
    largest kind of divergence there is — a whole document one harness has and
    the other does not — and reporting it as a footnote next to a line count
    would let it hide behind a small number.
    """
    result = PairResult(left=left, right=right)
    left_files = _skill_files(left, skill, repo_root)
    right_files = _skill_files(right, skill, repo_root)

    for name in sorted(set(left_files) | set(right_files)):
        left_path = left_files.get(name)
        right_path = right_files.get(name)
        left_text = _read(left_path) if left_path is not None else None
        right_text = _read(right_path) if right_path is not None else None

        if left_path is None or right_path is None:
            present = left_text if left_path is not None else right_text
            if left_path is None:
                result.only_right.append(name)
            else:
                result.only_left.append(name)
            # Presence itself is divergence, including empty package markers.
            result.lines += max(1, len(present.splitlines())) if present is not None else 1
            continue

        if left_text is None or right_text is None:
            # Undecodable on at least one side. Identical bytes are still
            # parity, so only a difference is a problem — and a difference here
            # cannot be measured at all. Scoring it as one line would let a
            # whole document's divergence sit under a ceiling of 1 and make the
            # number in the allowlist untrue, so it is rejected the same way a
            # symlink is: the payload has to become diffable text.
            if left_path.read_bytes() != right_path.read_bytes():
                raise ParityError(
                    f"{left}/skills/{skill}/{name} and {right}/skills/{skill}/{name} "
                    f"differ but are not both UTF-8 text, so the divergence cannot "
                    f"be measured. Prompt payloads must be diffable text."
                )
            continue

        left_lines = normalize(left_text, rules[left]).splitlines(keepends=True)
        right_lines = normalize(right_text, rules[right]).splitlines(keepends=True)
        delta = list(
            difflib.unified_diff(
                left_lines,
                right_lines,
                fromfile=f"{left}/skills/{skill}/{name}",
                tofile=f"{right}/skills/{skill}/{name}",
            )
        )
        changed = [
            line
            # Only the first two records are file headers. Real content can
            # start with `--` or `++` (frontmatter and shell options included).
            for line in delta[2:]
            if line.startswith(("+", "-"))
        ]
        result.lines += len(changed)
        if delta:
            result.diff.extend(line.rstrip("\n") for line in delta)
    return result


def measure(
    rules: dict[str, list[Rule]], repo_root: Path = REPO_ROOT
) -> list[SkillResult]:
    results: list[SkillResult] = []
    for skill, roots in shared_skills(repo_root).items():
        pairs = [
            compare_pair(skill, left, right, rules, repo_root)
            for left, right in zip(roots, roots[1:])
        ]
        results.append(SkillResult(skill=skill, roots=roots, pairs=pairs))
    return results


# --------------------------------------------------------------------------
# the allowlist
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    skill: str
    kind: str  # "recorded" | "held"
    citation: str  # a `docs/decisions/` filename, or an issue reference
    reason: str
    ceiling: int | None


def load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[str, Entry]:
    """Parse and structurally validate the allowlist.

    Every failure here is fatal rather than skipped. An allowlist that half
    parses is worse than none: the entries it dropped are the divergences
    nobody is accountable for.
    """
    if path.is_symlink():
        raise ParityError(f"{path}: allowlist must not be a symlink")
    if not path.exists():
        raise ParityError(f"{path}: allowlist not found")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ParityError(f"{path}: allowlist must be a mapping")

    unknown = set(document) - {"recorded", "held"}
    if unknown:
        raise ParityError(f"{path}: unknown top-level keys: {sorted(unknown)}")

    entries: dict[str, Entry] = {}
    for kind in ("recorded", "held"):
        block = document.get(kind)
        if block is None:
            continue
        if not isinstance(block, list):
            raise ParityError(f"{path}: `{kind}` must be a list")
        for raw in block:
            entry = _parse_entry(path, kind, raw)
            if entry.skill in entries:
                raise ParityError(f"{path}: `{entry.skill}` is listed twice")
            entries[entry.skill] = entry
    return entries


def _parse_entry(path: Path, kind: str, raw: object) -> Entry:
    if not isinstance(raw, dict):
        raise ParityError(f"{path}: every `{kind}` entry must be a mapping")
    allowed = {"skill", "reason"} | ({"record"} if kind == "recorded" else {"issue", "ceiling"})
    unknown = set(raw) - allowed
    if unknown:
        raise ParityError(f"{path}: `{kind}` entry has unknown keys: {sorted(unknown)}")

    skill = raw.get("skill")
    if not isinstance(skill, str) or not skill:
        raise ParityError(f"{path}: every `{kind}` entry needs a `skill`")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ParityError(f"{path}: `{skill}` needs a `reason`")

    if kind == "recorded":
        raw_record = raw.get("record")
        # One divergence can rest on more than one record — a specific one for
        # the behaviour and the standing one for the lineage — so a list is
        # accepted and a bare string is the one-record shorthand.
        records = [raw_record] if isinstance(raw_record, str) else raw_record
        if not isinstance(records, list) or not records:
            raise ParityError(
                f"{path}: `{skill}` needs a `record` (a filename, or a list of them)"
            )
        for record in records:
            if not isinstance(record, str) or not record:
                raise ParityError(f"{path}: `{skill}` has a malformed `record` entry")
            # The record is the whole point of the entry, so it is resolved
            # against the filesystem rather than trusted as a string. A
            # citation to a file nobody wrote is exactly the rot this lint
            # exists to stop.
            if "/" in record or record.startswith("."):
                raise ParityError(
                    f"{path}: `{skill}` record must be a bare filename in "
                    f"docs/decisions/: {record!r}"
                )
            if record == "README.md" or not (DECISIONS_DIR / record).is_file():
                raise ParityError(
                    f"{path}: `{skill}` cites a record that does not exist: "
                    f"docs/decisions/{record}"
                )
        return Entry(skill, kind, ", ".join(records), reason, None)

    issue = raw.get("issue")
    # `isinstance(True, int)` is true, so the bool guard is load-bearing here
    # exactly as it is on `ceiling` below: `issue: true` would otherwise be
    # accepted and cited as `#True`.
    #
    # The shape is checked; that the issue exists is not. A `record` is
    # resolved against the filesystem because it costs a `stat`, and the
    # asymmetry with an issue reference is deliberate — confirming one needs a
    # network call, and a lint that fails on a rate limit or an expired token
    # is a lint people learn to skip.
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise ParityError(f"{path}: `{skill}` needs an `issue` number to be held against")
    ceiling = raw.get("ceiling")
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 0:
        raise ParityError(f"{path}: `{skill}` needs a non-negative integer `ceiling`")
    return Entry(skill, kind, f"#{issue}", reason, ceiling)


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------


def evaluate(
    results: list[SkillResult], entries: dict[str, Entry]
) -> tuple[list[str], list[str]]:
    """Return (violations, promotion candidates)."""
    violations: list[str] = []
    candidates: list[str] = []
    measured = {result.skill: result for result in results}

    for skill in sorted(set(entries) - set(measured)):
        violations.append(
            f"docs/decisions/parity-allowlist.yml: `{skill}` is not a shared unrendered "
            f"skill — it is rendered, absent, or present in only one prompt root. "
            f"Remove the entry."
        )

    for skill in sorted(measured):
        result = measured[skill]
        entry = entries.get(skill)
        residual = result.residual_lines

        if entry is None:
            if residual == 0:
                candidates.append(skill)
            else:
                violations.append(
                    f"{skill}: {residual} residual line(s) across {' → '.join(result.roots)} "
                    f"with no allowlist entry. Reconcile the copies, or add an entry to "
                    f"docs/decisions/parity-allowlist.yml citing a docs/decisions/ record "
                    f"(deliberate) or a tracking issue and ceiling (drift)."
                )
            continue

        if residual == 0:
            violations.append(
                f"{skill}: stale entry — {entry.kind} against {entry.citation}, but the "
                f"copies now agree at 0 residual lines. The divergence it describes is "
                f"gone: promote the skill into prompts/skills/ and drop the entry, or "
                f"correct the entry if the disposition was wrong."
            )
            continue

        if entry.kind == "held":
            assert entry.ceiling is not None
            if residual > entry.ceiling:
                violations.append(
                    f"{skill}: divergence grew to {residual} residual line(s), above the "
                    f"ceiling of {entry.ceiling} held against {entry.citation}. Held drift "
                    f"may shrink, never grow."
                )
            elif residual < entry.ceiling:
                violations.append(
                    f"{skill}: down to {residual} residual line(s) from a ceiling of "
                    f"{entry.ceiling}. Lower the ceiling in "
                    f"docs/decisions/parity-allowlist.yml to {residual} so the ratchet holds."
                )
    return violations, candidates


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def format_table(results: list[SkillResult], entries: dict[str, Entry]) -> str:
    width = max((len(r.skill) for r in results), default=5)
    lines = [f"{'skill'.ljust(width)}  residual  disposition"]
    for result in sorted(results, key=lambda r: (-r.residual_lines, r.skill)):
        entry = entries.get(result.skill)
        if entry is None:
            disposition = "promotion candidate" if result.residual_lines == 0 else "UNLISTED"
        elif entry.kind == "recorded":
            disposition = f"recorded ({entry.citation})"
        else:
            disposition = f"held ({entry.citation}, ceiling {entry.ceiling})"
        lines.append(
            f"{result.skill.ljust(width)}  {result.residual_lines:>8}  {disposition}"
        )
        for name in result.only_in:
            lines.append(f"{' ' * width}            only in {name}")
    return "\n".join(lines)


def _write_step_summary(candidates: list[str]) -> None:
    """Put promotion candidates where a human will actually see them.

    A non-fatal finding printed into a green job's log is read by nobody, and
    a skill that reached zero residuals then sat unnoticed for months is the
    failure mode this whole mechanism is supposed to prevent.
    """
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destination or not candidates:
        return
    body = [
        "### Prompt parity — promotion candidates",
        "",
        "These skills are byte-identical across their prompt roots once harness "
        "vocabulary is normalized, so they can be single-sourced in `prompts/skills/` "
        "and rendered:",
        "",
    ]
    body.extend(f"- `{skill}`" for skill in candidates)
    body += ["", PROMOTION_CAVEAT, ""]
    try:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write("\n".join(body))
    except OSError:
        # A summary is a courtesy. Losing it must not fail an otherwise clean
        # gate, and the same list is on stdout regardless.
        pass


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def _skill_universe() -> Iterator[str]:
    """Every skill name in any root, for the `INVOKE` slash-stripping rule."""
    for root in ROOT_ORDER:
        yield from _skills_in(root)
    yield from rendered_roster()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the residual table and exit 0 regardless of violations",
    )
    parser.add_argument(
        "--diff",
        metavar="SKILL",
        help="print the normalized diff for one skill and exit 0",
    )
    args = parser.parse_args(argv)

    try:
        renderer = _load_render_prompts()
        profiles = renderer.load_profiles()
        check_root_coverage(profiles)
        check_root_trees()
        rules = build_rules(profiles, set(_skill_universe()))
        results = measure(rules)
        entries = load_allowlist()
    # `ValueError` is in this list because importing the renderer's loader
    # means inheriting its failure modes, deliberately. This lint is a
    # *consumer* of that reader rather than a second one, so a profile the
    # renderer rejects must fail here too — and as a config error (exit 2),
    # not as a parity violation (exit 1) or a traceback. Any validation the
    # renderer grows arrives here automatically, which is the point: one
    # reader of the profile schema, one verdict on what a valid profile is.
    except (ParityError, ValueError, OSError, yaml.YAMLError) as error:
        sys.stderr.write(f"prompt-parity: {error}\n")
        return 2

    if args.diff:
        match = [result for result in results if result.skill == args.diff]
        if not match:
            sys.stderr.write(
                f"prompt-parity: `{args.diff}` is not a shared unrendered skill; "
                f"known: {', '.join(sorted(r.skill for r in results))}\n"
            )
            return 2
        for pair in match[0].pairs:
            print(f"### {pair.left} → {pair.right} ({pair.lines} residual line(s))")
            print("\n".join(pair.diff))
        return 0

    violations, candidates = evaluate(results, entries)

    if args.report:
        print(format_table(results, entries))
        return 0

    if candidates:
        print(
            "Promotion candidates (zero residuals — single-sourceable in prompts/skills/): "
            + ", ".join(candidates)
        )
        print(f"  {PROMOTION_CAVEAT}")
        _write_step_summary(candidates)

    if violations:
        sys.stderr.write(
            "Every divergence between shared unrendered skill copies needs a disposition:\n"
        )
        for violation in violations:
            sys.stderr.write(f"  ❌ {violation}\n")
        sys.stderr.write("\n")
        sys.stderr.write(format_table(results, entries))
        sys.stderr.write("\n")
        return 1

    print(f"OK: {len(results)} shared unrendered skill(s), every divergence accounted for")
    return 0


if __name__ == "__main__":
    sys.exit(main())
