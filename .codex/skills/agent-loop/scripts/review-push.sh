#!/usr/bin/env bash
# Wrapper-owned, fail-closed publication of a review hook's committed fixes.

set -euo pipefail

if [ "$#" -eq 1 ] && [ "$1" = --protocol-version ]; then
    printf '1\n'
    exit 0
fi
if [ "$#" -ne 0 ]; then
    echo "review-push accepts no arguments; force, refspec, and destination selection are wrapper-owned" >&2
    exit 2
fi

: "${AGENT_LOOP_WORKTREE:?AGENT_LOOP_WORKTREE is required}"
: "${AGENT_LOOP_BRANCH:?AGENT_LOOP_BRANCH is required}"
: "${AGENT_LOOP_PR_HEAD_SHA:?AGENT_LOOP_PR_HEAD_SHA is required}"

case "$AGENT_LOOP_BRANCH" in
    refs/*|*:*|*' '*|*'~'*|*'^'*|*'?'*|*'['*|*\\*)
        echo "captured issue branch is not a safe branch name" >&2
        exit 1
        ;;
esac
git check-ref-format --branch "$AGENT_LOOP_BRANCH" >/dev/null

actual_root="$(git rev-parse --show-toplevel)"
[ "$actual_root" = "$AGENT_LOOP_WORKTREE" ] || {
    echo "review-push must run from the captured issue worktree" >&2
    exit 1
}
actual_branch="$(git symbolic-ref --quiet --short HEAD)" || {
    echo "review-push rejects detached HEAD" >&2
    exit 1
}
[ "$actual_branch" = "$AGENT_LOOP_BRANCH" ] || {
    echo "review-push rejects a different checked-out branch" >&2
    exit 1
}
[ -z "$(git status --porcelain)" ] || {
    echo "review-push requires a clean committed worktree" >&2
    exit 1
}

local_head="$(git rev-parse HEAD)"
git merge-base --is-ancestor "$AGENT_LOOP_PR_HEAD_SHA" "$local_head" || {
    echo "review-push rejects non-forward review history" >&2
    exit 1
}
remote_line="$(git ls-remote --heads origin "refs/heads/$AGENT_LOOP_BRANCH")"
[ -n "$remote_line" ] || {
    echo "review-push requires the captured remote issue branch" >&2
    exit 1
}
remote_head="${remote_line%%[[:space:]]*}"
[ "$remote_head" = "$AGENT_LOOP_PR_HEAD_SHA" ] || {
    echo "review-push rejects a stale or uncertain remote head" >&2
    exit 1
}

AGENT_LOOP_SAFE_REVIEW_PUSH=1 git push origin \
    "$local_head:refs/heads/$AGENT_LOOP_BRANCH"
observed="$(git ls-remote --heads origin "refs/heads/$AGENT_LOOP_BRANCH")"
[ "${observed%%[[:space:]]*}" = "$local_head" ] || {
    echo "review-push could not attest the remote head" >&2
    exit 1
}
printf '%s\n' "$local_head"
