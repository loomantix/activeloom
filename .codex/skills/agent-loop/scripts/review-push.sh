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
: "${AGENT_LOOP_REAL_GIT:?AGENT_LOOP_REAL_GIT is required}"
: "${AGENT_LOOP_ORIGIN_FETCH_URLS:?AGENT_LOOP_ORIGIN_FETCH_URLS is required}"
: "${AGENT_LOOP_ORIGIN_PUSH_URLS:?AGENT_LOOP_ORIGIN_PUSH_URLS is required}"

real_git="$AGENT_LOOP_REAL_GIT"

require_origin_identity() {
    local fetch_urls push_urls
    fetch_urls="$("$real_git" remote get-url --all origin)" || return 1
    push_urls="$("$real_git" remote get-url --push --all origin)" || return 1
    if [ "$fetch_urls" != "$AGENT_LOOP_ORIGIN_FETCH_URLS" ] || \
       [ "$push_urls" != "$AGENT_LOOP_ORIGIN_PUSH_URLS" ]; then
        echo "review-push rejects changed origin fetch/push identity" >&2
        return 1
    fi
}

case "$AGENT_LOOP_BRANCH" in
    refs/*|*:*|*' '*|*'~'*|*'^'*|*'?'*|*'['*|*\\*)
        echo "captured issue branch is not a safe branch name" >&2
        exit 1
        ;;
esac
"$real_git" check-ref-format --branch "$AGENT_LOOP_BRANCH" >/dev/null

actual_root="$("$real_git" rev-parse --show-toplevel)"
[ "$actual_root" = "$AGENT_LOOP_WORKTREE" ] || {
    echo "review-push must run from the captured issue worktree" >&2
    exit 1
}
actual_branch="$("$real_git" symbolic-ref --quiet --short HEAD)" || {
    echo "review-push rejects detached HEAD" >&2
    exit 1
}
[ "$actual_branch" = "$AGENT_LOOP_BRANCH" ] || {
    echo "review-push rejects a different checked-out branch" >&2
    exit 1
}
worktree_status="$("$real_git" status --porcelain)" || {
    echo "review-push could not inspect worktree cleanliness" >&2
    exit 1
}
[ -z "$worktree_status" ] || {
    echo "review-push requires a clean committed worktree" >&2
    exit 1
}

local_head="$("$real_git" rev-parse HEAD)"
"$real_git" merge-base --is-ancestor "$AGENT_LOOP_PR_HEAD_SHA" "$local_head" || {
    echo "review-push rejects non-forward review history" >&2
    exit 1
}
expected_remote_head="$AGENT_LOOP_PR_HEAD_SHA"
if [ -n "${AGENT_LOOP_REVIEW_PUSH_STATE_FILE:-}" ]; then
    if [ -L "$AGENT_LOOP_REVIEW_PUSH_STATE_FILE" ] || \
       [ ! -f "$AGENT_LOOP_REVIEW_PUSH_STATE_FILE" ]; then
        echo "review-push requires the wrapper-owned remote-head checkpoint" >&2
        exit 1
    fi
    IFS= read -r expected_remote_head < "$AGENT_LOOP_REVIEW_PUSH_STATE_FILE" || {
        echo "review-push could not read the remote-head checkpoint" >&2
        exit 1
    }
fi
[[ "$expected_remote_head" =~ ^[0-9a-f]{40}$ ]] || {
    echo "review-push found an invalid remote-head checkpoint" >&2
    exit 1
}
"$real_git" merge-base --is-ancestor "$expected_remote_head" "$local_head" || {
    echo "review-push rejects history that drops a previously published review commit" >&2
    exit 1
}
require_origin_identity
remote_line="$("$real_git" ls-remote --heads origin "refs/heads/$AGENT_LOOP_BRANCH")"
[ -n "$remote_line" ] || {
    echo "review-push requires the captured remote issue branch" >&2
    exit 1
}
remote_head="${remote_line%%[[:space:]]*}"
[ "$remote_head" = "$expected_remote_head" ] || {
    echo "review-push rejects a stale or uncertain remote head" >&2
    exit 1
}

require_origin_identity
"$real_git" push origin \
    "--force-with-lease=refs/heads/$AGENT_LOOP_BRANCH:$expected_remote_head" \
    "$local_head:refs/heads/$AGENT_LOOP_BRANCH"
require_origin_identity
observed="$("$real_git" ls-remote --heads origin "refs/heads/$AGENT_LOOP_BRANCH")"
require_origin_identity
[ "${observed%%[[:space:]]*}" = "$local_head" ] || {
    echo "review-push could not attest the remote head" >&2
    exit 1
}
if [ -n "${AGENT_LOOP_REVIEW_PUSH_STATE_FILE:-}" ]; then
    checkpoint_dir="$(dirname -- "$AGENT_LOOP_REVIEW_PUSH_STATE_FILE")"
    checkpoint_tmp="$(mktemp "$checkpoint_dir/.review-push-state.XXXXXX")"
    trap 'rm -f -- "$checkpoint_tmp"' EXIT
    chmod 600 "$checkpoint_tmp"
    printf '%s\n' "$local_head" > "$checkpoint_tmp"
    mv -f -- "$checkpoint_tmp" "$AGENT_LOOP_REVIEW_PUSH_STATE_FILE"
    trap - EXIT
fi
printf '%s\n' "$local_head"
