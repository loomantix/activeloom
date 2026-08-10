#!/usr/bin/env bash
# Deterministic, per-issue Claude agent loop with PR-first local reviews.
#
# The wrapper owns selection, claiming, worktrees, local reviews, base
# integration, draft PR creation, and review convergence. A worker only
# implements, validates, refactors, and commits locally — it never pushes or
# opens a pull request. No Gemini, Copilot, `reviewit`, or GitHub-hosted AI
# reviewer is invoked by this script.
#
# Source of truth: upstream `.claude/skills/agent-loop/scripts/`. Synced to
# consumers verbatim; edits in a consumer repo are overwritten on next sync.

set -euo pipefail
# Worktrees and persistent worker/reviewer logs can contain sensitive source or
# test output. Default every path this wrapper creates to owner-only access.
umask 077

unset AGENT_LOOP_REVIEW_BASE AGENT_LOOP_REVIEW_BASE_SHA AGENT_LOOP_REVIEW_ENGINE \
    AGENT_LOOP_REVIEW_ACTOR \
    AGENT_LOOP_REVIEW_OUTCOME_FILE AGENT_LOOP_REVIEW_RESULT_FILE \
    AGENT_LOOP_REVIEW_ROUND AGENT_LOOP_PR_NUMBER AGENT_LOOP_PR_URL \
    AGENT_LOOP_PR_HEAD_SHA

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

MAX_ITERATIONS=10
ISSUE_ALLOWLIST=""
RESUME_IN_PROGRESS=false
DRY_RUN=false
LEGACY_ITERATIONS_SEEN=false

usage() {
    cat <<'EOF'
Usage: agent-loop.sh [iterations] [options]

Options:
  --iterations N       Process at most N issues (default: 10).
  --issues N,N,...     Restrict selection to this explicit issue allowlist.
  --include-assigned   Include eligible issues assigned only to @me.
  --resume             Deprecated alias for --include-assigned.
  --dry-run            Show selection, gates, paths, hooks, and publication only.
  -h, --help           Show this help.

The legacy numeric first argument remains supported. Collection branches are no
longer supported: every issue receives its own branch, worktree, and pull
request. The removed `[collection-branch]` positional now errors instead of
being silently reinterpreted.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --iterations)
            [ "$#" -ge 2 ] || { echo "--iterations requires a value" >&2; exit 2; }
            MAX_ITERATIONS="$2"
            shift 2
            ;;
        --issues)
            [ "$#" -ge 2 ] || { echo "--issues requires a comma-separated value" >&2; exit 2; }
            ISSUE_ALLOWLIST="$2"
            shift 2
            ;;
        --resume|--include-assigned)
            RESUME_IN_PROGRESS=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        [0-9]*)
            if [ "$LEGACY_ITERATIONS_SEEN" = true ]; then
                echo "unexpected positional argument: $1" >&2
                exit 2
            fi
            MAX_ITERATIONS="$1"
            LEGACY_ITERATIONS_SEEN=true
            shift
            ;;
        *)
            # A non-numeric positional was almost certainly the removed
            # collection-branch argument. Fail loud rather than silently
            # reinterpret it — a caller expecting the old behavior must migrate.
            echo "unexpected argument: $1" >&2
            echo "collection branches were removed; use --issues N,N,... to scope a run" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if ! [[ "$MAX_ITERATIONS" =~ ^[1-9][0-9]*$ ]]; then
    echo "iterations must be a positive integer: $MAX_ITERATIONS" >&2
    exit 2
fi
if [ -n "$ISSUE_ALLOWLIST" ] && ! [[ "$ISSUE_ALLOWLIST" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]]; then
    echo "--issues must be a comma-separated list of positive issue numbers" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${AGENT_LOOP_PROJECT_DIR:-}" ]; then
    PROJECT_DIR="$AGENT_LOOP_PROJECT_DIR"
else
    PROJECT_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
fi
if [ -z "$PROJECT_DIR" ]; then
    echo "Could not find a Git repository from $SCRIPT_DIR" >&2
    exit 1
fi

CONFIG_FILE="$PROJECT_DIR/.claude/skills/agent-loop/agent-loop.config"
PROMPT_FILE="$PROJECT_DIR/.claude/skills/agent-loop/prompt.txt"
INSTRUCTIONS_FILE="$PROJECT_DIR/agent-loop-instructions.md"
ISSUES_READY="$PROJECT_DIR/.claude/skills/issues/scripts/ready.py"
REVIEW_LEDGER="$PROJECT_DIR/.claude/skills/critique/scripts/review-ledger.py"

BASE_BRANCH=""
SETUP_HOOK=""
VALIDATION_HOOK=""
CLAUDE_REVIEW_HOOK=""
CODEX_REVIEW_HOOK=""
WORKER_HOOK=""
WORKER_MODEL=""
WORKER_FALLBACK_MODEL=""
WORKER_RETRIES=1
WORKER_TIMEOUT_SECONDS=3600
HOOK_TIMEOUT_SECONDS=3600
REVIEW_CONTRACT_VERSION=""
REVIEW_MAX_ROUNDS=4
RETRY_ON_TIMEOUT=true
RETRY_DELAY_SECONDS=15
DEPENDENCY_GATE=ready
BRANCH_PREFIX=agent-loop
WORKTREE_ROOT="${TMPDIR:-/tmp}/agent-loop-worktrees"
LOG_ROOT="${TMPDIR:-/tmp}/agent-loop-logs"
LOG_MAX_KB=1024
OUTPUT_MAX_LINES=40

assign_config() {
    local key="$1" value="$2"
    case "$key" in
        base_branch) BASE_BRANCH="$value" ;;
        setup_hook) SETUP_HOOK="$value" ;;
        validation_hook) VALIDATION_HOOK="$value" ;;
        claude_review_hook) CLAUDE_REVIEW_HOOK="$value" ;;
        codex_review_hook) CODEX_REVIEW_HOOK="$value" ;;
        worker_hook) WORKER_HOOK="$value" ;;
        worker_model) WORKER_MODEL="$value" ;;
        worker_fallback_model) WORKER_FALLBACK_MODEL="$value" ;;
        worker_retries) WORKER_RETRIES="$value" ;;
        worker_timeout_seconds) WORKER_TIMEOUT_SECONDS="$value" ;;
        hook_timeout_seconds) HOOK_TIMEOUT_SECONDS="$value" ;;
        review_contract_version) REVIEW_CONTRACT_VERSION="$value" ;;
        review_max_rounds) REVIEW_MAX_ROUNDS="$value" ;;
        retry_on_timeout) RETRY_ON_TIMEOUT="$value" ;;
        retry_delay_seconds) RETRY_DELAY_SECONDS="$value" ;;
        dependency_gate) DEPENDENCY_GATE="$value" ;;
        branch_prefix) BRANCH_PREFIX="$value" ;;
        worktree_root) WORKTREE_ROOT="$value" ;;
        log_root) LOG_ROOT="$value" ;;
        log_max_kb) LOG_MAX_KB="$value" ;;
        output_max_lines) OUTPUT_MAX_LINES="$value" ;;
        *) echo "unknown agent-loop config key: $key" >&2; exit 1 ;;
    esac
}

if [ -e "$CONFIG_FILE" ]; then
    if [ ! -f "$CONFIG_FILE" ] || [ ! -r "$CONFIG_FILE" ]; then
        echo "agent-loop config is not a readable regular file: $CONFIG_FILE" >&2
        exit 1
    fi
    declare -A CONFIG_KEYS=()
    while IFS= read -r raw || [ -n "$raw" ]; do
        line="${raw#"${raw%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [ -z "$line" ] && continue
        [[ "$line" == \#* ]] && continue
        if ! [[ "$line" =~ ^([a-z_]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
            echo "invalid agent-loop config line: $raw" >&2
            exit 1
        fi
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        value="${value%"${value##*[![:space:]]}"}"
        [ -z "${CONFIG_KEYS[$key]:-}" ] || { echo "duplicate agent-loop config key: $key" >&2; exit 1; }
        CONFIG_KEYS[$key]=1
        assign_config "$key" "$value"
    done < "$CONFIG_FILE"
fi

if [ "$REVIEW_CONTRACT_VERSION" != 2 ] && [ "$REVIEW_CONTRACT_VERSION" != 3 ]; then
    echo "agent-loop config must set review_contract_version = 2 or review_contract_version = 3" >&2
    exit 1
fi
if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
    if [ ! -f "$REVIEW_LEDGER" ] || [ ! -r "$REVIEW_LEDGER" ]; then
        echo "review contract v3 helper is unavailable: $REVIEW_LEDGER" >&2
        exit 1
    fi
    [ "$(python3 "$REVIEW_LEDGER" --protocol-version)" = 3 ] || {
        echo "review contract v3 requires review-ledger.py protocol version 3" >&2
        exit 1
    }
fi

BASE_BRANCH="${AGENT_LOOP_BASE_BRANCH:-$BASE_BRANCH}"
if [ -z "$BASE_BRANCH" ]; then
    BASE_BRANCH="$(git -C "$PROJECT_DIR" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || true)"
fi
BASE_BRANCH="${BASE_BRANCH:-main}"

validate_ref_component() {
    local value="$1" label="$2"
    if ! git -C "$PROJECT_DIR" check-ref-format --branch "$value" >/dev/null 2>&1; then
        echo "$label is not a valid branch name: $value" >&2
        exit 1
    fi
}
validate_ref_component "$BASE_BRANCH" "base branch"
validate_ref_component "$BRANCH_PREFIX/example" "branch prefix"

for value in "$WORKER_RETRIES" "$WORKER_TIMEOUT_SECONDS" "$HOOK_TIMEOUT_SECONDS" \
             "$REVIEW_MAX_ROUNDS" "$RETRY_DELAY_SECONDS" "$LOG_MAX_KB" "$OUTPUT_MAX_LINES"; do
    [[ "$value" =~ ^[0-9]+$ ]] || { echo "numeric agent-loop config value required: $value" >&2; exit 1; }
done
[ "$REVIEW_MAX_ROUNDS" -gt 0 ] || { echo "review_max_rounds must be positive" >&2; exit 1; }
case "$RETRY_ON_TIMEOUT" in true|false) ;; *) echo "retry_on_timeout must be true or false" >&2; exit 1 ;; esac
case "$DEPENDENCY_GATE" in ready|merged-to-base) ;; *) echo "dependency_gate must be ready or merged-to-base" >&2; exit 1 ;; esac

for cmd in git gh jq python3 timeout; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "required command not found: $cmd" >&2; exit 1; }
done
if [ -z "$WORKER_HOOK" ]; then
    # The default worker shells out to the Claude CLI (see worker_command). A
    # consumer that sets worker_hook supplies its own runner and doesn't need it.
    command -v claude >/dev/null 2>&1 || {
        echo "required command not found for default worker: claude" >&2
        exit 1
    }
fi
[ -x "$ISSUES_READY" ] || { echo "issues ready.py not found or not executable: $ISSUES_READY" >&2; exit 1; }
[ -f "$INSTRUCTIONS_FILE" ] || { echo "agent-loop-instructions.md not found at repository root" >&2; exit 1; }
[ -n "$CLAUDE_REVIEW_HOOK" ] || { echo "claude_review_hook must be configured before running agent-loop" >&2; exit 1; }
[ -n "$CODEX_REVIEW_HOOK" ] || { echo "codex_review_hook must be configured before running agent-loop" >&2; exit 1; }

if [ -s "$PROMPT_FILE" ] && [ -r "$PROMPT_FILE" ]; then
    PROMPT_TEMPLATE="$(<"$PROMPT_FILE")"
else
    PROMPT_TEMPLATE="Read @agent-loop-instructions.md. Implement issue #{ISSUE_ID}, validate it, and commit locally. Do not push or open a pull request."
fi
[[ "$PROMPT_TEMPLATE" == *"{ISSUE_ID}"* ]] || { echo "prompt template must contain {ISSUE_ID}: $PROMPT_FILE" >&2; exit 1; }

cd "$PROJECT_DIR"
GH_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" || {
    echo "could not resolve the GitHub repository for $PROJECT_DIR" >&2
    exit 1
}
export GH_REPO
if [ "$DRY_RUN" = false ]; then
    git fetch origin "$BASE_BRANCH" --quiet
fi
git rev-parse --verify --quiet "refs/remotes/origin/$BASE_BRANCH" >/dev/null || {
    echo "configured base branch does not exist locally: origin/$BASE_BRANCH" >&2
    [ "$DRY_RUN" = true ] && echo "Dry-run does not fetch; fetch the base branch once and retry." >&2
    exit 1
}

# Resolve the current login once, up front. Doing it per-candidate inside an
# unchecked command substitution meant a transient gh failure silently rendered a
# resume-eligible issue "not mine" and skipped it.
CURRENT_LOGIN="$(gh api user --jq .login)" || {
    echo "could not determine current GitHub login (is gh authenticated?)" >&2
    exit 1
}
[ -n "$CURRENT_LOGIN" ] || { echo "current GitHub login resolved empty" >&2; exit 1; }
export AGENT_LOOP_REVIEW_ACTOR="$CURRENT_LOGIN"

REPO_NAME="$(basename "$PROJECT_DIR")"
RUN_TAG="$(date -u +%Y%m%d-%H%M%S)-$$"
ACTIVE_WORKTREE=""
RECOVERY_EMITTED=false
PROCESSED_ISSUES=()

recovery_message() {
    local reason="$1"
    RECOVERY_EMITTED=true
    echo -e "${RED}✗${NC} $reason" >&2
    # Only claim the worktree is preserved when it actually exists. A linked
    # worktree has a `.git` file; if `git worktree add` never completed (the
    # backstop can fire between claim and a successful add), pointing the
    # operator at a nonexistent path with "recover your commits" is actively
    # misleading — the commands would all report "not a git repository".
    if [ -n "$ACTIVE_WORKTREE" ] && [ -e "$ACTIVE_WORKTREE/.git" ]; then
        echo "Worktree preserved: $ACTIVE_WORKTREE" >&2
        echo "Inspect with: git -C '$ACTIVE_WORKTREE' status --short --branch" >&2
        echo "Recover commits with: git -C '$ACTIVE_WORKTREE' log --oneline --decorate -10" >&2
        echo "Do not reset, reuse, or remove it until the work is recovered." >&2
    elif [ -n "$ACTIVE_WORKTREE" ]; then
        echo "No worktree exists at $ACTIVE_WORKTREE (creation did not complete)." >&2
        echo "If issue #${SELECTED_ID:-?} was claimed, unassign it before it is re-selected." >&2
    fi
}

on_interrupt() {
    recovery_message "Interrupted; no cleanup was attempted."
    exit 130
}

on_exit() {
    local rc=$?
    # Backstop for unguarded `set -e` aborts (e.g. a mid-pipeline git fetch/worktree
    # failure) that would otherwise exit while an issue is claimed and live work sits
    # in the worktree, with no recovery guidance. recovery_message sets the flag, so
    # the explicit `recovery_message; exit 1` sites never double-report.
    if [ "$rc" -ne 0 ] && [ "$RECOVERY_EMITTED" = false ] && [ -n "$ACTIVE_WORKTREE" ]; then
        recovery_message "agent-loop aborted (exit $rc) with issue #${SELECTED_ID:-unknown} claimed."
    fi
}
trap on_interrupt INT TERM
trap on_exit EXIT

already_processed() {
    local candidate="$1" seen
    for seen in "${PROCESSED_ISSUES[@]:-}"; do
        [ "$seen" = "$candidate" ] && return 0
    done
    return 1
}

issue_json() {
    gh issue view "$1" --json number,title,body,state,labels,assignees
}

issue_is_selectable() {
    local number="$1" json="$2"
    [ "$(jq -r '.state' <<<"$json")" = OPEN ] || return 1
    jq -e '.labels | any(.name == "dev: agent")' <<<"$json" >/dev/null || return 1
    local count mine
    count="$(jq '.assignees | length' <<<"$json")"
    mine="$(jq -r '.assignees | any(.login == "'"$CURRENT_LOGIN"'")' <<<"$json")"
    if [ "$count" -eq 0 ]; then
        return 0
    fi
    [ "$RESUME_IN_PROGRESS" = true ] && [ "$mine" = true ] && [ "$count" -eq 1 ]
}

SELECTED_ID=""
SELECTED_BODY=""
SELECTED_ASSIGNED=false

select_next_issue() {
    SELECTED_ID=""
    SELECTED_BODY=""
    SELECTED_ASSIGNED=false
    local json number
    if [ -n "$ISSUE_ALLOWLIST" ]; then
        local candidates allowlist_ready_json
        # An allowlist is a scope ceiling, not an eligibility bypass. Resolve the
        # same hard excludes, open blockers, and addressed-PR checks as the normal
        # ready queue, while retaining assigned-but-ready rows for --resume.
        allowlist_ready_json="$("$ISSUES_READY" --agent --limit 1000 --json)" || return 2
        # ready.py exiting 0 with a non-array payload (a warning line prepended to
        # the JSON, a caught+printed traceback, a truncated write) must be a hard
        # error, not silently read as "nothing is ready" — which would exit 0 and
        # look like an empty backlog to an automation harness checking $?.
        jq -e 'type == "array"' <<<"$allowlist_ready_json" >/dev/null 2>&1 || return 2
        IFS=',' read -r -a candidates <<< "$ISSUE_ALLOWLIST"
        for number in "${candidates[@]}"; do
            already_processed "$number" && continue
            if ! jq -e --argjson number "$number" 'any(.number == $number)' \
                <<< "$allowlist_ready_json" >/dev/null; then
                echo -e "${DIM}○${NC} Allowlisted issue #$number is not ready." >&2
                PROCESSED_ISSUES+=("$number")
                continue
            fi
            json="$(issue_json "$number")" || return 2
            if ! issue_is_selectable "$number" "$json"; then
                echo -e "${DIM}○${NC} Allowlisted issue #$number is not open, agent-labeled, or safely assignable." >&2
                PROCESSED_ISSUES+=("$number")
                continue
            fi
            SELECTED_ID="$number"
            SELECTED_BODY="$(jq -r '.body // ""' <<<"$json")"
            [ "$(jq '.assignees | length' <<<"$json")" -gt 0 ] && SELECTED_ASSIGNED=true
            return 0
        done
        return 1
    fi

    local ready_json
    if [ "$RESUME_IN_PROGRESS" = true ]; then
        ready_json="$("$ISSUES_READY" --agent --limit 100 --json)" || return 2
    else
        ready_json="$("$ISSUES_READY" --unassigned --agent --limit 100 --json)" || return 2
    fi
    jq -e 'type == "array"' <<<"$ready_json" >/dev/null 2>&1 || return 2
    while IFS= read -r number; do
        [ -n "$number" ] || continue
        already_processed "$number" && continue
        json="$(issue_json "$number")" || return 2
        issue_is_selectable "$number" "$json" || continue
        SELECTED_ID="$number"
        SELECTED_BODY="$(jq -r '.body // ""' <<<"$json")"
        [ "$(jq '.assignees | length' <<<"$json")" -gt 0 ] && SELECTED_ASSIGNED=true
        return 0
    done < <(jq -r '.[].number' <<<"$ready_json")
    return 1
}

pr_merged_to_base() {
    local pr="$1" data state base oid
    data="$(gh pr view "$pr" --json state,baseRefName,mergeCommit --jq '[.state,.baseRefName,(.mergeCommit.oid // "")] | @tsv' 2>/dev/null)" || return 1
    IFS=$'\t' read -r state base oid <<< "$data"
    [ "$state" = MERGED ] && [ "$base" = "$BASE_BRANCH" ] && [ -n "$oid" ] || return 1
    git merge-base --is-ancestor "$oid" "origin/$BASE_BRANCH" >/dev/null 2>&1
}

issue_dependency_merged() {
    local issue="$1" rows pr
    rows="$(gh issue view "$issue" --json closedByPullRequestsReferences \
        --jq '.closedByPullRequestsReferences[]? | [.number,.state,.baseRefName,(.mergeCommit.oid // "")] | @tsv' 2>/dev/null)" || return 1
    while IFS=$'\t' read -r pr _; do
        [ -n "$pr" ] || continue
        pr_merged_to_base "$pr" && return 0
    done <<< "$rows"
    return 1
}

dependency_refs() {
    python3 -c 'import re,sys
body=sys.stdin.read()
pattern=re.compile(r"(?im)^\s*[-*]?\s*(?:blocked\s+by|depends\s+on)[:\s]+(?:(pr)\s*)?#(\d+)\b")
for kind, number in pattern.findall(body):
    print(("pr" if kind else "issue") + "\t" + number)' <<< "$1"
}

check_dependencies() {
    local body="$1" kind number found=false
    if [ "$DEPENDENCY_GATE" = ready ]; then
        echo "   Dependency gate: ready-queue semantics"
        return 0
    fi
    while IFS=$'\t' read -r kind number; do
        [ -n "$number" ] || continue
        found=true
        if { [ "$kind" = pr ] && pr_merged_to_base "$number"; } || \
           { [ "$kind" = issue ] && issue_dependency_merged "$number"; }; then
            echo "   Dependency $kind #$number: merged into origin/$BASE_BRANCH"
        else
            echo "   Dependency $kind #$number: NOT merged into origin/$BASE_BRANCH"
            return 1
        fi
    done < <(dependency_refs "$body")
    [ "$found" = true ] || echo "   Dependency gate: no declared dependencies"
}

claim_issue() {
    local number="$1" claim_json login added=false
    if [ "$SELECTED_ASSIGNED" = false ]; then
        if ! gh issue edit "$number" --add-assignee @me >/dev/null; then
            echo "could not add current user as assignee on issue #$number" >&2
            return 1
        fi
        added=true
    fi

    # Refetch after selection/claim and verify identity, not merely count. A
    # concurrent reassignment can otherwise leave a different sole assignee and
    # make this worker duplicate their work. Resumes need the same fresh check.
    claim_json="$(gh issue view "$number" --json assignees)" || claim_json=""
    login="$(jq -r 'if (.assignees | length) == 1 then .assignees[0].login else "" end' \
        <<< "$claim_json" 2>/dev/null || true)"
    if [ "$login" != "$CURRENT_LOGIN" ]; then
        if [ "$added" = true ]; then
            gh issue edit "$number" --remove-assignee @me >/dev/null 2>&1 || true
        fi
        echo "claim race or verification failure for issue #$number" >&2
        return 1
    fi
    if [ "$SELECTED_ASSIGNED" = true ]; then
        echo -e "${YELLOW}›${NC} Resuming issue #$number"
    fi
}

worktree_has_work() {
    local start_sha="$1"
    [ -n "$(git status --porcelain)" ] || [ "$(git rev-parse HEAD)" != "$start_sha" ]
}

run_bounded_hook() {
    local phase="$1" command="$2" timeout_seconds="$3" log_file="$4"
    local max_bytes=$((LOG_MAX_KB * 1024)) status=0
    echo -e "${BLUE}▸${NC} $phase"
    # Bound the captured log to its trailing LOG_MAX_KB with `tail -c`, NOT with a
    # process-wide `ulimit -f`: that rlimit is inherited by the worker and every hook
    # and would SIGXFSZ-kill (and truncate) any repo file they legitimately write
    # (lockfiles, build artifacts, generated code). `tail` drains all input, so the
    # hook is never signalled; keeping the tail preserves the failing output and any
    # capacity/overload marker the retry logic greps for; PIPESTATUS[0] keeps the
    # hook's real exit status.
    (
        set +e
        timeout --signal=TERM --kill-after=15 "${timeout_seconds}s" bash -lc "$command" 2>&1 \
            | tail -c "$max_bytes"
        exit "${PIPESTATUS[0]}"
    ) >"$log_file" 2>&1 || status=$?
    if [ "$status" -ne 0 ]; then
        echo -e "${RED}✗${NC} $phase failed (exit $status); bounded tail follows:" >&2
        tail -n "$OUTPUT_MAX_LINES" "$log_file" >&2 || true
    else
        echo -e "${GREEN}✓${NC} $phase"
    fi
    return "$status"
}

worker_command() {
    local model="$1"
    if [ -n "$WORKER_HOOK" ]; then
        printf '%s' "$WORKER_HOOK"
        return
    fi
    # The default worker shells out to the Claude CLI in headless,
    # auto-approving mode. This invocation is why the script is gated by
    # .claude/lint-claude-cli-invocations.py: the locked region below is
    # hashed and must match an entry in .claude/claude-cli-invocations.allowlist,
    # so any change to the flags, model handling, or prompt wiring is
    # reviewer-visible. Keep every claude/permission token inside the markers.
    # claude-cli-invocations:start
    local command="claude --permission-mode bypassPermissions --print"
    [ -n "$model" ] && command+=" --model '$model'"
    command+=" \"\$AGENT_LOOP_PROMPT\""
    printf '%s' "$command"
    # claude-cli-invocations:end
}

run_worker() {
    local start_sha="$1" attempt=0 model="$WORKER_MODEL" status log command retry
    while [ "$attempt" -le "$WORKER_RETRIES" ]; do
        attempt=$((attempt + 1))
        log="$AGENT_LOOP_LOG_DIR/worker-attempt-$attempt.log"
        command="$(worker_command "$model")"
        status=0
        run_bounded_hook "worker attempt $attempt" "$command" "$WORKER_TIMEOUT_SECONDS" "$log" || status=$?
        [ "$status" -eq 0 ] && return 0

        if worktree_has_work "$start_sha"; then
            recovery_message "Worker exited $status after changing or committing work."
            return "$status"
        fi

        retry=false
        if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
            [ "$RETRY_ON_TIMEOUT" = true ] && retry=true
        elif grep -Eqi 'capacity|overloaded|rate.?limit|temporarily unavailable|resource exhausted' "$log"; then
            retry=true
            [ -n "$WORKER_FALLBACK_MODEL" ] && model="$WORKER_FALLBACK_MODEL"
        fi
        if [ "$retry" != true ] || [ "$attempt" -gt "$WORKER_RETRIES" ]; then
            recovery_message "Worker exited $status without recoverable retry conditions."
            return "$status"
        fi
        echo -e "${YELLOW}›${NC} Retrying worker after bounded capacity/timeout failure (model: ${model:-default})"
        [ "$RETRY_DELAY_SECONDS" -gt 0 ] && sleep "$RETRY_DELAY_SECONDS"
    done
}

require_clean_committed_tree() {
    local phase="$1" start_sha="$2"
    if [ -n "$(git status --porcelain)" ]; then
        recovery_message "$phase left a dirty worktree."
        return 1
    fi
    if [ "$(git rev-parse HEAD)" = "$start_sha" ]; then
        recovery_message "$phase produced no local commit."
        return 1
    fi
}

run_validation() {
    local label="$1"
    [ -z "$VALIDATION_HOOK" ] && return 0
    run_bounded_hook "$label validation" "$VALIDATION_HOOK" "$HOOK_TIMEOUT_SECONDS" \
        "$AGENT_LOOP_LOG_DIR/${label// /-}-validation.log"
}

# Head attestation compares commit SHAs only, so a validation hook that exits 0
# while writing a tracked file leaves work that no gate can see — and the
# published PR would then be marked ready without it, moments before
# `git worktree remove --force` discards it.
require_clean_tree_after() {
    local label="$1"
    [ -z "$(git status --porcelain)" ] || {
        recovery_message "$label left uncommitted changes; the reviewed head does not contain them."
        return 1
    }
}

inspect_publication_diff() {
    local file_count
    # This function is called in an `||` context, so `set -e` is disabled in its
    # body; check the gate explicitly or its non-zero exit (conflict markers left in
    # a committed file, whitespace errors) is silently ignored and publication
    # proceeds with a corrupt diff.
    if ! git diff --check "origin/$BASE_BRANCH..HEAD"; then
        echo "publication diff contains conflict markers or whitespace errors" >&2
        return 1
    fi
    file_count="$(git diff --name-only "origin/$BASE_BRANCH..HEAD" | wc -l | tr -d ' ')"
    [ "$file_count" -gt 0 ] || { echo "publication diff is empty" >&2; return 1; }
    echo "   Publication diff: $file_count file(s)"
    git diff --stat "origin/$BASE_BRANCH..HEAD" | tail -n "$OUTPUT_MAX_LINES"
}

open_draft_pr() {
    local number="$1" branch="$2" body_file pr_url pr_number
    git push --set-upstream origin "$branch"
    body_file="$AGENT_LOOP_LOG_DIR/pr-body.md"
    {
        echo "## Summary"
        echo
        echo "Implementation is complete. Local Codex and Claude review is running"
        echo "against this draft PR and its inline review-thread ledger."
        echo
        echo "## Test plan"
        echo
        if [ -n "$SETUP_HOOK" ]; then echo "- [x] isolated dependency bootstrap"; fi
        echo "- [ ] bounded local Codex and Claude review convergence"
        echo "- [ ] every local-review thread replied to and resolved"
        echo "- [x] fresh-base integration and publication-diff inspection"
        if [ -n "$VALIDATION_HOOK" ]; then echo "- [x] configured local validation hook"; fi
        echo
        echo "Closes #$number"
    } > "$body_file"
    if ! pr_url="$(gh pr create --draft --base "$BASE_BRANCH" --head "$branch" \
        --title "agent-loop: resolve #$number" --body-file "$body_file")"; then
        recovery_message "Pushed origin/$branch, but 'gh pr create' failed. Open the PR for that branch manually, or delete it with 'git push origin --delete $branch', before re-running. The local branch $branch is retained."
        exit 1
    fi
    # The PR exists from here on, so every later failure must name it — a bare
    # `set -e` abort would leave an open draft PR nobody was told about.
    pr_number="$(gh pr view "$pr_url" --json number --jq .number)" || {
        recovery_message "Opened $pr_url but could not read its number. Review or close that PR before re-running."
        exit 1
    }
    export AGENT_LOOP_PR_NUMBER="$pr_number"
    export AGENT_LOOP_PR_URL="$pr_url"
    export AGENT_LOOP_PR_HEAD_SHA
    AGENT_LOOP_PR_HEAD_SHA="$(git rev-parse HEAD)"
    echo -e "${GREEN}✓${NC} Opened draft review ledger $pr_url"
}

attest_pr_boundary() {
    local expected_head="$1" expected_base="$2" remote_sha pr_data
    local pr_head pr_head_ref pr_base_ref pr_base_oid pr_state pr_draft
    remote_sha="$(git ls-remote origin "refs/heads/$AGENT_LOOP_BRANCH" | awk 'NR == 1 {print $1}')"
    pr_data="$(gh pr view "$AGENT_LOOP_PR_NUMBER" \
        --json headRefOid,headRefName,baseRefName,baseRefOid,state,isDraft \
        --jq '[.headRefOid,.headRefName,.baseRefName,.baseRefOid,.state,.isDraft] | @tsv')" ||
        return 1
    IFS=$'\t' read -r pr_head pr_head_ref pr_base_ref pr_base_oid pr_state pr_draft \
        <<< "$pr_data"
    if ! { [ "$expected_head" = "$(git rev-parse HEAD)" ] &&
        [ "$expected_head" = "$remote_sha" ] &&
        [ "$expected_head" = "$pr_head" ] &&
        [ "$AGENT_LOOP_BRANCH" = "$pr_head_ref" ] &&
        [ "$BASE_BRANCH" = "$pr_base_ref" ] &&
        [ "$pr_state" = OPEN ] &&
        [ "$pr_draft" = true ]; }; then
        return 1
    fi
    [ "$expected_base" = "$pr_base_oid" ] || return 2
}

# Every attestation the wrapper trusts must have been authored by the actor
# running the loop, so all three comment surfaces are filtered by login before
# the marker is matched. A `gh api` failure has to fail the lookup rather than
# yield an empty body set that reads as "marker absent" for the wrong reason —
# `pipefail` plus the explicit `|| return 1` below is what enforces that.
collect_reviewer_comment_bodies() {
    local out_file="$1" endpoint
    {
        for endpoint in "issues/$AGENT_LOOP_PR_NUMBER/comments" \
                        "pulls/$AGENT_LOOP_PR_NUMBER/comments" \
                        "pulls/$AGENT_LOOP_PR_NUMBER/reviews"; do
            gh api "repos/{owner}/{repo}/$endpoint" --paginate |
                jq -r --arg reviewer "$CURRENT_LOGIN" \
                    '.[] | select(.user.login == $reviewer) | .body // empty' ||
                return 1
        done
    } > "$out_file"
}

verify_attestation_marker() {
    local marker="$1" bodies_file="$2"
    collect_reviewer_comment_bodies "$bodies_file" || return 1
    grep -Fq -- "$marker" "$bodies_file"
}

# A review hook that exits 0 without committing or posting anything is
# indistinguishable from a genuinely clean pass unless the pass itself leaves
# evidence. Require the ledger's clean-pass attestation, bound to this engine,
# this round, and the exact head that was reviewed, so a no-op or silently
# declining hook cannot converge an unreviewed PR.
verify_clean_pass_attestation() {
    local slug="$1" round="$2" sha="$3"
    verify_attestation_marker \
        "<!-- local-review-pass:v1 engine=$slug round=$round head=$sha -->" \
        "$AGENT_LOOP_LOG_DIR/$slug-clean-pass-round-$round.txt"
}

verify_review_completion_attestation() {
    local slug="$1" round="$2" before="$3" after="$4"
    verify_attestation_marker \
        "<!-- local-review-complete:v1 engine=$slug round=$round before=$before head=$after -->" \
        "$AGENT_LOOP_LOG_DIR/$slug-review-complete-round-$round.txt"
}

fetch_local_review_threads() {
    local owner name ledger_file query
    owner="$(gh repo view --json owner --jq .owner.login)"
    name="$(gh repo view --json name --jq .name)"
    ledger_file="$AGENT_LOOP_LOG_DIR/local-review-threads.json"
    query='
query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$endCursor) {
        nodes {
          isResolved
          comments(first:100) {
            nodes { body databaseId author { login } }
            pageInfo { hasNextPage }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}'
    # This function is only ever called on the left of `||`, so `set -e` is
    # disabled in its body: check the fetch explicitly or a partial page set that
    # happens to stay parseable is verified as a complete ledger.
    gh api graphql --paginate --slurp -f query="$query" -f owner="$owner" \
        -f name="$name" -F number="$AGENT_LOOP_PR_NUMBER" > "$ledger_file" || return 1
    printf '%s\n' "$ledger_file"
}

verify_committed_pass_evidence() {
    local slug="$1" round="$2" after="$3" ledger_file
    ledger_file="$(fetch_local_review_threads)" || return 1
    jq -e --arg reviewer "$CURRENT_LOGIN" --arg engine "$slug" \
        --argjson round "$round" --arg after "$after" '
      def finding:
        capture("<!-- local-review:v1 engine=(?<engine>codex|claude) round=(?<round>[0-9]+) head=(?<head>[0-9a-f]{40}) fingerprint=(?<fingerprint>[A-Za-z0-9._:/-]+) -->");
      def disposition:
        capture("<!-- local-review-disposition:v1 engine=(?<engine>codex|claude) round=(?<round>[0-9]+) head=(?<head>[0-9a-f]{40}) fingerprint=(?<fingerprint>[A-Za-z0-9._:/-]+) outcome=(?<outcome>fixed|dismissed|deferred) -->");
      all(.[]; (.errors // []) | length == 0)
      and ([.[].data.repository.pullRequest.reviewThreads.nodes[]] as $threads
        | ($threads | all(.comments.pageInfo.hasNextPage | not))
        and any($threads[];
          . as $thread
          | $thread.isResolved
          and any($thread.comments.nodes | to_entries[];
            select(.value.author.login == $reviewer)
            | (try (.value.body | finding) catch null) as $finding
            | select(
                $finding != null
                and $finding.engine == $engine
                and ($finding.round | tonumber) == $round
              )
            | .key as $finding_index
            | any($thread.comments.nodes | to_entries[];
                (.key > $finding_index)
                and (.value.author.login == $reviewer)
                and ((try (.value.body | disposition) catch null) as $reply
                  | $reply != null
                  and $reply.engine == $engine
                  and ($reply.round | tonumber) == $round
                  and $reply.head == $after
                  and $reply.fingerprint == $finding.fingerprint
                  and $reply.outcome == "fixed")
              )
          )))
    ' "$ledger_file" >/dev/null
}

verify_local_review_threads() {
    local ledger_file
    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        python3 "$REVIEW_LEDGER" verify-ledger --repo "$GH_REPO" \
            --pr "$AGENT_LOOP_PR_NUMBER" --head "$(git rev-parse HEAD)" >/dev/null
        return
    fi
    ledger_file="$(fetch_local_review_threads)" || return 1
    # The comment-pagination guard runs before the marker filter on purpose: a
    # thread whose marker sits past the first comment page has no marker in
    # `comments.nodes`, so filtering first would drop exactly the threads whose
    # state cannot be established. Resolution is then checked per marker — a
    # marker with no later comment in its thread is an unanswered finding, which
    # a thread-length test cannot see on a reused thread.
    jq -e --arg marker '<!-- local-review:v1 ' --arg reviewer "$CURRENT_LOGIN" '
      def markers: [.comments.nodes | to_entries[]
        | select(
            (.value.author.login == $reviewer)
            and ((.value.body // "") | contains($marker))
          ) | .key];
      def has_reviewer_reply_after_latest_marker:
        (markers | max) as $latest
        | any(.comments.nodes | to_entries[];
            (.key > $latest) and (.value.author.login == $reviewer));
      all(.[]; (.errors // []) | length == 0)
      and ([.[].data.repository.pullRequest.reviewThreads.nodes[]] as $threads
        | ($threads | all(.comments.pageInfo.hasNextPage | not))
        and ($threads
          | map(select(markers | length > 0))
          | all(.isResolved and has_reviewer_reply_after_latest_marker)))
    ' "$ledger_file" >/dev/null
}

# Called on the left of `||`, so `set -e` is disabled throughout this body: every
# command whose failure must stop the run is checked explicitly. An unchecked
# `git fetch` here would pin a stale base SHA and hand both engines a base the
# reviewers believe is current.
run_review_convergence() {
    local round=1 engine slug hook before after material outcome_file classification round_base_sha
    local base_advanced boundary_status result_file result_json result_status blocker attest_json
    while [ "$round" -le "$REVIEW_MAX_ROUNDS" ]; do
        echo -e "${CYAN}↻${NC} Local review convergence round $round/$REVIEW_MAX_ROUNDS"
        export AGENT_LOOP_REVIEW_ROUND="$round"
        material=false
        base_advanced=false
        git fetch origin "$BASE_BRANCH" --quiet || {
            recovery_message "Could not fetch origin/$BASE_BRANCH before review round $round."
            return 1
        }
        round_base_sha="$(git rev-parse "origin/$BASE_BRANCH")" || {
            recovery_message "Could not resolve origin/$BASE_BRANCH before review round $round."
            return 1
        }
        export AGENT_LOOP_REVIEW_BASE="$round_base_sha"
        export AGENT_LOOP_REVIEW_BASE_SHA="$round_base_sha"
        if ! git merge-base --is-ancestor "$round_base_sha" HEAD; then
            echo -e "${BLUE}▸${NC} Integrating fresh base before review round $round"
            if ! git merge --no-edit "$round_base_sha"; then
                git merge --abort >/dev/null 2>&1 || true
                recovery_message "Fresh-base merge conflicted before review round $round."
                return 1
            fi
            inspect_publication_diff || return 1
            run_validation "fresh-base-round-$round" || return 1
            require_clean_tree_after "fresh-base-round-$round validation" || return 1
            git push origin "HEAD:refs/heads/$AGENT_LOOP_BRANCH" || return 1
            # Deliberately not material: both engines review this merged head
            # below, so the round is already a complete pass over the final tree.
            # The post-review base check at the bottom of the loop is what forces
            # a restart, because that is the case the reviewers did not see.
        fi
        for engine in Codex Claude; do
            if [ "$engine" = Codex ]; then
                slug=codex
                hook="$CODEX_REVIEW_HOOK"
            else
                slug=claude
                hook="$CLAUDE_REVIEW_HOOK"
            fi
            export AGENT_LOOP_REVIEW_ENGINE="$slug"
            outcome_file="$AGENT_LOOP_LOG_DIR/$slug-review-round-$round.outcome"
            result_file="$AGENT_LOOP_LOG_DIR/$slug-review-round-$round.result.json"
            rm -f -- "$outcome_file"
            rm -f -- "$result_file"
            if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
                unset AGENT_LOOP_REVIEW_OUTCOME_FILE
                export AGENT_LOOP_REVIEW_RESULT_FILE="$result_file"
            else
                unset AGENT_LOOP_REVIEW_RESULT_FILE
                export AGENT_LOOP_REVIEW_OUTCOME_FILE="$outcome_file"
            fi
            before="$(git rev-parse HEAD)"
            export AGENT_LOOP_PR_HEAD_SHA="$before"
            boundary_status=0
            attest_pr_boundary "$before" "$round_base_sha" || boundary_status=$?
            if [ "$boundary_status" -eq 2 ]; then
                material=true
                base_advanced=true
                echo "   PR base advanced before $engine; the next round pins and integrates it."
                break
            elif [ "$boundary_status" -ne 0 ]; then
                recovery_message "The local, remote, or draft PR review boundary diverged before $engine review round $round."
                return 1
            fi
            run_bounded_hook "local $engine PR review round $round" "$hook" \
                "$HOOK_TIMEOUT_SECONDS" "$AGENT_LOOP_LOG_DIR/$slug-review-round-$round.log" || {
                recovery_message "$engine review hook failed in round $round."
                return 1
            }
            [ -z "$(git status --porcelain)" ] || {
                recovery_message "$engine review left uncommitted changes in round $round."
                return 1
            }
            after="$(git rev-parse HEAD)"
            git merge-base --is-ancestor "$before" "$after" || {
                recovery_message "$engine review rewrote history in round $round."
                return 1
            }
            boundary_status=0
            attest_pr_boundary "$after" "$round_base_sha" || boundary_status=$?
            if [ "$boundary_status" -eq 2 ]; then
                material=true
                base_advanced=true
                echo "   PR base advanced during $engine; this round cannot converge."
            elif [ "$boundary_status" -ne 0 ]; then
                recovery_message "$engine review must preserve the open draft PR boundary and push its normal commit to that exact head."
                return 1
            fi
            run_validation "$slug-review-round-$round" || return 1
            require_clean_tree_after "$slug review round $round validation" || return 1
            if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
                result_json="$(python3 "$REVIEW_LEDGER" validate-result \
                    --engine "$slug" --round "$round" --base "$round_base_sha" \
                    --before "$before" --head "$after" --result-file "$result_file")" || {
                    recovery_message "$engine review did not produce a valid contract v3 result in round $round."
                    return 1
                }
                result_status="$(jq -r '.status' <<<"$result_json")"
                if [ "$result_status" = blocked ]; then
                    blocker="$(jq -r '.blocker' <<<"$result_json")"
                    recovery_message "$engine review blocked in round $round: $blocker"
                    return 1
                fi
                if [ "$base_advanced" = true ]; then
                    echo "   Valid review result is superseded by the advanced base; the next round revalidates it."
                else
                    attest_json="$(python3 "$REVIEW_LEDGER" attest --repo "$GH_REPO" \
                        --pr "$AGENT_LOOP_PR_NUMBER" --head "$after" --engine "$slug" \
                        --round "$round" --base "$round_base_sha" --before "$before" \
                        --result-file "$result_file")" || {
                        recovery_message "$engine review result attestation failed in round $round."
                        return 1
                    }
                    classification="$(jq -r 'if .status == "clean" then "clean" else .classification end' <<<"$attest_json")"
                    if [ "$classification" != clean ]; then
                        [ "$classification" = minor ] || material=true
                    fi
                fi
            elif [ "$base_advanced" = true ]; then
                echo "   Review evidence is superseded by the advanced base; the next round revalidates it."
            elif [ "$after" != "$before" ]; then
                classification=material
                if [ -e "$outcome_file" ]; then
                    [ -f "$outcome_file" ] && [ ! -L "$outcome_file" ] || {
                        recovery_message "$engine review outcome is not a regular file."
                        return 1
                    }
                    classification="$(tr -d '[:space:]' < "$outcome_file")"
                    case "$classification" in
                        material|minor) ;;
                        *)
                            recovery_message "$engine review outcome must be exactly material or minor."
                            return 1
                            ;;
                    esac
                fi
                verify_review_completion_attestation "$slug" "$round" "$before" "$after" || {
                    recovery_message "$engine review round $round committed but posted no final-lane completion attestation for head $after."
                    return 1
                }
                verify_committed_pass_evidence "$slug" "$round" "$after" || {
                    recovery_message "$engine review round $round committed without a resolved same-round finding and structured fix disposition for head $after."
                    return 1
                }
                [ "$classification" = minor ] || material=true
            else
                [ ! -e "$outcome_file" ] || {
                    recovery_message "$engine review wrote an outcome without a fix commit."
                    return 1
                }
                verify_clean_pass_attestation "$slug" "$round" "$after" || {
                    recovery_message "$engine review round $round committed nothing and posted no clean-pass attestation for head $after; the pass is unverified."
                    return 1
                }
            fi
            [ "$base_advanced" = false ] || break
        done
        # A failed fetch here would leave `origin/$BASE_BRANCH` at the SHA the
        # round already pinned, so the ancestry test below would pass and the
        # round could converge without ever seeing a base commit that landed.
        git fetch origin "$BASE_BRANCH" --quiet || {
            recovery_message "Could not re-fetch origin/$BASE_BRANCH after review round $round."
            return 1
        }
        if ! git merge-base --is-ancestor "origin/$BASE_BRANCH" HEAD; then
            material=true
            echo "   Base advanced during the round; the next round integrates it before Codex."
        fi
        if [ "$material" = false ]; then
            verify_local_review_threads || {
                recovery_message "Local-review threads need a reply and explicit resolution."
                return 1
            }
            REVIEW_ROUNDS_USED="$round"
            unset AGENT_LOOP_REVIEW_OUTCOME_FILE AGENT_LOOP_REVIEW_RESULT_FILE
            return 0
        fi
        echo "   Material fixes landed; the next round rereads the complete PR ledger."
        round=$((round + 1))
    done
    recovery_message "Local review did not converge within $REVIEW_MAX_ROUNDS round(s); draft PR preserved."
    unset AGENT_LOOP_REVIEW_OUTCOME_FILE AGENT_LOOP_REVIEW_RESULT_FILE
    return 1
}

finalize_pr() {
    local body_file="$AGENT_LOOP_LOG_DIR/pr-body-final.md" boundary_status=0
    verify_local_review_threads || {
        recovery_message "Local-review threads lost their reply or resolution before the PR could be marked ready."
        return 1
    }
    # Convergence proved base *ancestry*, not that the base tip still equals the
    # pinned SHA, and the final publication-diff inspection and reviewed-head
    # validation run between the two checks. A base commit landing in that window
    # is the ordinary way this fails, so name it: without these messages the
    # terminal step of a converged run aborts through the `on_exit` backstop with
    # nothing but an exit code, and the natural operator response is to mark the
    # PR ready by hand — the exact boundary this gate protects.
    attest_pr_boundary "$(git rev-parse HEAD)" "$AGENT_LOOP_REVIEW_BASE" || boundary_status=$?
    if [ "$boundary_status" -eq 2 ]; then
        recovery_message "The PR base advanced after review converged; re-run so a fresh round pins and reviews the new base. The draft PR is preserved."
        return 1
    elif [ "$boundary_status" -ne 0 ]; then
        recovery_message "The draft PR boundary diverged after review converged; the PR was not marked ready."
        return 1
    fi
    {
        echo "## Summary"
        echo
        echo "Local Codex and Claude review converged after $REVIEW_ROUNDS_USED round(s)."
        echo
        echo "## Test plan"
        echo
        if [ -n "$SETUP_HOOK" ]; then echo "- [x] isolated dependency bootstrap"; fi
        echo "- [x] bounded local Codex and Claude review convergence"
        echo "- [x] every local-review thread replied to and resolved"
        echo "- [x] fresh-base integration and publication-diff inspection"
        if [ -n "$VALIDATION_HOOK" ]; then echo "- [x] configured local validation hook"; fi
        echo
        echo "Closes #$AGENT_LOOP_ISSUE_ID"
    } > "$body_file"
    # The body rewrite is reporting, not a gate: every claim in it was already
    # proven by the checks above. `gh pr edit` is the flakiest call in this
    # function (it has aborted repo-wide on unrelated Projects-classic API
    # deprecations), and letting it abort here would strand a fully converged PR
    # in draft — the state an operator is most likely to "fix" with a manual
    # `gh pr ready` that skips the boundary gate. Warn and continue instead.
    gh pr edit "$AGENT_LOOP_PR_NUMBER" --body-file "$body_file" ||
        echo -e "${YELLOW}!${NC} Could not update the PR body; the draft body still describes review as in progress." >&2
    gh pr ready "$AGENT_LOOP_PR_NUMBER" || {
        recovery_message "Review converged but 'gh pr ready' failed; mark $AGENT_LOOP_PR_URL ready manually."
        return 1
    }
    echo -e "${GREEN}✓${NC} Review converged; PR ready: $AGENT_LOOP_PR_URL"
}

echo -e "${CYAN}→${NC} agent-loop repository: $PROJECT_DIR"
echo "   Base: origin/$BASE_BRANCH"
if [ -n "$ISSUE_ALLOWLIST" ]; then
    echo "   Selection: allowlist $ISSUE_ALLOWLIST"
else
    echo "   Selection: ready queue"
fi
echo "   Dependency gate: $DEPENDENCY_GATE"
echo "   Dry run: $DRY_RUN"
echo "   Hooks:"
echo "     setup: ${SETUP_HOOK:-<none>}"
echo "     validation: ${VALIDATION_HOOK:-<none>}"
echo "     Claude review: $CLAUDE_REVIEW_HOOK"
echo "     Codex review: $CODEX_REVIEW_HOOK"

ITERATION=0
while [ "$ITERATION" -lt "$MAX_ITERATIONS" ]; do
    select_status=0
    select_next_issue || select_status=$?
    if [ "$select_status" -eq 2 ]; then
        echo "issue selection failed" >&2
        exit 1
    fi
    [ "$select_status" -eq 0 ] || break

    PROCESSED_ISSUES+=("$SELECTED_ID")
    ITERATION=$((ITERATION + 1))
    branch="$BRANCH_PREFIX/issue-$SELECTED_ID-$RUN_TAG"
    safe_repo="${REPO_NAME//[^A-Za-z0-9._-]/-}"
    ACTIVE_WORKTREE="$WORKTREE_ROOT/$safe_repo-issue-$SELECTED_ID-$RUN_TAG"
    proposed_log_dir="$LOG_ROOT/$safe_repo-issue-$SELECTED_ID-$RUN_TAG"

    echo -e "${CYAN}▶${NC} Issue #$SELECTED_ID ($ITERATION/$MAX_ITERATIONS)"
    if ! check_dependencies "$SELECTED_BODY"; then
        echo -e "${YELLOW}○${NC} Issue #$SELECTED_ID blocked by dependency gate"
        ACTIVE_WORKTREE=""
        continue
    fi
    echo "   Worktree: $ACTIVE_WORKTREE"
    echo "   Branch: $branch"
    echo "   Setup hook: ${SETUP_HOOK:-<none>}"
    echo "   Review order: draft PR -> Codex -> Claude, capped at $REVIEW_MAX_ROUNDS rounds"
    echo "   Publication: draft PR on $branch; ready only after thread convergence"

    if [ "$DRY_RUN" = true ]; then
        echo -e "${GREEN}✓${NC} Dry-run only: no claim, worktree, hook, push, or PR mutation"
        ACTIVE_WORKTREE=""
        continue
    fi

    mkdir -p "$WORKTREE_ROOT"
    if [ -L "$LOG_ROOT" ]; then
        echo "Log root must not be a symlink: $LOG_ROOT" >&2
        ACTIVE_WORKTREE=""
        exit 1
    fi
    mkdir -p "$LOG_ROOT"
    chmod 700 "$LOG_ROOT"
    if ! mkdir -m 700 "$proposed_log_dir"; then
        echo "Could not exclusively create private log directory: $proposed_log_dir" >&2
        ACTIVE_WORKTREE=""
        exit 1
    fi

    claim_issue "$SELECTED_ID" || {
        echo -e "${YELLOW}○${NC} Issue #$SELECTED_ID could not be claimed; skipping"
        rmdir "$proposed_log_dir" 2>/dev/null || true
        ACTIVE_WORKTREE=""
        continue
    }
    AGENT_LOOP_LOG_DIR="$proposed_log_dir"
    # Never let the issue branch inherit origin/<base> as its upstream. With
    # push.default=upstream, a bare `git push` from a worker/reviewer would
    # otherwise target the integration branch and bypass local review.
    git worktree add --no-track -b "$branch" "$ACTIVE_WORKTREE" "origin/$BASE_BRANCH"
    cd "$ACTIVE_WORKTREE"

    export AGENT_LOOP_ISSUE_ID="$SELECTED_ID"
    export AGENT_LOOP_BASE_BRANCH="$BASE_BRANCH"
    export AGENT_LOOP_BRANCH="$branch"
    export AGENT_LOOP_WORKTREE="$ACTIVE_WORKTREE"
    export AGENT_LOOP_LOG_DIR
    export AGENT_LOOP_PROMPT="${PROMPT_TEMPLATE//\{ISSUE_ID\}/$SELECTED_ID}"

    start_sha="$(git rev-parse HEAD)"
    if [ -n "$SETUP_HOOK" ]; then
        run_bounded_hook "isolated dependency bootstrap" "$SETUP_HOOK" "$HOOK_TIMEOUT_SECONDS" "$AGENT_LOOP_LOG_DIR/setup.log" || {
            recovery_message "Setup hook failed."
            exit 1
        }
        [ -z "$(git status --porcelain)" ] || { recovery_message "Setup hook dirtied tracked files."; exit 1; }
    fi

    run_worker "$start_sha" || exit 1
    require_clean_committed_tree "Worker" "$start_sha" || exit 1
    run_validation "worker" || { recovery_message "Worker validation failed."; exit 1; }

    echo -e "${BLUE}▸${NC} Fresh-base integration"
    git fetch origin "$BASE_BRANCH" --quiet
    if ! git merge --no-edit "origin/$BASE_BRANCH"; then
        git merge --abort >/dev/null 2>&1 || true
        recovery_message "Fresh-base merge conflicted; original commits were preserved."
        exit 1
    fi
    inspect_publication_diff || { recovery_message "Fresh-base publication diff inspection failed."; exit 1; }
    run_validation "fresh-base" || { recovery_message "Fresh-base validation failed."; exit 1; }
    [ -z "$(git status --porcelain)" ] || { recovery_message "Fresh-base validation dirtied the worktree."; exit 1; }

    # `git ls-remote --exit-code` returns 0 (ref present), 2 (absent), or 128
    # (transport/auth error). Treat only 2 as "safe to publish"; a 128 must not
    # fail open — that would skip the "did a worker/hook already push?" guard on
    # exactly the transient failures where it matters.
    ls_remote_rc=0
    git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1 || ls_remote_rc=$?
    case "$ls_remote_rc" in
        0)
            recovery_message "Remote branch existed before wrapper publication; a worker or hook may have pushed."
            exit 1
            ;;
        2) : ;; # branch absent — expected
        *)
            recovery_message "Could not verify remote branch state (git ls-remote exit $ls_remote_rc); refusing to publish."
            exit 1
            ;;
    esac
    open_draft_pr "$SELECTED_ID" "$branch"
    REVIEW_ROUNDS_USED=0
    # Both of these already emit their own recovery message before failing, so
    # they exit explicitly rather than relying on `set -e` — every other step in
    # this loop is guarded the same way, and an unguarded call reads as a step
    # whose failure is tolerated.
    run_review_convergence || exit 1
    inspect_publication_diff || { recovery_message "Final reviewed diff inspection failed."; exit 1; }
    run_validation "final-reviewed-head" || { recovery_message "Final reviewed-head validation failed."; exit 1; }
    require_clean_tree_after "final-reviewed-head validation" || exit 1
    finalize_pr || exit 1

    # Publication already succeeded and the branch is retained locally and on
    # origin, so cleanup can no longer lose work. Clear ACTIVE_WORKTREE before any
    # further step (the cd or the removal) so the recovery trap can't
    # misattribute a later failure to this published issue, then force removal and
    # tolerate its failure: a worker or setup hook may leave a non-ignored
    # untracked file (generated artifact, scratch file), which makes a plain
    # `git worktree remove` exit non-zero — and under `set -e` that would abort
    # the whole batch after a successful PR, fire the trap with guidance that
    # contradicts reality, and drop the remaining issues.
    worktree_path="$ACTIVE_WORKTREE"
    ACTIVE_WORKTREE=""
    cd "$PROJECT_DIR"
    if ! git worktree remove --force "$worktree_path"; then
        echo -e "${YELLOW}!${NC} Published; could not remove worktree — remove it manually: $worktree_path" >&2
    fi
    echo -e "${GREEN}✓${NC} Issue #$SELECTED_ID complete; local branch retained at $branch"
done

if [ "$ITERATION" -eq 0 ]; then
    echo -e "${DIM}○${NC} No selectable issues."
fi
echo -e "${GREEN}■${NC} agent-loop finished after $ITERATION issue(s)"
