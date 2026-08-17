#!/usr/bin/env bash
# Install the upstream Antigravity / Gemini skills into the user's global skills
# directory (~/.gemini/config/skills) by symlinking. Updates then flow via `git pull`
# in this checkout rather than per-repo sync PRs. Re-run after a pull that adds,
# renames, or retires a skill so new links are installed and stale owned links are pruned.
#
# Usage:
#   ./scripts/install-skills.sh           # install missing skills and prune retired owned links
#   ./scripts/install-skills.sh --force   # replace existing entries (backed up)
#   ./scripts/install-skills.sh --dry-run # report what would happen, write nothing
#
# Existing symlinks pick up in-place upstream edits automatically.
#
# Source root override: by default the script uses the parent of its own
# directory (so `clone-root/scripts/install-skills.sh` finds skills at
# `clone-root/.agents/skills`). Set `UPSTREAM_ROOT_OVERRIDE` to point at a
# different checkout — useful when this script has been copied or vendored
# into a consumer that wants to install skills from a sibling clone.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPSTREAM_ROOT="${UPSTREAM_ROOT_OVERRIDE:-$(dirname "$SCRIPT_DIR")}"
SKILLS_SRC="$UPSTREAM_ROOT/.agents/skills"
SKILLS_DEST="${GEMINI_SKILLS_DIR:-$HOME/.gemini/config/skills}"

FORCE=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [ ! -d "$SKILLS_SRC" ]; then
  echo "❌ no skills found at $SKILLS_SRC — is this checkout complete?" >&2
  exit 2
fi
UPSTREAM_ROOT="$(cd "$UPSTREAM_ROOT" && pwd)"
SKILLS_SRC="$UPSTREAM_ROOT/.agents/skills"

if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$SKILLS_DEST"
fi

installed=0
skipped=0
replaced=0

for src in "$SKILLS_SRC"/*/; do
  [ -d "$src" ] || continue
  name="$(basename "$src")"
  target="$SKILLS_DEST/$name"
  src_resolved="$(cd "$src" && pwd)"

  if [ -L "$target" ]; then
    current="$(readlink "$target")"
    if [ "$current" = "$src_resolved" ]; then
      echo "  ✓ $name (already linked)"
      skipped=$((skipped + 1))
      continue
    fi
    if [ "$FORCE" -eq 0 ]; then
      echo "  ⚠️  $name links elsewhere ($current) — re-run with --force to replace"
      skipped=$((skipped + 1))
      continue
    fi
    if [ "$DRY_RUN" -eq 0 ]; then
      rm "$target"
    fi
    echo "  🔗 $name (replacing existing symlink)"
    if [ "$DRY_RUN" -eq 0 ]; then
      ln -s "$src_resolved" "$target"
    fi
    replaced=$((replaced + 1))
    continue
  fi

  if [ -e "$target" ]; then
    if [ "$FORCE" -eq 0 ]; then
      echo "  ⚠️  $name exists as a regular file/dir — re-run with --force to replace (will be backed up)"
      skipped=$((skipped + 1))
      continue
    fi
    backup="$target.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    echo "  📦 $name → backing up existing to $(basename "$backup")"
    if [ "$DRY_RUN" -eq 0 ]; then
      mv "$target" "$backup"
      ln -s "$src_resolved" "$target"
    fi
    replaced=$((replaced + 1))
    continue
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  ➕ $name (would install)"
  else
    echo "  ➕ $name (installing)"
    ln -s "$src_resolved" "$target"
  fi
  installed=$((installed + 1))
done

# Prune links this script owns whose source has gone away.
pruned=0
for target in "$SKILLS_DEST"/*; do
  [ -L "$target" ] || continue
  [ -e "$target" ] && continue
  current="$(readlink "$target")"
  name="$(basename "$target")"
  [ "$current" = "$SKILLS_SRC/$name" ] || continue
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  🧹 $name (retired upstream — would prune dangling link)"
  else
    echo "  🧹 $name (retired upstream — removing dangling link)"
    rm "$target"
  fi
  pruned=$((pruned + 1))
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "Dry run: $installed would install, $replaced would replace, $pruned would prune, $skipped left alone."
  echo "(no changes written)"
else
  echo ""
  echo "Done: $installed installed, $replaced replaced, $pruned pruned, $skipped left alone."
  echo "Skills now resolve from $SKILLS_DEST → $SKILLS_SRC."
  echo "Run \`git pull\` in $UPSTREAM_ROOT to pick up in-place skill edits."
  echo "Re-run this script after a pull that adds, renames, or retires a skill."
fi
