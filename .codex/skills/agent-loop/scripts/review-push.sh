#!/usr/bin/env bash
# Wrapper-owned, fail-closed publication of a review hook's committed fixes.

set -euo pipefail

if [ "$#" -eq 1 ] && [ "$1" = --protocol-version ]; then
    printf '2\n'
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
: "${AGENT_LOOP_REVIEW_PUSH_STATE_FILE:?AGENT_LOOP_REVIEW_PUSH_STATE_FILE is required}"
: "${AGENT_LOOP_REVIEW_VALIDATION_HOOK:?AGENT_LOOP_REVIEW_VALIDATION_HOOK is required}"
: "${AGENT_LOOP_PROCESS_SUPERVISOR:?AGENT_LOOP_PROCESS_SUPERVISOR is required}"
: "${AGENT_LOOP_PROCESS_SUPERVISOR_SHA256:?AGENT_LOOP_PROCESS_SUPERVISOR_SHA256 is required}"
: "${AGENT_LOOP_HOOK_TIMEOUT_SECONDS:?AGENT_LOOP_HOOK_TIMEOUT_SECONDS is required}"
: "${AGENT_LOOP_REVIEW_DEADLINE_EPOCH:?AGENT_LOOP_REVIEW_DEADLINE_EPOCH is required}"
: "${AGENT_LOOP_REVIEW_VALIDATION_LOG:?AGENT_LOOP_REVIEW_VALIDATION_LOG is required}"
: "${AGENT_LOOP_HOOK_GUARD_BIN:?AGENT_LOOP_HOOK_GUARD_BIN is required}"

real_git="$AGENT_LOOP_REAL_GIT"
expected_config_sha256="${AGENT_LOOP_GIT_CONFIG_SHA256:?AGENT_LOOP_GIT_CONFIG_SHA256 is required}"
state_file="$AGENT_LOOP_REVIEW_PUSH_STATE_FILE"
state_lock="${state_file}.lock"

for private_file in "$state_file" "$state_lock"; do
    [ -f "$private_file" ] && [ ! -L "$private_file" ] || {
        echo "review-push requires its wrapper-owned private pass journal" >&2
        exit 1
    }
    [ "$(stat -c '%u:%a' "$private_file")" = "$(id -u):600" ] || {
        echo "review-push pass journal must be owner-only" >&2
        exit 1
    }
done
exec {state_lock_fd}<>"$state_lock"
flock -x "$state_lock_fd"

cd -- "$AGENT_LOOP_WORKTREE" || {
    echo "review-push could not enter the captured issue worktree" >&2
    exit 1
}

# The review process is untrusted and may export transport, repository, index,
# config-location, or credential-helper overrides that never appear in
# `git config --list`. Drop the complete Git environment namespace before the
# first Git invocation. The helper's required inputs deliberately use the
# AGENT_LOOP_* namespace, so none are lost here.
while IFS= read -r variable; do
    case "$variable" in GIT_*) unset "$variable" ;; esac
done < <(compgen -e)
export GIT_TERMINAL_PROMPT=0
# The purge above also drops GIT_NO_REPLACE_OBJECTS. `refs/replace/*` is a shared
# ref namespace the review process may write and `config --list` never shows, so
# restore it before the head-identity and ancestry gates below read the object
# graph they are supposed to prove things about.
export GIT_NO_REPLACE_OBJECTS=1
actual_config_sha256="$(timeout 10 "$real_git" --no-replace-objects config \
        --null --list | sha256sum | awk '{print $1}')" || {
    echo "review-push could not verify trusted Git configuration" >&2
    exit 1
}
[ "$actual_config_sha256" = "$expected_config_sha256" ] || {
    echo "review-push rejects changed Git configuration" >&2
    exit 1
}

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
worktree_status="$("$real_git" -c core.fsmonitor=false status --porcelain)" || {
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
state_json="$(cat "$state_file")" || {
    echo "review-push could not read its pass journal" >&2
    exit 1
}
jq -e '
    type == "object" and keys == ["publishedSha", "startSha", "validatedSha", "validationReceipt", "version"] and
    .version == 2 and (.startSha | type == "string") and
    ((.validatedSha == null) or (.validatedSha | type == "string")) and
    ((.publishedSha == null) or (.publishedSha | type == "string")) and
    ((.validationReceipt == null) or (.validationReceipt | type == "string"))
' <<<"$state_json" >/dev/null || {
    echo "review-push pass journal is invalid" >&2
    exit 1
}
expected_remote_head="$(jq -r '.startSha' <<<"$state_json")"
validated_head="$(jq -r '.validatedSha // empty' <<<"$state_json")"
published_head="$(jq -r '.publishedSha // empty' <<<"$state_json")"
validation_receipt="$(jq -r '.validationReceipt // empty' <<<"$state_json")"
[[ "$expected_remote_head" =~ ^[0-9a-f]{40}$ ]] || {
    echo "review-push found an invalid remote-head checkpoint" >&2
    exit 1
}
[ -z "$published_head" ] || {
    echo "review-push permits only one publication per reviewer pass" >&2
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
    if [ "$remote_head" = "$local_head" ] && [ "$validated_head" = "$local_head" ] && \
       [[ "$validation_receipt" =~ ^[0-9a-f]{64}$ ]] && [ -z "$published_head" ]; then
        journal_dir="$(dirname -- "$state_file")"
        journal_tmp="$(mktemp "$journal_dir/.review-push-state.XXXXXX")"
        chmod 600 "$journal_tmp"
        jq -n --arg start "$expected_remote_head" --arg head "$local_head" \
            --arg receipt "$validation_receipt" \
            '{version:2,startSha:$start,validatedSha:$head,publishedSha:$head,validationReceipt:$receipt}' \
            > "$journal_tmp"
        mv -f -- "$journal_tmp" "$state_file"
        printf '%s\n' "$local_head"
        exit 0
    fi
    echo "review-push rejects a stale or uncertain remote head" >&2
    exit 1
}

supervisor_sha="$(sha256sum "$AGENT_LOOP_PROCESS_SUPERVISOR" | awk '{print $1}')" || {
    echo "review-push could not verify the process supervisor" >&2
    exit 1
}
[ "$supervisor_sha" = "$AGENT_LOOP_PROCESS_SUPERVISOR_SHA256" ] || {
    echo "review-push rejects a changed process supervisor" >&2
    exit 1
}
[[ "$AGENT_LOOP_HOOK_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
    echo "review-push validation timeout is invalid" >&2
    exit 1
}
[[ "$AGENT_LOOP_REVIEW_DEADLINE_EPOCH" =~ ^[1-9][0-9]*$ ]] || {
    echo "review-push whole-run deadline is invalid" >&2
    exit 1
}
remaining_seconds=$((AGENT_LOOP_REVIEW_DEADLINE_EPOCH - $(date +%s)))
[ "$remaining_seconds" -gt 0 ] || {
    echo "review-push whole-run deadline is exhausted" >&2
    exit 1
}
validation_timeout="$AGENT_LOOP_HOOK_TIMEOUT_SECONDS"
[ "$remaining_seconds" -ge "$validation_timeout" ] || validation_timeout="$remaining_seconds"

validation_status=0
(
    unset AGENT_LOOP_REVIEW_PUSH_HELPER AGENT_LOOP_ORIGIN_FETCH_URLS \
        AGENT_LOOP_ORIGIN_PUSH_URLS AGENT_LOOP_REVIEW_CONTRACT_VERSION
    export AGENT_LOOP_ALLOW_REVIEW_MUTATIONS=false
    export AGENT_LOOP_HOOK_COMMAND="$AGENT_LOOP_REVIEW_VALIDATION_HOOK"
    export PATH="$AGENT_LOOP_HOOK_GUARD_BIN:$PATH"
    export GIT_CONFIG_COUNT=2
    export GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false
    export GIT_CONFIG_KEY_1=core.hooksPath GIT_CONFIG_VALUE_1=/dev/null
    python3 -I "$AGENT_LOOP_PROCESS_SUPERVISOR" \
        --timeout-seconds "$validation_timeout" \
        --kill-after-seconds 15 -- bash -lc \
        'unset -f git gh 2>/dev/null || true; unalias git gh 2>/dev/null || true; export PATH="$AGENT_LOOP_HOOK_GUARD_BIN:$PATH"; eval "$AGENT_LOOP_HOOK_COMMAND"'
) >"$AGENT_LOOP_REVIEW_VALIDATION_LOG" 2>&1 || validation_status=$?
if [ "$validation_status" -ne 0 ]; then
    echo "review-push pre-publication validation failed (exit $validation_status)" >&2
    tail -n 40 "$AGENT_LOOP_REVIEW_VALIDATION_LOG" >&2 || true
    exit "$validation_status"
fi
require_origin_identity
actual_config_sha256="$(timeout 10 "$real_git" --no-replace-objects config \
        --null --list | sha256sum | awk '{print $1}')" || exit 1
[ "$actual_config_sha256" = "$expected_config_sha256" ] || {
    echo "review-push validation changed trusted Git configuration" >&2
    exit 1
}
[ "$local_head" = "$("$real_git" rev-parse HEAD)" ] && \
    [ -z "$("$real_git" -c core.fsmonitor=false status --porcelain)" ] || {
    echo "review-push validation mutated the candidate head or worktree" >&2
    exit 1
}
validation_receipt="$(
    { printf '%s\n' "$expected_remote_head" "$local_head" "$expected_config_sha256"; \
      sha256sum "$AGENT_LOOP_REVIEW_VALIDATION_LOG"; } | sha256sum | awk '{print $1}'
)"
journal_dir="$(dirname -- "$state_file")"
journal_tmp="$(mktemp "$journal_dir/.review-push-state.XXXXXX")"
chmod 600 "$journal_tmp"
jq -n --arg start "$expected_remote_head" --arg head "$local_head" \
    --arg receipt "$validation_receipt" \
    '{version:2,startSha:$start,validatedSha:$head,publishedSha:null,validationReceipt:$receipt}' \
    > "$journal_tmp"
mv -f -- "$journal_tmp" "$state_file"

require_origin_identity
"$real_git" -c core.hooksPath=/dev/null -c core.fsmonitor=false push origin \
    "--force-with-lease=refs/heads/$AGENT_LOOP_BRANCH:$expected_remote_head" \
    "$local_head:refs/heads/$AGENT_LOOP_BRANCH"
require_origin_identity
observed="$("$real_git" ls-remote --heads origin "refs/heads/$AGENT_LOOP_BRANCH")"
require_origin_identity
[ "${observed%%[[:space:]]*}" = "$local_head" ] || {
    echo "review-push could not attest the remote head" >&2
    exit 1
}
journal_tmp="$(mktemp "$journal_dir/.review-push-state.XXXXXX")"
trap 'rm -f -- "$journal_tmp"' EXIT
chmod 600 "$journal_tmp"
jq -n --arg start "$expected_remote_head" --arg head "$local_head" \
    --arg receipt "$validation_receipt" \
    '{version:2,startSha:$start,validatedSha:$head,publishedSha:$head,validationReceipt:$receipt}' \
    > "$journal_tmp"
mv -f -- "$journal_tmp" "$state_file"
trap - EXIT
printf '%s\n' "$local_head"
