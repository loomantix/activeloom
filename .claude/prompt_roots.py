#!/usr/bin/env python3
"""Declared harness prompt roots — the scope source shared by both gate linters.

`.claude/lint-skill-content.py` and `.claude/lint-claude-cli-invocations.py`
both scan trees that live under a harness prompt root. Those roots are declared
in exactly one place, `prompts/profiles/*.yml` (`root:`), and the renderer
writes every rendered skill into every declared root.

Until now each linter hand-listed the roots it knew about. The hand-list and
the profile set were maintained independently, which is the drift: a profile
adding `root: .cursor` renders the whole skill roster into `.cursor/skills/**`,
and neither gate would have read a byte of it. Deriving scope from the profiles
closes that general case, including roots that do not exist yet.

`MINIMUM_ROOTS` closes the inverse failure. Derivation on its own is
fail-open in one direction: delete a profile (or make it unparseable in a way
that swallowed the error) and the scope silently shrinks by one whole harness.
The floor makes that a hard error instead. It is the same shape as
`REQUIRED_FILES` in the CLI-invocation lint, and for the same reason.

Every function returns `(value, errors)` rather than raising or printing.
Callers treat a non-empty `errors` as exit-2: a gate that cannot establish its
own scope must fail closed, never degrade to scanning less.
"""

from __future__ import annotations

import glob
import os

PROFILES_DIR = "prompts/profiles"
PROFILE_GLOB = os.path.join(PROFILES_DIR, "*.yml")

# Scope floor. A declared-root set that does not cover these is an error, not
# a smaller scan. Add a root here when a profile for it lands; removing one is
# a deliberate, reviewer-visible narrowing of both gates.
MINIMUM_ROOTS = (".agents", ".claude", ".codex")


def _validate_root(root: object, source: str) -> tuple[str | None, str | None]:
    """Return (normalized root, error). Exactly one is non-None.

    A profile is an input to the gates' own scope, so a malformed `root:` has
    to be rejected rather than normalized into something plausible. An absolute
    path or one containing `..` would point the scan outside the repository —
    at best it scans nothing, at worst it is a way to aim a gate somewhere it
    was never reviewed for.
    """
    if not isinstance(root, str) or not root.strip():
        return None, f"{source}: `root:` must be a non-empty string, got {root!r}"
    if os.path.isabs(root) or root.startswith("~"):
        return None, f"{source}: `root:` must be repo-relative, got {root!r}"
    normalized = os.path.normpath(root)
    if normalized != root.rstrip("/") or normalized in (".", ".."):
        return None, f"{source}: `root:` must be a normalized path, got {root!r}"
    if ".." in normalized.split(os.sep):
        return None, f"{source}: `root:` must not escape the repo, got {root!r}"
    return normalized, None


def declared_prompt_roots() -> tuple[list[str], list[str]]:
    """Return (sorted declared roots, errors).

    Reads every `prompts/profiles/*.yml`. Errors are returned, not raised, so a
    caller can print all of them at once and exit 2.
    """
    errors: list[str] = []
    try:
        import yaml
    except ImportError:
        return [], [
            f"pyyaml not installed — required to derive gate scope from {PROFILE_GLOB}."
        ]

    paths = sorted(glob.glob(PROFILE_GLOB))
    if not paths:
        return [], [f"no profiles found at {PROFILE_GLOB} — cannot derive gate scope"]

    roots: set[str] = set()
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except OSError as exc:
            errors.append(f"{path}: unreadable: {exc}")
            continue
        except yaml.YAMLError as exc:
            errors.append(f"{path}: {exc}")
            continue
        # `yaml.safe_load("")` is None, and a top-level list or scalar is not a
        # mapping either. Surface the malformed profile instead of crashing on
        # `.get`, and never treat "no mapping" as "no root declared".
        if not isinstance(doc, dict):
            errors.append(
                f"{path}: expected top-level mapping, got {type(doc).__name__}"
            )
            continue
        if "root" not in doc:
            errors.append(f"{path}: no `root:` key — every profile declares its root")
            continue
        normalized, error = _validate_root(doc["root"], path)
        if error is not None:
            errors.append(error)
            continue
        assert normalized is not None
        roots.add(normalized)

    missing = sorted(set(MINIMUM_ROOTS) - roots)
    if missing:
        errors.append(
            f"declared profile roots {sorted(roots)} do not cover the required "
            f"minimum {list(MINIMUM_ROOTS)} (missing {missing}). A profile was "
            f"removed or renamed; update MINIMUM_ROOTS in "
            f".claude/prompt_roots.py in the same PR so the narrowing is "
            f"reviewer-visible."
        )
    return sorted(roots), errors
