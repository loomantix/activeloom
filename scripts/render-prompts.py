#!/usr/bin/env python3
"""Render the single-sourced skills in `prompts/skills/` into each harness root.

One source file per skill, one profile per harness, `<<KEY>>` substitution and
nothing else. The rendered outputs (`.claude/skills/<skill>/…`,
`.codex/skills/…`, `.agents/skills/…`) are committed, so a reader of any
harness root sees the real prompt rather than a template, and consumers keep
receiving the harness-specific distribution artifacts they select.

**Zero conditionals.** The substitution engine has no branching construct and
none is planned: a skill whose text must differ structurally between harnesses
is per-harness by definition and stays unrendered, tracked instead by a
`docs/decisions/` record. See `docs/prompt-rendering.md`.

Markdown is normalized with Prettier after substitution. This is not cosmetic:
substituting a value of a different width into a Markdown table changes the
column alignment Prettier enforces, so a render that skipped the formatter
would fail the repo's own `Prettier --check` on its own output. The formatter
therefore belongs *inside* the render step, and the pinned version below is the
one the CI `Prettier --check` step uses.

Usage:
    python3 scripts/render-prompts.py            # write the harness roots
    python3 scripts/render-prompts.py --check    # verify committed == rendered

Exit codes: 0 clean, 1 drift (`--check`) or render error, 2 usage error.
"""

from __future__ import annotations

import argparse
import filecmp
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import cast

import yaml

# Where sibling scripts live, and where rendered output goes. Normally one is
# the parent of the other, but they are different concepts and only `REPO_ROOT`
# is a rendering destination — keeping them apart is what lets the tests point
# the destination at a scratch tree without hiding `sync-engine.py`.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
SKILLS_SRC = PROMPTS_DIR / "skills"
PROFILES_DIR = PROMPTS_DIR / "profiles"
MANIFEST_PATH = PROMPTS_DIR / "rendered-files.txt"

# The prompt stack's semantic version, single-sourced here and stamped into
# every emitted stack manifest. It is not the sync protocol pin: that tag is
# force-moved whenever content changes, so two consumers "on sync-v1" at
# different times are running different prompts and the tag carries no content
# identity. It is not `hashInputVersion` either — that versions the *hash input
# definition*. This one versions the prompts themselves, and it is what makes a
# telemetry comparison legible: a digest identifies a generation but does not
# order two of them.
VERSION_PATH = REPO_ROOT / "PROMPT_STACK_VERSION"

# Deliberately strict. The value is emitted into a telemetry record through the
# ledger's `--prompt-stack-version`, which accepts a protocol token — a looser
# grammar than this. Anything that is not three dotted integers is a typo here,
# and a typo that ships is a prompt generation nobody can order.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Emitted into each harness root that declares a `prompt_stack`. Read by that
# root's `skills/critique/scripts/prompt-stack-hash.js`.
STACK_MANIFEST_NAME = "prompt-stack.json"

# The manifest's own schema version, carried in the file so a consumer holding
# an older synced copy is told what it is holding rather than guessed at. A
# reader that does not recognise the value must abstain, not improvise.
STACK_MANIFEST_SCHEMA_VERSION = 1

# Same pin as the `Prettier --check` step in `.github/workflows/ci.yml`. If one
# moves and the other does not, CI fails on the renderer's own output — which is
# the intended failure, but the fix is to move both.
PRETTIER = "prettier@3.8.3"

# Suffixes Prettier owns. Everything else is written through byte-for-byte.
FORMATTED_SUFFIXES = (".md",)

# `FM_EXTRAS` is the one key whose placeholder sits alone on a line and must
# take the line with it when the value is empty — a harness with no extra
# frontmatter keys would otherwise get a blank line inside its YAML block.
# Every other key is substituted inline, where an empty value is just an empty
# string. Kept explicit rather than inferred so a new whole-line placeholder is
# a deliberate addition.
COLLAPSE_KEYS = ("FM_EXTRAS",)

# Rendering and deletion are intentionally confined to the harness roots this
# repository ships. Adding a harness is a code-reviewed authority change, not a
# side effect of adding an arbitrary profile file. A removed profile remains in
# this set so its previously rendered outputs can be retired safely.
SUPPORTED_PROFILE_ROOTS = frozenset({".agents", ".claude", ".codex"})

# A removed source skill must be named here for the one render that retires its
# old outputs, then may be removed after the generated-path manifest is clean.
# This keeps retirement possible without letting the deletion manifest promote
# an unrelated hand-authored skill into the renderer's ownership domain.
RETIRED_SKILLS: frozenset[str] = frozenset()

# Build artifacts that appear *inside* the source tree and must never be
# rendered into a harness root. `prompts/skills/issues/scripts/*.py` are real
# Python, so CI's `Compile-check every Python source` step (and any local
# `py_compile` or import) drops `__pycache__/*.pyc` next to them. Those are
# gitignored, so they are invisible in review, but `rglob("*")` sees them and
# would copy stale bytecode into all three roots.
IGNORED_DIR_NAMES = frozenset({"__pycache__"})
IGNORED_SUFFIXES = (".pyc", ".pyo")

# Deliberately looser than the engine's `<<KEY>>` pattern, because its whole
# purpose is to catch tokens that pattern would *miss* — a key mangled into
# `<<REVIEW*CHAIN_POINTER>>` is no longer a `<<KEY>>` but is still a bug.
#
# It must not, however, match a shell heredoc: `issues/SKILL.md` is a rendered
# skill and contains `cat > /tmp/issue-body.md << 'BODY'`. Excluding whitespace
# and quotes from the token body is what separates a placeholder-shaped run from
# `<< 'BODY'`, `<<'PY'`, and `<<<"$VAR"`.
RESIDUE_RE = re.compile(r"""<<[^<>\s'"]{1,80}>>""")


def _load_sync_engine() -> ModuleType:
    """Import `scripts/sync-engine.py` for its substitution primitives.

    The renderer deliberately does not carry its own `<<KEY>>` implementation.
    Two substitution engines in one repo drift, and this one is already the
    audited path every consumer sync runs through — including the empty-line
    collapse contract and the fail-closed check for a placeholder with no
    configured value. The filename is hyphenated and so not importable, hence
    `spec_from_file_location` (`tests/conftest.py` loads it the same way).
    """
    path = SCRIPT_DIR / "sync-engine.py"
    spec = importlib.util.spec_from_file_location("sync_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_engine"] = module
    spec.loader.exec_module(module)
    return module


class Profile:
    """One harness: the root it renders into and the values it substitutes."""

    def __init__(self, path: Path) -> None:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError(f"{path}: profile must be a mapping")

        root = doc.get("root")
        if not isinstance(root, str) or not root:
            raise ValueError(f"{path}: `root` must be a non-empty string")
        # A profile root is joined to the repo root and must stay inside it.
        # Profiles are checked-in config, not user input, but a rendering step
        # that can be pointed at `/` by a typo is worth one cheap assertion.
        root_path = Path(root)
        if root.startswith("~") or root_path.is_absolute() or ".." in root_path.parts:
            raise ValueError(
                f"{path}: `root` must be a relative path inside the repo: {root!r}"
            )
        normalized_root = root_path.as_posix()
        if normalized_root == ".":
            raise ValueError(
                f"{path}: `root` must name a prompt root below the repo: {root!r}"
            )
        if normalized_root not in SUPPORTED_PROFILE_ROOTS:
            raise ValueError(
                f"{path}: `root` must be one of the supported harness roots: "
                f"{sorted(SUPPORTED_PROFILE_ROOTS)}; got {root!r}"
            )

        # `is None` rather than a falsy test: an omitted key is fine and means
        # "empty", but `values: []` is a malformed profile and must not be
        # silently coerced to an empty mapping.
        values = doc.get("values")
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ValueError(f"{path}: `values` must be a mapping")
        values = _validated_values(values, path, "values")

        skills = doc.get("skills")
        if skills is None:
            skills = {}
        if not isinstance(skills, dict):
            raise ValueError(f"{path}: `skills` must be a mapping")
        for skill, overrides in skills.items():
            if not isinstance(skill, str):
                raise ValueError(f"{path}: skill names must be strings")
            if overrides is not None and not isinstance(overrides, dict):
                raise ValueError(f"{path}: `skills.{skill}` must be a mapping")
            if overrides is not None:
                skills[skill] = _validated_values(overrides, path, f"skills.{skill}")

        self.path = path
        self.name = path.stem
        self.root = normalized_root
        self.values = values
        self.skills = cast(dict[str, dict[str, object] | None], skills)
        self.prompt_stack = _validated_prompt_stack(doc.get("prompt_stack"), path, normalized_root)

    def values_for(self, skill: str) -> dict[str, object]:
        """Profile-wide values, with this skill's per-skill values layered on.

        Per-skill values win. They exist for vocabulary that is genuinely
        per-skill *and* per-harness — frontmatter only one harness understands,
        and the trigger prose whose presence depends on that frontmatter.
        """
        merged = dict(self.values)
        merged.update(self.skills.get(skill) or {})
        return merged


def _validated_values(
    values: dict[object, object], path: Path, label: str
) -> dict[str, object]:
    """Require substitution vocabulary to be strings, except collapse-key nulls."""
    validated: dict[str, object] = {}
    for key, value in values.items():
        if not isinstance(key, str):
            raise ValueError(f"{path}: `{label}` keys must be strings")
        if value is None:
            if key not in COLLAPSE_KEYS:
                raise ValueError(f"{path}: `{label}.{key}` must be a string")
        elif not isinstance(value, str):
            raise ValueError(f"{path}: `{label}.{key}` must be a string")
        validated[key] = value
    return validated


def _validated_prompt_stack(
    declared: object, path: Path, root: str
) -> list[str] | None:
    """Validate a profile's declared prompt stack, or return None if it has none.

    Absent and empty are different profiles. A harness with no hasher declares
    nothing and gets no manifest; a harness that declared an empty list would be
    claiming its review runs on no prompts at all, which is never true and would
    ship a manifest whose digest is null by construction.

    Paths are repo-root-relative and must sit inside the profile's own root.
    That is what keeps one harness's manifest from naming another's files: the
    digest is supposed to identify *this* engine's prompt generation, and a
    cross-root entry would fold a sibling's edits into it.
    """
    if declared is None:
        return None
    if not isinstance(declared, list) or not declared:
        raise ValueError(f"{path}: `prompt_stack` must be a non-empty list of paths")
    prefix = f"{root}/"
    stack: list[str] = []
    for entry in declared:
        if not isinstance(entry, str) or not entry:
            raise ValueError(f"{path}: `prompt_stack` entries must be non-empty strings")
        entry_path = Path(entry)
        if (
            entry.startswith("~")
            or entry_path.is_absolute()
            or ".." in entry_path.parts
            or entry_path.as_posix() != entry
        ):
            raise ValueError(
                f"{path}: `prompt_stack` entries must be plain relative paths: {entry!r}"
            )
        if not entry.startswith(prefix):
            raise ValueError(
                f"{path}: `prompt_stack` entries must sit inside {root!r}: {entry!r}"
            )
        stack.append(entry)
    if len(stack) != len(set(stack)):
        raise ValueError(f"{path}: `prompt_stack` contains duplicate paths")
    return stack


def load_prompt_stack_version() -> str:
    """Read and validate the single-sourced prompt-stack version."""
    if VERSION_PATH.is_symlink():
        raise ValueError(f"prompt-stack version file must not be a symlink: {VERSION_PATH}")
    try:
        raw = VERSION_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(
            f"missing {VERSION_PATH.name}: the prompt stack has no semantic version to stamp"
        ) from None
    version = raw.strip()
    if not VERSION_RE.match(version):
        raise ValueError(
            f"{VERSION_PATH}: version must be MAJOR.MINOR.PATCH; got {raw.strip()!r}"
        )
    return version


def load_profiles() -> list[Profile]:
    paths = sorted(PROFILES_DIR.glob("*.yml"))
    if not paths:
        raise ValueError(f"no profiles found in {PROFILES_DIR}")
    profiles = [Profile(p) for p in paths]

    roots = [p.root for p in profiles]
    duplicates = {r for r in roots if roots.count(r) > 1}
    if duplicates:
        raise ValueError(
            f"profiles share a root, so one would overwrite the other: {sorted(duplicates)}"
        )

    # Keep the containment assertion even though the current supported roots
    # are siblings. It protects the invariant if that explicit set is extended.
    for outer in profiles:
        for inner in profiles:
            if outer is inner:
                continue
            if _is_within(Path(inner.root), Path(outer.root)):
                raise ValueError(
                    f"profile roots must not nest, so one would render inside the "
                    f"other: {outer.root!r} contains {inner.root!r}"
                )
    return profiles


def _is_within(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or sits beneath it, comparing path parts.

    Lexical on purpose: these are relative repo paths that need not exist yet,
    so `resolve()` would be both unnecessary and filesystem-dependent.
    """
    return child.parts[: len(parent.parts)] == parent.parts


def rendered_roster() -> list[str]:
    """Skill names to render — every directory under `prompts/skills/`.

    Derived from the filesystem rather than a manifest so that adding a
    rendered skill is one source directory plus whatever profile values it
    needs, with no third place to forget.
    """
    if not SKILLS_SRC.is_dir():
        return []
    roster: list[str] = []
    for entry in SKILLS_SRC.iterdir():
        if entry.is_symlink() and entry.is_dir():
            raise ValueError(f"skill source directories must not be symlinks: {entry}")
        if entry.is_dir():
            roster.append(entry.name)
    return sorted(roster)


def render_tree(
    engine: ModuleType, profiles: list[Profile], destination: Path, version: str
) -> list[Path]:
    """Render every skill for every profile under `destination`.

    Returns the written paths, relative to `destination`.
    """
    written: list[Path] = []
    for skill in rendered_roster():
        source_dir = SKILLS_SRC / skill
        for source in sorted(_source_files(source_dir)):
            relative = source.relative_to(source_dir)
            raw = source.read_bytes()
            for profile in profiles:
                target = destination / profile.root / "skills" / skill / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _render_file(engine, raw, source, profile, skill, target)
                # Preserve the source's executable bit: the issues scripts are
                # synced with `mode: '0755'` and are executed from the skill.
                shutil.copymode(source, target)
                written.append(target.relative_to(destination))
    written.extend(write_stack_manifests(profiles, destination, version))
    return written


def write_stack_manifests(
    profiles: list[Profile], destination: Path, version: str
) -> list[Path]:
    """Emit `<root>/prompt-stack.json` for every profile that declares a stack.

    The manifest is a build output for the same reason the rendered skills are:
    the prompt stack's identity stops depending on two copies of a hasher
    holding the same hard-coded list in the same order, and starts depending on
    one declaration that a render either satisfies or fails on.
    """
    written: list[Path] = []
    for profile in profiles:
        if profile.prompt_stack is None:
            continue
        engine_id = profile.values.get("ENGINE_ID")
        if not isinstance(engine_id, str) or not engine_id:
            raise ValueError(
                f"{profile.path}: a profile declaring `prompt_stack` must define "
                "`ENGINE_ID`, which names the engine the manifest identifies"
            )
        relative = Path(profile.root) / STACK_MANIFEST_NAME
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _stack_manifest_text(engine_id, profile, version), encoding="utf-8"
        )
        written.append(relative)
    return written


def _stack_manifest_text(engine_id: str, profile: Profile, version: str) -> str:
    """Serialise one stack manifest.

    Two-space JSON with a trailing newline, which is what the repo's pinned
    Prettier produces — the emitted file sits inside a harness root the
    repo-wide `Prettier --check` covers, so a hand-rolled shape would fail the
    build on the renderer's own output.

    Paths are sorted here so the shipped artifact is the ordered one. The
    consumer sorts again rather than trusting this, because a manifest is a file
    that can be edited in a consumer checkout and the hash-input definition must
    not be one of the things such an edit can change.
    """
    payload = {
        "manifestVersion": STACK_MANIFEST_SCHEMA_VERSION,
        "promptStackVersion": version,
        "engine": engine_id,
        "root": profile.root,
        "files": sorted(profile.prompt_stack or []),
    }
    return f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n"


def verify_stack_declarations(profiles: list[Profile], written: list[Path]) -> None:
    """Fail the render when a declared prompt-stack file does not exist.

    Without this the declaration degrades silently: the hasher reports a missing
    file as `absent`, which is the honest answer for a consumer that never
    received it and the wrong one for a file this repository renamed. Absence
    would move every digest at once and read downstream as a real
    prompt-generation change, with nothing anywhere saying a path went stale.
    """
    generated = {path.as_posix() for path in written}
    for profile in profiles:
        for entry in profile.prompt_stack or []:
            if entry in generated or (REPO_ROOT / entry).is_file():
                continue
            raise ValueError(
                f"{profile.path}: `prompt_stack` names a file that does not exist: "
                f"{entry!r}. Update the declaration when a prompt file is renamed "
                "or retired — a stack entry that silently reads as absent moves "
                "every digest at once."
            )


def _source_files(source_dir: Path) -> Iterator[Path]:
    """Every renderable file under a skill source directory.

    Skips build artifacts — see `IGNORED_DIR_NAMES` / `IGNORED_SUFFIXES`. They
    are gitignored, so a reviewer never sees them, which is exactly why the
    renderer has to exclude them rather than rely on the tree being clean.
    """
    for path in source_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"skill source entries must not be symlinks: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source_dir)
        if IGNORED_DIR_NAMES.intersection(relative.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        yield path


def _validate_generated_path(path: Path) -> None:
    """Prove a generated path belongs to an explicit root and rendered skill."""
    owned_skills = set(rendered_roster()) | set(RETIRED_SKILLS)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"path is outside the renderer ownership domain: {path.as_posix()!r}"
        )
    # The stack manifest is the one generated file that is not inside a skill
    # directory. It is admitted by exact name at the top of a supported root and
    # nothing else is, so widening the domain by one file does not widen the
    # deletion authority the generated-path inventory carries.
    if (
        len(path.parts) == 2
        and path.parts[0] in SUPPORTED_PROFILE_ROOTS
        and path.parts[1] == STACK_MANIFEST_NAME
    ):
        return
    if (
        len(path.parts) < 4
        or path.parts[0] not in SUPPORTED_PROFILE_ROOTS
        or path.parts[1] != "skills"
        or path.parts[2] not in owned_skills
    ):
        raise ValueError(
            f"path is outside the renderer ownership domain: {path.as_posix()!r}"
        )


def _destination_path(relative: Path) -> Path:
    """Return a repository destination after rejecting every symlink component."""
    _validate_generated_path(relative)
    current = REPO_ROOT
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(
                f"generated destinations must not contain symlinks: {current}"
            )
    return current


def _manifest_text(written: list[Path]) -> str:
    """Canonical generated-path ownership record."""
    return "".join(f"{path.as_posix()}\n" for path in sorted(set(written)))


def _load_manifest(profiles: list[Profile]) -> list[Path]:
    """Read and validate the previously generated path set.

    Every entry is a path `_publish_outputs` may delete, which makes this the
    one input in the renderer that authorizes destruction. It is therefore
    constrained to the domain the renderer actually owns — `<declared profile
    root>/skills/...` — and not merely to "some directory named `skills`".

    Without the root check, `prompts/skills/<skill>/SKILL.md` satisfies every
    other condition, so a hand-edited or badly-merged line deletes a rendered
    skill's single source on the next write-mode run and reports success.
    """
    if MANIFEST_PATH.is_symlink():
        raise ValueError(
            f"generated-path manifest must not be a symlink: {MANIFEST_PATH}"
        )
    if not MANIFEST_PATH.exists():
        return []
    paths: list[Path] = []
    for raw in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        path = Path(raw)
        try:
            if not raw:
                raise ValueError
            _validate_generated_path(path)
        except ValueError:
            raise ValueError(f"{MANIFEST_PATH}: invalid generated path: {raw!r}")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise ValueError(f"{MANIFEST_PATH}: duplicate generated paths")
    return paths


def unowned_files(profiles: list[Profile], written: list[Path]) -> list[Path]:
    """Return files sitting inside a rendered skill directory that no render wrote.

    The generated-path inventory answers "did every file we produced survive?".
    It cannot answer "is there anything else in there?", because a file the
    renderer never emitted is in neither the inventory nor the written set and
    so is reported by nothing — a hand-added `EXTRA.md`, or a `scripts/exfil.py`
    that a `SKILL.md` sources, sat in a tree the drift gate called clean.

    The ownership rule this closes the gap with: a rendered skill's directory in
    a harness root is **wholly** owned by the renderer. Everything in it comes
    from `prompts/skills/<skill>/`, so anything else is drift regardless of who
    put it there — which is the only rule that does not require the gate to
    guess at intent. The single exception is the build artifacts the render step
    already excludes at the source (`__pycache__`, `.pyc`), because CI's own
    compile step drops those next to the rendered issue scripts and failing on
    them would turn the gate red for something no PR did.

    Retired skills are swept too, so retiring a skill empties its directory
    rather than leaving whatever the inventory did not happen to name.
    """
    owned_skills = sorted(set(rendered_roster()) | set(RETIRED_SKILLS))
    generated = set(written)
    found: list[Path] = []
    for profile in profiles:
        for skill in owned_skills:
            skill_dir = REPO_ROOT / profile.root / "skills" / skill
            for path in _existing_files(skill_dir):
                relative = path.relative_to(REPO_ROOT)
                if relative in generated:
                    continue
                found.append(relative)
    return sorted(set(found))


def _existing_files(directory: Path) -> Iterator[Path]:
    """Walk a rendered skill directory without following symlinked directories.

    A symlink is yielded as a file so it is reported and removed like any other
    unowned entry, and never descended into: a link to `/` inside a harness root
    would otherwise turn a drift check into a filesystem-wide walk, and the
    thing being defended against here is a hostile addition to that directory.
    """
    if directory.is_symlink() or not directory.is_dir():
        return
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink():
            yield entry
        elif entry.is_dir():
            if entry.name in IGNORED_DIR_NAMES:
                continue
            yield from _existing_files(entry)
        elif entry.is_file():
            if entry.suffix in IGNORED_SUFFIXES:
                continue
            yield entry


def _remove_unowned(relative: Path) -> None:
    """Delete one unowned file after re-proving it is inside a skill we own."""
    _validate_generated_path(relative)
    if len(relative.parts) < 4 or relative.parts[1] != "skills":
        # `_validate_generated_path` also admits `<root>/prompt-stack.json`,
        # which the sweep never produces. Refuse anything that reached here by
        # another route rather than trusting the shared validator's wider domain.
        raise ValueError(
            f"refusing to remove a path outside a rendered skill directory: "
            f"{relative.as_posix()!r}"
        )
    target = REPO_ROOT / relative
    if target.is_file() or target.is_symlink():
        target.unlink()


def _publish_outputs(
    staging: Path,
    written: list[Path],
    previously_owned: list[Path],
    profiles: list[Profile],
    unowned: list[Path],
) -> None:
    """Replace the generated path set and remove outputs retired from source."""
    current = set(written)
    for relative in unowned:
        _remove_unowned(relative)
    for relative in sorted(set(previously_owned) - current):
        # Belt and braces with `_load_manifest`: the deleting line revalidates
        # both ownership and every existing path component.
        target = _destination_path(relative)
        if target.is_file() or target.is_symlink():
            target.unlink()
    for relative in written:
        target = _destination_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staging / relative, target)
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(_manifest_text(written), encoding="utf-8")


def _render_file(
    engine: ModuleType,
    raw: bytes,
    source: Path,
    profile: Profile,
    skill: str,
    target: Path,
) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Not text, so not substitutable — copy it through untouched rather
        # than corrupting it or refusing to render the whole roster.
        #
        # But byte-passthrough skips substitution *and* every guard below it,
        # so a text file that merely fails to decode (one latin-1 smart quote
        # is enough) would ship with its `<<KEY>>` placeholders intact into all
        # three harness roots, exit 0, and then match itself on `--check`.
        # Passthrough is for genuinely binary assets; a source carrying
        # placeholder delimiters is text with an encoding defect.
        if _delimiter_residue(raw.decode("utf-8", "replace")):
            raise ValueError(
                f"{source} is not valid UTF-8 but contains placeholder delimiters. "
                "Re-save it as UTF-8; byte-passthrough is for binary assets only."
            )
        target.write_bytes(raw)
        return

    values = profile.values_for(skill)
    found = sorted(set(engine.PLACEHOLDER_RE.findall(text)))

    # `key not in values` catches an omitted key. A key written bare
    # (`SKILLS_ROOT:`) parses as `None`, which the engine coerces to `""` — so
    # without the second test a one-character edit renders `.//issues/...`
    # into a harness root and exits 0. `COLLAPSE_KEYS` are exempt: an empty
    # value is their normal state and the whole line is dropped.
    undefined = [
        key
        for key in found
        if key not in values or (values[key] is None and key not in COLLAPSE_KEYS)
    ]
    if undefined:
        raise ValueError(
            f"{source} uses placeholders with no value in {profile.path.name}: "
            f"{', '.join(undefined)}"
        )

    # `target_keys` is the set actually present in this file, not everything the
    # profile defines. A profile may legitimately carry vocabulary no rendered
    # skill uses yet (the review-chain keys exist for skills still held back),
    # and passing those would make the engine warn on every render.
    rendered = engine.substitute(
        text,
        values,
        target_keys=found,
        source=str(source.relative_to(REPO_ROOT)),
        collapse_empty_substitutions=[k for k in COLLAPSE_KEYS if k in found],
    )
    # Nothing that looks like a placeholder delimiter may survive into a
    # harness root. The `undefined` check above only sees tokens that match the
    # engine's `<<KEY>>` pattern, so it cannot catch a *mangled* one — and
    # mangling is a real failure mode, not a hypothetical: Prettier's Markdown
    # parser rewrote `<<REVIEW_CHAIN_POINTER>>` to `<<REVIEW*CHAIN_POINTER>>` by
    # pairing its underscores with a neighbouring `_emphasis_` span, which then
    # substituted nothing and rendered through verbatim. A blanket delimiter
    # check costs nothing and closes the whole class.
    residue = _delimiter_residue(rendered)
    if residue:
        raise ValueError(
            f"{source} rendered for {profile.name} still contains placeholder "
            f"delimiters: {', '.join(residue)}. A placeholder that survives "
            f"substitution is usually mangled — check that the source is "
            f"excluded from Prettier and that the key matches "
            f"{engine.PLACEHOLDER_RE.pattern}."
        )
    target.write_text(rendered, encoding="utf-8")


def _delimiter_residue(text: str) -> list[str]:
    """Return the distinct `<<…>>`-ish fragments left in rendered output."""
    return sorted({match.group(0) for match in RESIDUE_RE.finditer(text)})


def format_markdown(destination: Path, written: list[Path]) -> None:
    """Run Prettier over the rendered Markdown, in place."""
    targets = [str(destination / p) for p in written if p.suffix in FORMATTED_SUFFIXES]
    if not targets:
        return
    try:
        subprocess.run(
            [
                "npx",
                "--yes",
                PRETTIER,
                # The rendered tree may be a temp dir outside the repo, where
                # Prettier's config discovery would find nothing and silently
                # fall back to its defaults. Name the config explicitly.
                "--config",
                str(REPO_ROOT / ".prettierrc"),
                "--log-level",
                "warn",
                "--write",
                *targets,
            ],
            check=True,
            cwd=REPO_ROOT,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "npx not found. The renderer normalizes Markdown with "
            f"{PRETTIER}; install Node 20+ or run this in CI."
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed harness roots match a fresh render; write nothing",
    )
    args = parser.parse_args(argv)

    engine = _load_sync_engine()
    profiles = load_profiles()
    version = load_prompt_stack_version()
    roster = rendered_roster()
    if not roster:
        sys.stderr.write(f"no skill sources found under {SKILLS_SRC}\n")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        written = render_tree(engine, profiles, staging, version)
        verify_stack_declarations(profiles, written)
        format_markdown(staging, written)
        previously_owned = _load_manifest(profiles)
        unowned = unowned_files(profiles, written)

        if args.check:
            return _report_drift(staging, written, previously_owned, unowned)

        _publish_outputs(staging, written, previously_owned, profiles, unowned)

    print(
        f"rendered {len(roster)} skill(s) into {len(profiles)} harness root(s): "
        f"{', '.join(p.root for p in profiles)} at prompt stack version {version}"
    )
    return 0


def _report_drift(
    staging: Path,
    written: list[Path],
    previously_owned: list[Path],
    unowned: list[Path],
) -> int:
    missing: list[Path] = []
    differing: list[Path] = []
    stale = sorted(set(previously_owned) - set(written))
    # A stale path is already reported as stale; reporting it twice under a
    # second name would suggest two problems where there is one.
    unowned = [path for path in unowned if path not in set(stale)]
    for relative in written:
        committed = _destination_path(relative)
        if not committed.exists():
            missing.append(relative)
        elif not filecmp.cmp(staging / relative, committed, shallow=False):
            differing.append(relative)
        elif (
            staging / relative
        ).stat().st_mode & 0o111 != committed.stat().st_mode & 0o111:
            differing.append(relative)

    manifest_drift = not MANIFEST_PATH.exists() or MANIFEST_PATH.read_text(
        encoding="utf-8"
    ) != _manifest_text(written)

    if (
        not missing
        and not differing
        and not stale
        and not unowned
        and not manifest_drift
    ):
        print(f"rendered output matches all {len(written)} committed file(s)")
        return 0

    sys.stderr.write("rendered output does not match the committed harness roots.\n")
    for relative in missing:
        sys.stderr.write(f"  missing:   {relative}\n")
    for relative in differing:
        sys.stderr.write(f"  differs:   {relative}\n")
    for relative in stale:
        sys.stderr.write(f"  stale:     {relative}\n")
    for relative in unowned:
        sys.stderr.write(f"  unowned:   {relative}\n")
    if manifest_drift:
        sys.stderr.write(f"  differs:   {MANIFEST_PATH.relative_to(REPO_ROOT)}\n")
    sys.stderr.write(
        "\nThe harness roots are generated from `prompts/`. Edit the source in "
        "`prompts/skills/` (or the value in `prompts/profiles/`), then run:\n"
        "  python3 scripts/render-prompts.py\n"
    )
    if unowned:
        sys.stderr.write(
            "\nA rendered skill directory is wholly owned by the renderer, so a "
            "file it did not emit is drift whoever added it. Move it into "
            "`prompts/skills/<skill>/` if it belongs in the prompt, or delete "
            "it; a write-mode render removes it.\n"
        )
    for relative in differing:
        sys.stderr.write(f"\n--- diff: {relative} ---\n")
        # Capture and re-emit rather than handing `sys.stderr` to the child:
        # under pytest (or any wrapped stream) it has no file descriptor and
        # `subprocess` raises `io.UnsupportedOperation: fileno`.
        diff = subprocess.run(
            ["diff", "-u", str(REPO_ROOT / relative), str(staging / relative)],
            check=False,
            capture_output=True,
            text=True,
        )
        sys.stderr.write(diff.stdout)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as exc:
        sys.stderr.write(f"❌ {exc}\n")
        sys.exit(1)
