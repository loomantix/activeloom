#!/usr/bin/env bash
# Install this checkout's skills into each agent harness's user-level skills
# directory by symlinking. Updates then flow via `git pull` in this checkout
# rather than per-repo sync PRs; re-run only to pick up added, renamed, or
# retired skills.
#
# Usage:
#   ./scripts/install-skills.sh                   # every harness found here
#   ./scripts/install-skills.sh --harness codex   # only the named harness(es)
#   ./scripts/install-skills.sh --force           # replace entries (backed up)
#   ./scripts/install-skills.sh --dry-run         # report only, write nothing
#   ./scripts/install-skills.sh --check           # is the install complete?
#   ./scripts/install-skills.sh --dest gemini=DIR # install a harness elsewhere
#
# Harnesses come from the `harnesses:` block in scripts/sync-targets.yml:
# each one's `root` tree (`<root>/skills`) is linked into `$HOME/<skills>`.
# By default every harness whose `home` directory exists or whose `cli` is on
# PATH is selected; with no evidence at all, Claude Code is.
#
# Options:
#   --harness ID[,ID]  select harnesses; repeatable
#   --dest ID=DIR      skills directory for one harness; repeatable
#   --force            replace entries that are not this checkout's links
#   --dry-run          print what would happen and write nothing
#   --check            write nothing; list every skill not installed (and the
#                      skills that require it), stale copy, and missing support
#                      file, with the command that fixes it; exit 1 if any
#
# Each harness also gets the support files skills read beside their skills
# directory (`references/`, `agents/`, `REVIEW_WORKFLOW.md`, ...), linked one
# file at a time; an existing file there is never replaced. Required skill
# dependencies come from scripts/skill-requires.txt. Every run ends with the
# same completeness report `--check` prints.
#
# A destination can also be set with `<ID>_SKILLS_DIR` (CLAUDE_SKILLS_DIR,
# CODEX_SKILLS_DIR, GEMINI_SKILLS_DIR); `--dest` wins over the variable.
# Set UPSTREAM_ROOT_OVERRIDE to install from a different checkout.
#
# Safety: only missing skills are installed unless --force; --force backs up a
# real file or directory before replacing it; only dangling symlinks pointing
# into this checkout are pruned; real directories are never removed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPSTREAM_ROOT="${UPSTREAM_ROOT_OVERRIDE:-$(dirname "$SCRIPT_DIR")}"
if [ ! -d "$UPSTREAM_ROOT" ]; then
  echo "❌ upstream checkout not found at $UPSTREAM_ROOT" >&2
  exit 2
fi
# Absolute, so link targets and the prune prefix compare exactly.
UPSTREAM_ROOT="$(cd "$UPSTREAM_ROOT" && pwd)"
MANIFEST="$UPSTREAM_ROOT/scripts/sync-targets.yml"
REQUIRES="$UPSTREAM_ROOT/scripts/skill-requires.txt"

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

FORCE=0
DRY_RUN=0
CHECK=0
REQUESTED=""
DESTS=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --check)
      CHECK=1
      DRY_RUN=1
      ;;
    --harness | --dest)
      if [ "$#" -lt 2 ]; then
        echo "$1 needs a value" >&2
        exit 2
      fi
      if [ "$1" = "--harness" ]; then
        REQUESTED="$REQUESTED $(printf '%s' "$2" | tr ',' ' ')"
      else
        DESTS="$DESTS$2"$'\n'
      fi
      shift
      ;;
    --harness=*) REQUESTED="$REQUESTED $(printf '%s' "${1#--harness=}" | tr ',' ' ')" ;;
    --dest=*) DESTS="$DESTS${1#--dest=}"$'\n' ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [ ! -f "$MANIFEST" ]; then
  echo "❌ no harness manifest at $MANIFEST — is this checkout complete?" >&2
  exit 2
fi

# One tab-separated line per harness: id root home skills legacy cli.
# An absent optional field is written as `-` so `read` keeps column order.
HARNESS_TABLE="$(awk '
  function flush() {
    if (id != "") printf "%s\t%s\t%s\t%s\t%s\t%s\n", id, f["root"], f["home"], f["skills"], f["legacy_skills"], f["cli"]
  }
  /^harnesses:[[:space:]]*$/ { inblock = 1; next }
  !inblock { next }
  /^[^[:space:]#]/ { exit }
  /^  [A-Za-z0-9_-]+:[[:space:]]*$/ {
    flush()
    id = $1; sub(/:$/, "", id)
    f["root"] = "-"; f["home"] = "-"; f["skills"] = "-"; f["legacy_skills"] = "-"; f["cli"] = "-"
    next
  }
  /^    (root|home|skills|legacy_skills|cli):[[:space:]]*[^[:space:]]+[[:space:]]*$/ {
    key = $1; sub(/:$/, "", key); f[key] = $2
  }
  END { flush() }
' "$MANIFEST")"

if [ -z "$HARNESS_TABLE" ]; then
  echo "❌ no harnesses parsed from $MANIFEST" >&2
  exit 2
fi

KNOWN=""
while IFS=$'\t' read -r id root home skills _legacy cli; do
  for value in "$root" "$home" "$skills" "$cli"; do
    if [ "$value" = "-" ]; then
      echo "❌ harness \`$id\` in $MANIFEST needs root, home, skills, and cli" >&2
      exit 2
    fi
  done
  KNOWN="$KNOWN $id"
done <<<"$HARNESS_TABLE"

is_known() {
  case " $KNOWN " in *" $1 "*) return 0 ;; esac
  return 1
}

for id in $REQUESTED; do
  if ! is_known "$id"; then
    echo "unknown harness: $id (known:$KNOWN)" >&2
    exit 2
  fi
done

while IFS= read -r spec; do
  [ -n "$spec" ] || continue
  id="${spec%%=*}"
  dir="${spec#*=}"
  if [ "$id" = "$spec" ] || [ -z "$id" ] || [ -z "$dir" ]; then
    echo "--dest takes ID=DIR, got: $spec" >&2
    exit 2
  fi
  if ! is_known "$id"; then
    echo "unknown harness in --dest: $id (known:$KNOWN)" >&2
    exit 2
  fi
done <<<"$DESTS"

# Resolve the skills directory for one harness: --dest, then <ID>_SKILLS_DIR,
# then the manifest default under $HOME. The last --dest for an id wins.
dest_for() {
  local id="$1" default="$2" spec found="" var
  while IFS= read -r spec; do
    [ "${spec%%=*}" = "$id" ] && found="${spec#*=}"
  done <<<"$DESTS"
  if [ -n "$found" ]; then
    printf '%s\n' "$found"
    return
  fi
  var="$(printf '%s' "$id" | tr 'abcdefghijklmnopqrstuvwxyz-' 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_')_SKILLS_DIR"
  printf '%s\n' "${!var:-$HOME/$default}"
}

# Choose harnesses: explicit, else detected, else Claude Code.
SELECTED=""
REASON=""
if [ -n "${REQUESTED// /}" ]; then
  REASON="requested with --harness"
  while IFS=$'\t' read -r id _rest; do
    case " $REQUESTED " in *" $id "*) SELECTED="$SELECTED $id" ;; esac
  done <<<"$HARNESS_TABLE"
else
  REASON="detected on this machine"
  while IFS=$'\t' read -r id _root home _skills _legacy cli; do
    if [ -d "$HOME/$home" ] || command -v "$cli" >/dev/null 2>&1; then
      SELECTED="$SELECTED $id"
    fi
  done <<<"$HARNESS_TABLE"
  if [ -z "$SELECTED" ]; then
    REASON="no harness detected — defaulting to Claude Code"
    is_known claude && SELECTED=" claude"
  fi
fi
if [ -z "$SELECTED" ]; then
  echo "❌ no harness selected" >&2
  exit 2
fi

# Resolve existing ancestors physically without creating a destination.
canonical_dest() {
  local dir="$1" parent name
  if [ -d "$dir" ]; then
    (cd -- "$dir" && pwd -P)
    return
  fi
  name="$(basename -- "$dir")"
  parent="$(canonical_dest "$(dirname -- "$dir")")"
  case "$name" in
    .) printf '%s\n' "$parent" ;;
    ..) dirname -- "$parent" ;;
    *) printf '%s/%s\n' "${parent%/}" "$name" ;;
  esac
}

# Two harnesses sharing one directory would overwrite each other's skills.
seen_dests=""
while IFS=$'\t' read -r id _root _home skills _legacy _cli; do
  case " $SELECTED " in *" $id "*) ;; *) continue ;; esac
  dest="$(canonical_dest "$(dest_for "$id" "$skills")")"
  case "$seen_dests" in
    *$'\n'"$dest"$'\n'*)
      echo "❌ two selected harnesses install into $dest — give one a --dest" >&2
      exit 2
      ;;
  esac
  seen_dests="$seen_dests"$'\n'"$dest"$'\n'
done <<<"$HARNESS_TABLE"

echo "Harnesses:$SELECTED ($REASON)"

installed=0
skipped=0
replaced=0
pruned=0
missing_src=0

# Remove symlinks in $1 that point into this checkout and no longer resolve.
# A real directory is a locally-authored skill and is never touched; a symlink
# pointing anywhere else belongs to another checkout and is left alone.
prune_dangling() {
  local dir="$1" target current
  [ -d "$dir" ] || return 0
  for target in "$dir"/*; do
    [ -L "$target" ] || continue
    # -e follows the link: false means the destination is gone.
    [ -e "$target" ] && continue
    current="$(readlink "$target")"
    case "$current" in
      "$UPSTREAM_ROOT"/*) ;;
      *) continue ;;
    esac
    echo "  🧹 $(basename "$target") (retired upstream — removing dangling link)"
    if [ "$DRY_RUN" -eq 0 ]; then
      rm "$target"
    fi
    pruned=$((pruned + 1))
  done
}

# Report live links into this checkout left in a harness's older skills
# directory: the harness reads both, so each such skill would appear twice.
report_legacy() {
  local dir="$1" target current
  [ -d "$dir" ] || return 0
  for target in "$dir"/*; do
    if [ ! -L "$target" ] || [ ! -e "$target" ]; then
      continue
    fi
    current="$(readlink "$target")"
    case "$current" in
      "$UPSTREAM_ROOT"/*)
        echo "  ⚠️  $(basename "$target") is also linked from $dir — remove it to avoid a duplicate: rm '$target'"
        ;;
    esac
  done
}

install_harness() {
  local id="$1" src_dir="$2" dest="$3" src name target src_resolved current backup

  if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$dest"
  fi

  for src in "$src_dir"/*/; do
    # Guard against an unexpanded glob when the source tree is empty. Absolute
    # paths come from `cd && pwd` rather than `readlink -f`, which is GNU-only.
    [ -d "$src" ] || continue
    name="$(basename "$src")"
    target="$dest/$name"
    src_resolved="$(cd "$src" && pwd)"

    if [ -L "$target" ]; then
      # `readlink` returns the literal link content; this script always writes
      # absolute targets, so a literal comparison is exact for its own links.
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
      echo "  🔗 $name (replacing existing symlink)"
      if [ "$DRY_RUN" -eq 0 ]; then
        rm "$target"
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

    echo "  ➕ $name (would install)"
    if [ "$DRY_RUN" -eq 0 ]; then
      ln -s "$src_resolved" "$target"
    fi
    installed=$((installed + 1))
  done

  # A skill renamed or retired upstream leaves a dangling link that `git pull`
  # cannot clean up, and it occupies the name for any later skill.
  prune_dangling "$dest"
}

# Files skills read beside their skills directory, as `../../<path>`. The same
# list as SUPPORT_PATHS in cli/lib/add.js.
SUPPORT_PATHS="MODEL_NOTES.md REVIEW_WORKFLOW.md SKILL_AUTHORING.md agents prompt-stack.json references"

# Link each support file of $1 (a harness root in this checkout) into $2 (the
# directory holding the skills directory). Existing files are never replaced.
install_support() {
  local src_root="$1" dest_root="$2" rel src file to
  for rel in $SUPPORT_PATHS; do
    src="$src_root/$rel"
    [ -e "$src" ] || continue
    if [ -d "$src" ]; then
      while IFS= read -r file; do
        to="$dest_root/$rel/${file#"$src"/}"
        [ -e "$to" ] || [ -L "$to" ] && continue
        echo "  ➕ support file ${to#"$dest_root"/}"
        if [ "$DRY_RUN" -eq 0 ]; then
          mkdir -p "$(dirname "$to")"
          ln -s "$file" "$to"
        fi
      done < <(find "$src" -type f ! -path '*/__pycache__/*' | sort)
    else
      to="$dest_root/$rel"
      [ -e "$to" ] || [ -L "$to" ] && continue
      echo "  ➕ support file $rel"
      if [ "$DRY_RUN" -eq 0 ]; then
        mkdir -p "$dest_root"
        ln -s "$src" "$to"
      fi
    fi
  done
}

# Required dependencies of skill $1, from scripts/skill-requires.txt.
required_of() {
  [ -f "$REQUIRES" ] || return 0
  awk -v skill="$1" '
    { sub(/#.*/, "") }
    index($0, skill ":") == 1 { sub(/^[^:]*:/, ""); sub(/\|.*/, ""); print; exit }
  ' "$REQUIRES"
}

# Installed skills in $2 that require $1, space-separated.
required_by() {
  local dep="$1" dest="$2" entry name out=""
  for entry in "$dest"/*; do
    [ -e "$entry/SKILL.md" ] || continue
    name="$(basename "$entry")"
    case " $(required_of "$name") " in *" $dep "*) out="$out $name" ;; esac
  done
  printf '%s\n' "${out# }"
}

problems=0

check_support_file() {
  local to="$1" label="$2" fix="$3"
  [ -e "$to" ] && return 0
  if [ -L "$to" ]; then
    # Installation preserves support entries, including broken foreign links.
    printf -v fix 'unlink %q && %s' "$to" "$fix"
  fi
  echo "  ❌ support file $label is missing — fix: $fix"
  problems=$((problems + 1))
}

# Report what the harness still lacks: upstream skills not installed (naming
# the installed skills that require them), stale copies shadowing an upstream
# skill, and missing support files. Each line ends in the command that fixes it.
check_harness() {
  local id="$1" src_root="$2" src_dir="$3" dest="$4" fix="$5" src name target needers rel to file
  for src in "$src_dir"/*/; do
    [ -d "$src" ] || continue
    name="$(basename "$src")"
    target="$dest/$name"
    if { [ -e "$target" ] || [ -L "$target" ]; } && [ ! -f "$target/SKILL.md" ]; then
      echo "  ❌ $name has no usable SKILL.md — fix: $fix --force (backs up regular entries)"
      problems=$((problems + 1))
    elif [ ! -e "$target" ]; then
      needers="$(required_by "$name" "$dest")"
      if [ -n "$needers" ]; then
        echo "  ❌ $name is not installed (required by: $needers) — fix: $fix"
      else
        echo "  ❌ $name is not installed — fix: $fix"
      fi
      problems=$((problems + 1))
    elif [ ! -L "$target" ] && [ -d "$target" ] &&
      ! diff -rq -x __pycache__ -x '*.pyc' "$target" "$src" >/dev/null 2>&1; then
      echo "  ❌ $name is a copy that differs from this checkout (stale or local edits) — fix: $fix --force (backs it up)"
      problems=$((problems + 1))
    fi
  done
  for rel in $SUPPORT_PATHS; do
    [ -e "$src_root/$rel" ] || continue
    if [ -d "$src_root/$rel" ]; then
      while IFS= read -r file; do
        to="$(dirname "$dest")/$rel/${file#"$src_root/$rel"/}"
        check_support_file "$to" "$rel/${file#"$src_root/$rel"/}" "$fix"
      done < <(find "$src_root/$rel" -type f ! -path '*/__pycache__/*' | sort)
    else
      check_support_file "$(dirname "$dest")/$rel" "$rel" "$fix"
    fi
  done
}

while IFS=$'\t' read -r id root _home skills legacy _cli; do
  case " $SELECTED " in *" $id "*) ;; *) continue ;; esac
  src_dir="$UPSTREAM_ROOT/$root/skills"
  dest="$(canonical_dest "$(dest_for "$id" "$skills")")"
  echo ""
  echo "$id: $root/skills → $dest"
  if [ ! -d "$src_dir" ]; then
    echo "  ❌ no skills at $src_dir — is this checkout complete?" >&2
    missing_src=$((missing_src + 1))
    continue
  fi
  printf -v fix '%q --harness %q' "$UPSTREAM_ROOT/scripts/install-skills.sh" "$id"
  if [ "$dest" != "$(canonical_dest "$HOME/$skills")" ]; then
    printf -v fix '%s --dest %q' "$fix" "$id=$dest"
  fi
  if [ "$CHECK" -eq 0 ]; then
    install_harness "$id" "$src_dir" "$dest"
    install_support "$UPSTREAM_ROOT/$root" "$(dirname "$dest")"
    if [ "$legacy" != "-" ] && [ "$HOME/$legacy" != "$dest" ]; then
      prune_dangling "$HOME/$legacy"
      report_legacy "$HOME/$legacy"
    fi
  fi
  # A dry run has written nothing, so its check would only repeat the plan.
  if [ "$CHECK" -eq 1 ] || [ "$DRY_RUN" -eq 0 ]; then
    check_harness "$id" "$UPSTREAM_ROOT/$root" "$src_dir" "$dest" "$fix"
  fi
done <<<"$HARNESS_TABLE"

echo ""
if [ "$CHECK" -eq 1 ]; then
  if [ "$problems" -gt 0 ]; then
    echo "Check: $problems problem(s). A skill that calls a missing one fails partway through; run the fix shown."
  else
    echo "Check: complete — every skill, required dependency and support file is installed."
  fi
elif [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run: $installed would install, $replaced would replace, $pruned would prune, $skipped left alone."
  echo "(no changes written)"
else
  echo "Done: $installed installed, $replaced replaced, $pruned pruned, $skipped left alone."
  echo "Run \`git pull\` in $UPSTREAM_ROOT to update — no re-install needed."
  echo "Re-run this script after a pull that adds, renames, or retires a skill; \`--check\` tells you whether one did."
  if [ "$problems" -gt 0 ]; then
    echo "⚠️  $problems problem(s) left above; each names its fix."
  fi
  echo 'Next: run review-setup for personal review settings and the repository auto-glance choice (one local non-author reviewer for small changes).'
  echo 'glance.auto is opt-in in each repository, not a personal setting; setup asks before writing it.'
fi

if [ "$missing_src" -gt 0 ]; then
  exit 2
fi
if [ "$CHECK" -eq 1 ] && [ "$problems" -gt 0 ]; then
  exit 1
fi
