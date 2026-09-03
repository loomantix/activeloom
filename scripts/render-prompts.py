#!/usr/bin/env python3
"""Render the single-sourced skills in `prompts/skills/` into each harness root.

One source file per skill, one profile per harness, `<<KEY>>` substitution and
nothing else. The rendered outputs (`.claude/skills/<skill>/…`,
`.codex/skills/…`, `.agents/skills/…`) are committed, so a reader of any
harness root sees the real prompt rather than a template, and consumers keep
syncing from the paths they already sync from.

**Zero conditionals.** The substitution engine has no branching construct and
none is planned: a skill whose text must differ structurally between harnesses
is per-harness by definition and stays unrendered, reconciled instead by the
parity lint against a `docs/decisions/` record. See `docs/prompt-rendering.md`.

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
import re
import shutil
import subprocess
import sys
import tempfile
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
        if root.startswith(("/", "~")) or ".." in Path(root).parts:
            raise ValueError(f"{path}: `root` must be a relative path inside the repo: {root!r}")

        # `is None` rather than a falsy test: an omitted key is fine and means
        # "empty", but `values: []` is a malformed profile and must not be
        # silently coerced to an empty mapping.
        values = doc.get("values")
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ValueError(f"{path}: `values` must be a mapping")

        skills = doc.get("skills")
        if skills is None:
            skills = {}
        if not isinstance(skills, dict):
            raise ValueError(f"{path}: `skills` must be a mapping")
        for skill, overrides in skills.items():
            if overrides is not None and not isinstance(overrides, dict):
                raise ValueError(f"{path}: `skills.{skill}` must be a mapping")

        self.path = path
        self.name = path.stem
        self.root = root
        self.values = cast(dict[str, object], values)
        self.skills = cast(dict[str, dict[str, object] | None], skills)

    def values_for(self, skill: str) -> dict[str, object]:
        """Profile-wide values, with this skill's per-skill values layered on.

        Per-skill values win. They exist for vocabulary that is genuinely
        per-skill *and* per-harness — frontmatter only one harness understands,
        and the trigger prose whose presence depends on that frontmatter.
        """
        merged = dict(self.values)
        merged.update(self.skills.get(skill) or {})
        return merged


def load_profiles() -> list[Profile]:
    paths = sorted(PROFILES_DIR.glob("*.yml"))
    if not paths:
        raise ValueError(f"no profiles found in {PROFILES_DIR}")
    profiles = [Profile(p) for p in paths]

    roots = [p.root for p in profiles]
    duplicates = {r for r in roots if roots.count(r) > 1}
    if duplicates:
        raise ValueError(f"profiles share a root, so one would overwrite the other: {sorted(duplicates)}")
    return profiles


def rendered_roster() -> list[str]:
    """Skill names to render — every directory under `prompts/skills/`.

    Derived from the filesystem rather than a manifest so that adding a
    rendered skill is one source directory plus whatever profile values it
    needs, with no third place to forget.
    """
    if not SKILLS_SRC.is_dir():
        return []
    return sorted(d.name for d in SKILLS_SRC.iterdir() if d.is_dir())


def render_tree(engine: ModuleType, profiles: list[Profile], destination: Path) -> list[Path]:
    """Render every skill for every profile under `destination`.

    Returns the written paths, relative to `destination`.
    """
    written: list[Path] = []
    for skill in rendered_roster():
        source_dir = SKILLS_SRC / skill
        for source in sorted(p for p in source_dir.rglob("*") if p.is_file()):
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
    return written


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
        target.write_bytes(raw)
        return

    values = profile.values_for(skill)
    found = sorted(set(engine.PLACEHOLDER_RE.findall(text)))

    undefined = [key for key in found if key not in values]
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
    roster = rendered_roster()
    if not roster:
        sys.stderr.write(f"no skill sources found under {SKILLS_SRC}\n")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        written = render_tree(engine, profiles, staging)
        format_markdown(staging, written)

        if args.check:
            return _report_drift(staging, written)

        for relative in written:
            target = REPO_ROOT / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / relative, target)

    print(
        f"rendered {len(roster)} skill(s) into {len(profiles)} harness root(s): "
        f"{', '.join(p.root for p in profiles)}"
    )
    return 0


def _report_drift(staging: Path, written: list[Path]) -> int:
    missing: list[Path] = []
    differing: list[Path] = []
    for relative in written:
        committed = REPO_ROOT / relative
        if not committed.exists():
            missing.append(relative)
        elif not filecmp.cmp(staging / relative, committed, shallow=False):
            differing.append(relative)
        elif (staging / relative).stat().st_mode & 0o111 != committed.stat().st_mode & 0o111:
            differing.append(relative)

    if not missing and not differing:
        print(f"rendered output matches all {len(written)} committed file(s)")
        return 0

    sys.stderr.write("rendered output does not match the committed harness roots.\n")
    for relative in missing:
        sys.stderr.write(f"  missing:   {relative}\n")
    for relative in differing:
        sys.stderr.write(f"  differs:   {relative}\n")
    sys.stderr.write(
        "\nThe harness roots are generated from `prompts/`. Edit the source in "
        "`prompts/skills/` (or the value in `prompts/profiles/`), then run:\n"
        "  python3 scripts/render-prompts.py\n"
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
