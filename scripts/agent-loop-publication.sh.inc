# Shared pre-publication recovery, rendered inline into each pinned controller.
loop_state() {
    if declare -F run_state_helper >/dev/null; then
        run_state_helper "$@"
    else
        python3 -I "$RUN_STATE_HELPER" "$@"
    fi
}

prepared_validation_hook() {
    if [ -n "$PREPARATION_HOOK" ]; then
        # An AND-list disables errexit inside its left-hand subshell.
        printf '( %s\n)\nactiveloom_preparation_status=$?\nif [ "$activeloom_preparation_status" -ne 0 ]; then exit "$activeloom_preparation_status"; fi\n( %s\n)' \
            "$PREPARATION_HOOK" "$VALIDATION_HOOK"
    else
        printf '%s' "$VALIDATION_HOOK"
    fi
}

integrate_initial_base() {
    local target before
    fetch_base || return 1
    target="$(git rev-parse "${ISSUE_BASE_REMOTE_REF:-$BASE_REMOTE_REF}")" || return 1
    before="$(git rev-parse HEAD)" || return 1
    if [ -n "$AGENT_LOOP_RUN_STATE_FILE" ]; then
        update_run_state integrating 1 "$target" "$before" || return 1
    fi
    if ! "$REAL_GIT_BIN" -c core.hooksPath=/dev/null -c core.fsmonitor=false merge --no-edit "$target"; then
        "$REAL_GIT_BIN" -c core.hooksPath=/dev/null -c core.fsmonitor=false merge --abort >/dev/null 2>&1 || true
        return 1
    fi
    update_run_state integrated 1 "$target" "$(git rev-parse HEAD)"
}

publish_initial_branch() {
    local branch="$1" head="$2" base="$3" remote phase="" saved
    remote="$("$REAL_GIT_BIN" ls-remote --heads origin "refs/heads/$branch")" || return 1
    if [ -n "$AGENT_LOOP_RUN_STATE_FILE" ]; then
        saved="$(loop_state show --file "$AGENT_LOOP_RUN_STATE_FILE")" || return 1
        phase="$(jq -r '.phase' <<<"$saved")"
        [ "$(jq -r '.headSha' <<<"$saved")" = "$head" ] && \
            [ "$(jq -r '.baseSha' <<<"$saved")" = "$base" ] || return 1
    fi
    if [ -n "$remote" ]; then
        # Only a saved publication intent permits adopting a matching remote.
        case "$phase" in publishing|pushed) ;; *) echo "Remote branch existed before publication intent" >&2; return 1 ;; esac
        [ "${remote%%[[:space:]]*}" = "$head" ] || { echo "Initial publication remote head mismatch" >&2; return 1; }
    else
        [ "$phase" != pushed ] || { echo "Previously pushed branch disappeared" >&2; return 1; }
        update_run_state publishing 1 "$base" "$head" || return 1
        if declare -F require_trusted_git_config >/dev/null; then
            require_trusted_git_config || return 1
        fi
        "$REAL_GIT_BIN" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
            push --force-with-lease="refs/heads/$branch:" origin "$head:refs/heads/$branch" || return 1
    fi
    attest_remote_branch "$branch" "$head" "after initial publication push" || return 1
    update_run_state pushed 1 "$base" "$head" || return 1
}

find_existing_initial_pr() {
    local branch="$1" head="$2" rows count
    EXISTING_INITIAL_PR_URL=""
    EXISTING_INITIAL_PR_NUMBER=""
    # Include closed PRs and all pages: neither duplicates nor a closed publication
    # may be silently replaced. Ignore forks that share a branch name.
    rows="$(gh api --paginate --slurp "repos/$GH_REPO/pulls?state=all&head=${GH_REPO%%/*}:$branch&per_page=100")" || return 1
    rows="$(jq --arg repo "$GH_REPO" --arg branch "$branch" \
        '[.[][] | select(.head.repo.full_name == $repo and .head.ref == $branch)]' <<<"$rows")" || return 1
    count="$(jq length <<<"$rows")" || return 1
    [ "$count" -le 1 ] || { echo "Multiple PRs match the publication branch" >&2; return 1; }
    if [ "$count" -eq 1 ]; then
        jq -e --arg repo "$GH_REPO" --arg base "${ISSUE_BASE_BRANCH:-$BASE_BRANCH}" --arg head "$head" \
            '.[0] | .state == "open" and .draft == true and .head.sha == $head and .base.ref == $base and .base.repo.full_name == $repo' \
            <<<"$rows" >/dev/null || { echo "Existing initial PR does not match the saved publication" >&2; return 1; }
        EXISTING_INITIAL_PR_NUMBER="$(jq -er '.[0].number | select(type == "number" and . > 0)' <<<"$rows")" || return 1
        EXISTING_INITIAL_PR_URL="$(jq -er '.[0].html_url | select(type == "string" and length > 0)' <<<"$rows")" || return 1
    fi
}

resume_initial_publication() {
    local phase="$1" saved_head="$2" current base parents
    if [ "$phase" = worker-running ]; then
        recovery_message "Worker completion was not checkpointed; preserve and inspect its work. The worker will not be replayed." worker-ambiguous-bail
        return 1
    fi
    current="$(git rev-parse HEAD)" || return 1
    base="$(jq -r '.baseSha' <<<"$RESUME_STATE_JSON")" || return 1
    if [ "$current" != "$saved_head" ]; then
        # The only uncheckpointed head transition accepted is the controller's
        # recorded integration: fast-forward to the target or one merge of it.
        [ "$phase" = integrating ] || { echo "Pre-publication head changed" >&2; return 1; }
        parents="$(git show -s --format=%P "$current")" || return 1
        if [ "$current" = "$base" ]; then
            git merge-base --is-ancestor "$saved_head" "$base" || return 1
        elif [ "$parents" != "$saved_head $base" ]; then
            echo "Interrupted integration does not match its recorded parents" >&2
            return 1
        fi
        update_run_state integrated 1 "$base" "$current" || return 1
    fi
    case "$phase" in
        worker-complete|integrating|integrated)
            integrate_initial_base || return 1
            base="$(git rev-parse "${ISSUE_BASE_REMOTE_REF:-$BASE_REMOTE_REF}")" || return 1
            ;;
    esac
    inspect_publication_diff "$base" || return 1
    run_validation "resumed-initial-publication" || return 1
    find_existing_initial_pr "$AGENT_LOOP_BRANCH" "$(git rev-parse HEAD)" || return 1
    verify_issue_for_publication "$SELECTED_ID" "$EXISTING_INITIAL_PR_NUMBER" || return 1
    open_draft_pr "$SELECTED_ID" "$AGENT_LOOP_BRANCH" "$(git rev-parse HEAD)" "$base" || return 1
    if human_glance_gate "$base" "$(git rev-parse HEAD)"; then
        recovery_message "Draft PR requires a human glance; no review chain was started." human-glance
        return 1
    fi
}
