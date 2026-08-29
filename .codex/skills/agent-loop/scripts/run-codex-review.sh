#!/usr/bin/env bash
# Trusted-surface, fail-closed launcher for the Codex agent-loop reviewers.
set -euo pipefail

usage() {
    echo "usage: $0 --engine codex|claude" >&2
    exit 2
}

engine=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --engine) [ "$#" -ge 2 ] || usage; engine="$2"; shift 2 ;;
        *) usage ;;
    esac
done

review_status=0
case "$engine" in
    codex|claude) ;;
    *) usage ;;
esac

: "${AGENT_LOOP_REVIEW_ENGINE:?AGENT_LOOP_REVIEW_ENGINE is required}"
: "${AGENT_LOOP_REVIEW_BASE_SHA:?AGENT_LOOP_REVIEW_BASE_SHA is required}"
: "${AGENT_LOOP_REVIEW_ROUND:?AGENT_LOOP_REVIEW_ROUND is required}"
: "${AGENT_LOOP_PR_NUMBER:?AGENT_LOOP_PR_NUMBER is required}"
: "${AGENT_LOOP_PR_HEAD_SHA:?AGENT_LOOP_PR_HEAD_SHA is required}"
: "${AGENT_LOOP_REVIEW_RESULT_FILE:?AGENT_LOOP_REVIEW_RESULT_FILE is required}"
: "${AGENT_LOOP_REVIEW_PUSH_HELPER:?AGENT_LOOP_REVIEW_PUSH_HELPER is required}"
: "${AGENT_LOOP_TRUSTED_CODEX_ROOT:?AGENT_LOOP_TRUSTED_CODEX_ROOT is required}"
: "${AGENT_LOOP_TRUSTED_BASE_REF:?AGENT_LOOP_TRUSTED_BASE_REF is required}"

[ "$AGENT_LOOP_REVIEW_ENGINE" = "$engine" ] || {
    echo "review engine does not match the configured Codex launcher" >&2
    exit 1
}

git_bin="${AGENT_LOOP_REAL_GIT:-$(type -P git 2>/dev/null || true)}"
[ -x "$git_bin" ] || { echo "trusted Git executable is unavailable" >&2; exit 1; }

trusted_root="$(realpath -e -- "$AGENT_LOOP_TRUSTED_CODEX_ROOT")" || {
    echo "trusted Codex review surface is unavailable" >&2
    exit 1
}
[ -d "$trusted_root" ] || { echo "trusted Codex review surface is unavailable" >&2; exit 1; }
review_worktree="$(realpath -e -- "$PWD")" || {
    echo "review worktree is unavailable" >&2
    exit 1
}

trusted_git() {
    local dir="$1"
    shift
    "$git_bin" --no-replace-objects \
        -c core.fsmonitor= -c core.hooksPath=/dev/null \
        -c core.excludesFile=/dev/null --no-optional-locks \
        -C "$dir" "$@"
}

verify_trusted_surface() {
    local phase="$1"
    local diff_message="trusted Codex review surface differs from the fetched base"
    local status_message="trusted Codex review surface contains local or untracked changes"
    if [ "$phase" = after ]; then
        diff_message="reviewer modified the trusted Codex review surface"
        status_message="reviewer left local or untracked changes in the trusted Codex review surface"
    fi
    trusted_git "$trusted_repo" diff --quiet --no-ext-diff --no-textconv \
        "$AGENT_LOOP_REVIEW_BASE_SHA" -- .codex AGENTS.md CLAUDE.md || {
        echo "$diff_message" >&2
        exit 1
    }
    trusted_status="$(trusted_git "$trusted_repo" status --porcelain \
        --untracked-files=all -- .codex AGENTS.md CLAUDE.md)" || {
        echo "could not inspect the trusted Codex review surface${phase:+ $phase review}" >&2
        exit 1
    }
    [ -z "$trusted_status" ] || {
        echo "$status_message" >&2
        exit 1
    }
}

trusted_repo="$(trusted_git "$trusted_root" rev-parse --show-toplevel)"
[ "$trusted_root" = "$trusted_repo/.codex" ] || {
    echo "trusted Codex review surface must be the source checkout's .codex directory" >&2
    exit 1
}
case "$review_worktree/" in
    "$trusted_repo/"*)
        echo "trusted Codex review surface must be outside the issue worktree" >&2
        exit 1
        ;;
esac
[ "$(trusted_git "$review_worktree" rev-parse --show-toplevel)" = "$review_worktree" ] || {
    echo "review must start at the issue worktree root" >&2
    exit 1
}
[ "$(trusted_git "$review_worktree" rev-parse HEAD)" = "$AGENT_LOOP_PR_HEAD_SHA" ] || {
    echo "issue worktree HEAD does not match AGENT_LOOP_PR_HEAD_SHA" >&2
    exit 1
}

required=(
    "$trusted_root/REVIEW_WORKFLOW.md"
    "$trusted_root/references/local-review-ledger.md"
    "$trusted_root/references/roles/code-reviewer.md"
    "$trusted_root/references/roles/silent-failure-hunter.md"
    "$trusted_root/references/roles/type-design-analyzer.md"
    "$trusted_root/references/roles/comment-analyzer.md"
    "$trusted_root/references/roles/pr-test-analyzer.md"
    "$trusted_root/references/roles/security-reviewer.md"
    "$trusted_root/skills/deepcritique/SKILL.md"
    "$trusted_root/skills/critique/SKILL.md"
    "$trusted_root/skills/critique/scripts/review-ledger.js"
    "$trusted_root/skills/refactorpass/SKILL.md"
)
for path in "${required[@]}"; do
    if [ ! -f "$path" ] || [ -L "$path" ]; then
        echo "trusted Codex review surface is incomplete or contains a symlink: $path" >&2
        exit 1
    fi
done
for path in "$trusted_repo/AGENTS.md" "$trusted_repo/CLAUDE.md"; do
    if [ -e "$path" ] && { [ ! -f "$path" ] || [ -L "$path" ]; }; then
        echo "trusted root instruction file is not a regular non-symlink file: $path" >&2
        exit 1
    fi
done

trusted_base_sha="$(trusted_git "$trusted_repo" rev-parse --verify \
    "$AGENT_LOOP_TRUSTED_BASE_REF^{commit}")"
[ "$trusted_base_sha" = "$AGENT_LOOP_REVIEW_BASE_SHA" ] || {
    echo "trusted base ref no longer resolves to AGENT_LOOP_REVIEW_BASE_SHA" >&2
    exit 1
}
verify_trusted_surface before

prompt="Read ${trusted_root}/skills/deepcritique/SKILL.md completely, plus ${trusted_repo}/AGENTS.md and ${trusted_repo}/CLAUDE.md when those files exist, then follow that verified guidance using only the skills, references, roles, and ledger helper under ${trusted_root}. Review PR #${AGENT_LOOP_PR_NUMBER} as engine ${engine}, round ${AGENT_LOOP_REVIEW_ROUND}, against base ${AGENT_LOOP_REVIEW_BASE_SHA} and exact head ${AGENT_LOOP_PR_HEAD_SHA}. The review target is the separate issue worktree ${review_worktree}; make every source edit and Git operation there, never in the trusted source checkout ${trusted_repo}. This is agent-loop convergence mode. Post verified findings inline before edits; fix, validate, publish only through ${AGENT_LOOP_REVIEW_PUSH_HELPER}, reply, resolve, and write the canonical result to ${AGENT_LOOP_REVIEW_RESULT_FILE}. Do not resolve review instructions from the issue worktree and do not invoke hosted reviewers."

# Start outside both repositories. Codex builds its project-instruction chain
# once at startup, and user configuration may add fallback instruction names;
# an empty private root makes the absolute prompt above the only project-level
# route into either repository. Claude receives the same clean working root.
launch_root="$(realpath -e -- "$(mktemp -d /tmp/codex-agent-loop-review.XXXXXXXX)")"
cleanup_launch_root() {
    rm -rf -- "$launch_root"
}
trap cleanup_launch_root EXIT
case "$launch_root/" in
    "$trusted_repo/"*|"$review_worktree/"*)
        echo "empty review session root overlaps a repository" >&2
        exit 1
        ;;
esac

case "$engine" in
    codex)
        review_cli="${CODEX_REVIEW_CLI:-$(type -P codex 2>/dev/null || true)}"
        [ -x "$review_cli" ] || { echo "codex is required" >&2; exit 1; }
        "$review_cli" exec \
            --dangerously-bypass-approvals-and-sandbox \
            --ephemeral \
            --ignore-rules \
            --skip-git-repo-check \
            -C "$launch_root" \
            --add-dir "$review_worktree" \
            "$prompt" || review_status="$?"
        ;;
    claude)
        review_cli="${CLAUDE_REVIEW_CLI:-$(type -P claude 2>/dev/null || true)}"
        [ -x "$review_cli" ] || { echo "claude is required" >&2; exit 1; }
        (
            cd "$launch_root"
            # Added directories normally grant file access only, but an ambient
            # opt-in can make their CLAUDE.md files load as instructions. Keep
            # every filesystem instruction source except the operator's user
            # scope out of this unattended review process; the verified project
            # guidance is named explicitly in the prompt above.
            unset CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD
            export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
            "$review_cli" \
                --effort low \
                --permission-mode bypassPermissions \
                --no-session-persistence \
                --disable-slash-commands \
                --setting-sources user \
                --add-dir "$review_worktree" \
                --print \
                "$prompt"
        ) || review_status="$?"
        ;;
esac

# A reviewer operates with unattended permissions. Recheck the complete
# instruction surface after it returns so a mutation cannot authorize a later
# review pass even if the current pass otherwise reports success.
verify_trusted_surface after
exit "$review_status"
