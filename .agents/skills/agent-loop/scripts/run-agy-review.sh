#!/usr/bin/env bash
# Trusted-surface, fail-closed launcher for the Agy review engines.
set -euo pipefail

usage() {
    echo "usage: $0 --engine gemini|claude" >&2
    exit 2
}

engine=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --engine) [ "$#" -ge 2 ] || usage; engine="$2"; shift 2 ;;
        *) usage ;;
    esac
done

case "$engine" in
    gemini) model="gemini-3.7-flash-high"; effort="high" ;;
    claude) model="claude-sonnet-4-6"; effort="low" ;;
    *) usage ;;
esac

review_timeout_seconds="${LOCAL_REVIEW_PASS_TIMEOUT_SECONDS:-1800}"
[[ "$review_timeout_seconds" =~ ^[1-9][0-9]*$ ]] && \
    [ "$review_timeout_seconds" -le 3600 ] || {
    echo "LOCAL_REVIEW_PASS_TIMEOUT_SECONDS must be an integer from 1 through 3600" >&2
    exit 2
}

: "${AGENT_LOOP_REVIEW_ENGINE:?AGENT_LOOP_REVIEW_ENGINE is required}"
: "${AGENT_LOOP_REVIEW_BASE_SHA:?AGENT_LOOP_REVIEW_BASE_SHA is required}"
: "${AGENT_LOOP_REVIEW_ROUND:?AGENT_LOOP_REVIEW_ROUND is required}"
: "${AGENT_LOOP_PR_NUMBER:?AGENT_LOOP_PR_NUMBER is required}"
: "${AGENT_LOOP_PR_HEAD_SHA:?AGENT_LOOP_PR_HEAD_SHA is required}"
: "${AGENT_LOOP_REVIEW_RESULT_FILE:?AGENT_LOOP_REVIEW_RESULT_FILE is required}"
: "${AGENT_LOOP_REVIEW_PUSH_HELPER:?AGENT_LOOP_REVIEW_PUSH_HELPER is required}"
: "${AGENT_LOOP_TRUSTED_AGENTS_ROOT:?AGENT_LOOP_TRUSTED_AGENTS_ROOT is required}"
: "${AGENT_LOOP_TRUSTED_BASE_REF:?AGENT_LOOP_TRUSTED_BASE_REF is required}"

[ "$AGENT_LOOP_REVIEW_ENGINE" = "$engine" ] || {
    echo "review engine does not match the configured Agy launcher" >&2
    exit 1
}

trusted_root="$(realpath -e -- "$AGENT_LOOP_TRUSTED_AGENTS_ROOT")"
[ -d "$trusted_root" ] || { echo "trusted Agy review surface is unavailable" >&2; exit 1; }
case "$PWD/" in
    "$trusted_root/"*) echo "trusted Agy review surface must be outside the issue worktree" >&2; exit 1 ;;
esac

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
        echo "trusted Agy review surface is incomplete or contains a symlink: $path" >&2
        exit 1
    fi
done

trusted_repo="$(git -C "$trusted_root" rev-parse --show-toplevel)"
[ "$trusted_root" = "$trusted_repo/.agents" ] || {
    echo "trusted Agy review surface must be the source checkout's .agents directory" >&2
    exit 1
}
git -C "$trusted_repo" diff --quiet "$AGENT_LOOP_TRUSTED_BASE_REF" -- \
    .agents/REVIEW_WORKFLOW.md .agents/references \
    .agents/skills/deepcritique .agents/skills/critique .agents/skills/refactorpass || {
    echo "trusted Agy review surface differs from the fetched base" >&2
    exit 1
}
[ -z "$(git -C "$trusted_repo" status --porcelain --untracked-files=all -- \
    .agents/REVIEW_WORKFLOW.md .agents/references \
    .agents/skills/deepcritique .agents/skills/critique .agents/skills/refactorpass)" ] || {
    echo "trusted Agy review surface contains local or untracked changes" >&2
    exit 1
}

prompt="Read ${trusted_root}/skills/deepcritique/SKILL.md completely, then follow it using only the skills, references, roles, and ledger helper under ${trusted_root}. Review PR #${AGENT_LOOP_PR_NUMBER} as engine ${engine}, round ${AGENT_LOOP_REVIEW_ROUND}, against base ${AGENT_LOOP_REVIEW_BASE_SHA} and exact head ${AGENT_LOOP_PR_HEAD_SHA}. This is agent-loop convergence mode. Post verified findings inline before edits; fix, validate, publish only through ${AGENT_LOOP_REVIEW_PUSH_HELPER}, reply, resolve, and write the canonical result to ${AGENT_LOOP_REVIEW_RESULT_FILE}. Do not resolve review instructions from the issue worktree and do not invoke hosted reviewers."

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=run-agy-launch.sh
source "$SCRIPT_DIR/run-agy-launch.sh"

# claude-cli-invocations:start
run_agy_and_parse "agy review" \
    --model "$model" \
    --effort "$effort" \
    --mode accept-edits \
    --dangerously-skip-permissions \
    --disable-slash-commands \
    --add-dir "$trusted_root" \
    --output-format json \
    --print-timeout "${review_timeout_seconds}s" \
    --print "$prompt"
# claude-cli-invocations:end
