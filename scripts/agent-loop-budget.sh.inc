# Shared budget protocol; rendered into the three controllers.
prepare_review_pass_budget() {
    local remaining
    if [ -n "$AGENT_LOOP_RUN_STATE_FILE" ]; then
        remaining="$(budget_state budget-show --file "$AGENT_LOOP_RUN_STATE_FILE" \
            --lock-fd "$AGENT_LOOP_RUN_LOCK_FD")" || return 1
    else
        remaining="$REVIEW_REMAINING_SECONDS"
    fi
    if [ "$remaining" -lt "$REVIEW_PASS_MIN_SECONDS" ]; then
        recovery_message "Local review exhausted its active execution budget." budget-exhausted
        return 1
    fi
    REVIEW_PASS_TIMEOUT_SECONDS="$HOOK_TIMEOUT_SECONDS"
    if [ "$remaining" -lt "$REVIEW_PASS_TIMEOUT_SECONDS" ]; then
        REVIEW_PASS_TIMEOUT_SECONDS="$remaining"
    fi
    # Compatibility for nested pre-push validation. This transient bound is
    # enclosed by the hook timeout and is never restored from a checkpoint.
    REVIEW_DEADLINE_EPOCH=$(( $(date +%s) + REVIEW_PASS_TIMEOUT_SECONDS ))
    echo "Review execution budget: ${remaining}s; hook bound ${REVIEW_PASS_TIMEOUT_SECONDS}s"
}

budget_begin() {
    local seconds="$1"
    if [ -n "$AGENT_LOOP_RUN_STATE_FILE" ]; then
        BUDGET_ATTEMPT="$(budget_state budget-begin --file "$AGENT_LOOP_RUN_STATE_FILE" \
            --lock-fd "$AGENT_LOOP_RUN_LOCK_FD" --seconds "$seconds" --owner "$$")" || return 1
    else
        [ "$seconds" -le "$REVIEW_REMAINING_SECONDS" ] || return 1
        REVIEW_REMAINING_SECONDS=$((REVIEW_REMAINING_SECONDS - seconds))
        BUDGET_STARTED_NS="$(python3 -c 'import time; print(time.monotonic_ns())')" || return 1
    fi
}

budget_finish() {
    local seconds="$1" elapsed
    if [ -n "$AGENT_LOOP_RUN_STATE_FILE" ]; then
        budget_state budget-finish --file "$AGENT_LOOP_RUN_STATE_FILE" \
            --lock-fd "$AGENT_LOOP_RUN_LOCK_FD" --attempt "$BUDGET_ATTEMPT" --owner "$$" >/dev/null || return 1
    else
        elapsed="$(python3 -c 'import sys,time; print(max(1,(time.monotonic_ns()-int(sys.argv[1])+999999999)//1000000000))' "$BUDGET_STARTED_NS")" || return 1
        [ "$elapsed" -le "$seconds" ] || elapsed="$seconds"
        REVIEW_REMAINING_SECONDS=$((REVIEW_REMAINING_SECONDS + seconds - elapsed))
    fi
}
