#!/usr/bin/env bash
# Deterministic, per-issue agent loop with PR-first local review convergence.

set -euo pipefail
# Worktrees and persistent worker/reviewer logs can contain sensitive source or
# test output. Default every path this wrapper creates to owner-only access.
umask 077

# Exported shell functions and login-shell aliases outrank PATH lookups. Remove
# ambient GitHub command customizations before the wrapper resolves or invokes
# its real binaries; the bounded login shell repeats this before evaluating a
# configured hook because shell startup files can define them again.
unset -f git gh 2>/dev/null || true
unalias git gh 2>/dev/null || true

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

# Review context is owned by this wrapper. Do not leak state from an outer
# agent-loop/deepcritique invocation into setup, worker, or worker validation.
unset AGENT_LOOP_REVIEW_BASE AGENT_LOOP_REVIEW_BASE_SHA AGENT_LOOP_REVIEW_ENGINE \
    AGENT_LOOP_REVIEW_ACTOR \
    AGENT_LOOP_REVIEW_OUTCOME_FILE AGENT_LOOP_REVIEW_RESULT_FILE \
    AGENT_LOOP_REVIEW_ROUND AGENT_LOOP_PR_NUMBER AGENT_LOOP_PR_URL \
    AGENT_LOOP_PR_HEAD_SHA AGENT_LOOP_REVIEW_CONTRACT_VERSION \
    AGENT_LOOP_ORIGIN_FETCH_URLS AGENT_LOOP_ORIGIN_PUSH_URLS \
    AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE \
    AGENT_LOOP_REVIEW_PUSH_STATE_FILE

MAX_ITERATIONS=10
ISSUE_ALLOWLIST=""
INCLUDE_ASSIGNED=false
RESUME_RUN_FILE=""
RESUME_BATCH_FILE=""
BATCH_STATE_FILE=""
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
  --resume-run FILE    Resume review/finalization from a private run-state file.
  --resume-batch FILE  Resume an ordered multi-issue batch from durable state.
  --dry-run            Show selection, gates, paths, hooks, and publication only.
  -h, --help           Show this help.

The legacy numeric first argument remains supported. Collection branches are no
longer supported: every issue receives its own branch, worktree, and pull request.
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
            INCLUDE_ASSIGNED=true
            shift
            ;;
        --resume-run)
            [ "$#" -ge 2 ] || { echo "--resume-run requires a state file" >&2; exit 2; }
            RESUME_RUN_FILE="$2"
            shift 2
            ;;
        --resume-batch)
            [ "$#" -ge 2 ] || { echo "--resume-batch requires a state file" >&2; exit 2; }
            RESUME_BATCH_FILE="$2"
            shift 2
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
            echo "unexpected argument: $1" >&2
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
if [ -n "$RESUME_RUN_FILE" ] && { [ -n "$ISSUE_ALLOWLIST" ] || [ "$DRY_RUN" = true ]; }; then
    echo "--resume-run cannot be combined with --issues or --dry-run" >&2
    exit 2
fi
if [ -n "$RESUME_BATCH_FILE" ] && { [ -n "$RESUME_RUN_FILE" ] || [ -n "$ISSUE_ALLOWLIST" ] || [ "$DRY_RUN" = true ]; }; then
    echo "--resume-batch cannot be combined with --resume-run, --issues, or --dry-run" >&2
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
REVIEW_LEDGER="$PROJECT_DIR/.claude/skills/critique/scripts/review-ledger.js"
RUN_STATE_HELPER="$PROJECT_DIR/.claude/skills/agent-loop/scripts/agent-loop-state.py"
REVIEW_PUSH_HELPER="$PROJECT_DIR/.claude/skills/agent-loop/scripts/review-push.sh"
CONFIG_DOCTOR_HELPER="$PROJECT_DIR/.claude/skills/agent-loop/scripts/config-doctor.py"
HOOK_GIT_GUARD="$SCRIPT_DIR/hook-git-guard"
HOOK_GH_GUARD="$SCRIPT_DIR/hook-gh-guard"

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
CONFIG_DOCTOR=true
CLAUDE_EFFORT_POLICY=""
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
        config_doctor) CONFIG_DOCTOR="$value" ;;
        claude_effort_policy) CLAUDE_EFFORT_POLICY="$value" ;;
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
if [ -n "$RESUME_BATCH_FILE" ] && [ "$REVIEW_CONTRACT_VERSION" != 3 ]; then
    echo "--resume-batch requires review_contract_version = 3" >&2
    exit 1
fi
# Catch retired reviewer names before any issue mutation. Both local engines
# use the canonical deepcritique skill name.
for hook_key in claude_review_hook codex_review_hook; do
    case "$hook_key" in
        claude_review_hook) hook_value="$CLAUDE_REVIEW_HOOK" ;;
        codex_review_hook) hook_value="$CODEX_REVIEW_HOOK" ;;
        *) echo "unhandled hook key in retired-name preflight: $hook_key" >&2; exit 1 ;;
    esac
    invalid_pattern='(^|[^[:alnum:]_-])(deepgrill|pr-grill|grill)([^[:alnum:]_-]|$)'
    if [[ "$hook_value" =~ $invalid_pattern ]]; then
        echo "$hook_key names an incorrect reviewer skill; Codex and Claude use deepcritique" >&2
        exit 1
    fi
    # The Python ledger was retired for the vendored `review-ledger.js` bundle,
    # and sync deletes it. `agent-loop.config` is bootstrapped create-if-missing
    # and never rewritten, so a consumer keeps whatever path it was written
    # with — catch it here, where it is one diagnosable line, rather than an
    # hour of model spend later when the hook's write-result cannot find it.
    if [[ "$hook_value" == *review-ledger.py* ]]; then
        echo "$hook_key invokes the retired review-ledger.py; use: node .claude/skills/critique/scripts/review-ledger.js" >&2
        exit 1
    fi
done

if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
    if [ ! -f "$REVIEW_LEDGER" ] || [ ! -r "$REVIEW_LEDGER" ]; then
        echo "review contract v3 helper is unavailable: $REVIEW_LEDGER" >&2
        exit 1
    fi
    command -v node >/dev/null 2>&1 || {
        echo "review contract v3 requires Node.js to run $REVIEW_LEDGER" >&2
        exit 1
    }
    # Capture status separately: command substitution inside `[ ]` is exempt
    # from `set -e`, so comparing stdout alone reports a crashed or unparsable
    # bundle as a protocol mismatch. Node too old for the bundle's ESM syntax
    # is the reachable case, and it deserves its own message.
    if ! ledger_protocol="$(node "$REVIEW_LEDGER" --protocol-version 2>&1)"; then
        echo "node could not execute $REVIEW_LEDGER: $ledger_protocol" >&2
        exit 1
    fi
    [ "$ledger_protocol" = 3 ] || {
        echo "review-ledger.js reports protocol '$ledger_protocol'; review contract v3 requires 3" >&2
        exit 1
    }
    for hook_key in claude_review_hook codex_review_hook; do
        case "$hook_key" in
            claude_review_hook) hook_value="$CLAUDE_REVIEW_HOOK" ;;
            codex_review_hook) hook_value="$CODEX_REVIEW_HOOK" ;;
            *) echo "unhandled hook key in contract-v3 preflight: $hook_key" >&2; exit 1 ;;
        esac
        [ -n "$hook_value" ] || {
            echo "$hook_key must be configured for review contract v3" >&2
            exit 1
        }
        [[ "$hook_value" == *AGENT_LOOP_REVIEW_PUSH_HELPER* ]] || {
            echo "$hook_key must use AGENT_LOOP_REVIEW_PUSH_HELPER for review contract v3" >&2
            exit 1
        }
        [[ "$hook_value" == *AGENT_LOOP_REVIEW_RESULT_FILE* ]] || {
            echo "$hook_key must write AGENT_LOOP_REVIEW_RESULT_FILE for review contract v3" >&2
            exit 1
        }
        [[ "$hook_value" == *write-result* ]] || {
            echo "$hook_key must use review-ledger.js write-result for review contract v3" >&2
            exit 1
        }
    done
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
BASE_REMOTE_REF="refs/remotes/origin/$BASE_BRANCH"
BASE_FETCH_REFSPEC="+refs/heads/$BASE_BRANCH:$BASE_REMOTE_REF"

for value in "$WORKER_RETRIES" "$WORKER_TIMEOUT_SECONDS" "$HOOK_TIMEOUT_SECONDS" \
             "$REVIEW_MAX_ROUNDS" "$RETRY_DELAY_SECONDS" "$LOG_MAX_KB" "$OUTPUT_MAX_LINES"; do
    [[ "$value" =~ ^[0-9]+$ ]] || { echo "numeric agent-loop config value required: $value" >&2; exit 1; }
done
[ "$WORKER_TIMEOUT_SECONDS" -gt 0 ] || { echo "worker_timeout_seconds must be a positive integer" >&2; exit 1; }
[ "$HOOK_TIMEOUT_SECONDS" -gt 0 ] || { echo "hook_timeout_seconds must be a positive integer" >&2; exit 1; }
[ "$REVIEW_MAX_ROUNDS" -gt 0 ] || { echo "review_max_rounds must be a positive integer" >&2; exit 1; }
case "$RETRY_ON_TIMEOUT" in true|false) ;; *) echo "retry_on_timeout must be true or false" >&2; exit 1 ;; esac
case "$CONFIG_DOCTOR" in true|false) ;; *) echo "config_doctor must be true or false" >&2; exit 1 ;; esac
case "$DEPENDENCY_GATE" in ready|merged-to-base) ;; *) echo "dependency_gate must be ready or merged-to-base" >&2; exit 1 ;; esac

for cmd in git gh jq node python3 timeout flock realpath; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "required command not found: $cmd" >&2; exit 1; }
done
REAL_GIT_BIN="$(type -P git)"
REAL_GH_BIN="$(type -P gh)"
[ -x "$REAL_GIT_BIN" ] || { echo "required Git executable not found" >&2; exit 1; }
[ -x "$REAL_GH_BIN" ] || { echo "required gh executable not found" >&2; exit 1; }
if [ -z "$WORKER_HOOK" ]; then
    DEFAULT_CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
    [ -n "$DEFAULT_CLAUDE_BIN" ] || {
        echo "required command not found for default worker: claude" >&2
        exit 1
    }
fi
[ -x "$HOOK_GIT_GUARD" ] || { echo "hook Git guard not found or not executable: $HOOK_GIT_GUARD" >&2; exit 1; }
[ -x "$HOOK_GH_GUARD" ] || { echo "hook gh guard not found or not executable: $HOOK_GH_GUARD" >&2; exit 1; }
[ -x "$ISSUES_READY" ] || { echo "issues ready.py not found or not executable: $ISSUES_READY" >&2; exit 1; }
[ -x "$RUN_STATE_HELPER" ] || { echo "agent-loop run-state helper not found or not executable: $RUN_STATE_HELPER" >&2; exit 1; }
[ -x "$REVIEW_PUSH_HELPER" ] || { echo "agent-loop review push helper not found or not executable: $REVIEW_PUSH_HELPER" >&2; exit 1; }
[ -x "$CONFIG_DOCTOR_HELPER" ] || { echo "agent-loop config doctor not found or not executable: $CONFIG_DOCTOR_HELPER" >&2; exit 1; }
[ -f "$INSTRUCTIONS_FILE" ] || { echo "agent-loop-instructions.md not found at repository root" >&2; exit 1; }
[ -n "$VALIDATION_HOOK" ] || { echo "validation_hook must be configured before running agent-loop" >&2; exit 1; }
[ -n "$CLAUDE_REVIEW_HOOK" ] || { echo "claude_review_hook must be configured before running agent-loop" >&2; exit 1; }
[ -n "$CODEX_REVIEW_HOOK" ] || { echo "codex_review_hook must be configured before running agent-loop" >&2; exit 1; }

if [ "$CONFIG_DOCTOR" = true ]; then
    doctor_command=(python3 "$CONFIG_DOCTOR_HELPER" --project-dir "$PROJECT_DIR")
    if [ -n "$CLAUDE_EFFORT_POLICY" ]; then
        doctor_command+=(--claude-effort "$CLAUDE_EFFORT_POLICY")
    fi
    "${doctor_command[@]}" || exit 1
fi

if [ -s "$PROMPT_FILE" ] && [ -r "$PROMPT_FILE" ]; then
    PROMPT_TEMPLATE="$(<"$PROMPT_FILE")"
else
    PROMPT_TEMPLATE="Read @agent-loop-instructions.md. Implement issue #{ISSUE_ID} using its title in \$AGENT_LOOP_ISSUE_TITLE and description in \$AGENT_LOOP_ISSUE_BODY (GitHub-facing gh commands are disabled inside the loop), validate it, and commit locally. Do not push or open a pull request."
fi
[[ "$PROMPT_TEMPLATE" == *"{ISSUE_ID}"* ]] || { echo "prompt template must contain {ISSUE_ID}: $PROMPT_FILE" >&2; exit 1; }

cd "$PROJECT_DIR"

# Relative path controls are interpreted from the repository root during an
# original run. Pin that interpretation now so persisted absolute state uses
# the same boundary when a later resume starts from another directory.
WORKTREE_ROOT="$(realpath -m -- "$WORKTREE_ROOT")" || exit 1
LOG_ROOT="$(realpath -m -- "$LOG_ROOT")" || exit 1

# Repository-scoped gh commands must resolve from this checkout, never from an
# ambient GH_REPO that could point issue claims and PR creation at another repo.
unset GH_REPO
GH_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" || {
    echo "could not resolve the GitHub repository for $PROJECT_DIR" >&2
    exit 1
}
[[ "$GH_REPO" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] || {
    echo "resolved GitHub repository is invalid: $GH_REPO" >&2
    exit 1
}
export GH_REPO

ORIGIN_FETCH_URLS="$(git remote get-url --all origin)" || {
    echo "could not capture origin fetch identity" >&2
    exit 1
}
ORIGIN_PUSH_URLS="$(git remote get-url --push --all origin)" || {
    echo "could not capture origin push identity" >&2
    exit 1
}

require_origin_identity() {
    local fetch_urls push_urls
    fetch_urls="$(git remote get-url --all origin)" || return 1
    push_urls="$(git remote get-url --push --all origin)" || return 1
    if [ "$fetch_urls" != "$ORIGIN_FETCH_URLS" ] || [ "$push_urls" != "$ORIGIN_PUSH_URLS" ]; then
        echo "origin fetch/push identity changed during agent-loop" >&2
        return 1
    fi
}

fetch_base() {
    require_origin_identity || return 1
    git fetch origin "$BASE_FETCH_REFSPEC" --quiet
}

if [ "$DRY_RUN" = false ]; then
    fetch_base
fi
git rev-parse --verify --quiet "$BASE_REMOTE_REF" >/dev/null || {
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
AGENT_LOOP_RUN_STATE_FILE=""
AGENT_LOOP_RUN_LOCK_FD=""
AGENT_LOOP_BATCH_LOCK_FD="${AGENT_LOOP_BATCH_LOCK_FD:-}"
RESUME_STATE_JSON=""

acquire_run_lock() {
    local log_dir="$1"
    if [ ! -d "$log_dir" ] || [ -L "$log_dir" ]; then
        echo "run log directory is unavailable or unsafe: $log_dir" >&2
        return 1
    fi
    exec {AGENT_LOOP_RUN_LOCK_FD}<"$log_dir" || return 1
    flock -n "$AGENT_LOOP_RUN_LOCK_FD" || {
        echo "another process already owns this agent-loop run" >&2
        return 1
    }
}

acquire_batch_lock() {
    local state_file="$1" lock_file="${1}.lock"
    if [ -L "$lock_file" ]; then
        echo "batch lock is unsafe: $lock_file" >&2
        return 1
    fi
    if [ ! -e "$lock_file" ]; then
        (umask 077; set -o noclobber; : > "$lock_file") 2>/dev/null || true
    fi
    if [ ! -f "$lock_file" ] || [ -L "$lock_file" ]; then
        echo "batch lock is unavailable or unsafe: $lock_file" >&2
        return 1
    fi
    chmod 600 "$lock_file" || return 1
    exec {AGENT_LOOP_BATCH_LOCK_FD}<>"$lock_file" || return 1
    export AGENT_LOOP_BATCH_LOCK_FD
    flock -n "$AGENT_LOOP_BATCH_LOCK_FD" || {
        echo "another process already owns this agent-loop batch: $state_file" >&2
        return 1
    }
}

if [ -n "$RESUME_RUN_FILE" ]; then
    [ "$REVIEW_CONTRACT_VERSION" = 3 ] || {
        echo "--resume-run requires review_contract_version = 3" >&2
        exit 1
    }
    RESUME_RUN_FILE="$(python3 -c '
from pathlib import Path
import sys
path = Path(sys.argv[1])
if path.is_symlink():
    raise SystemExit(2)
print(path.resolve(strict=True))
' "$RESUME_RUN_FILE")" || exit 1
    AGENT_LOOP_RUN_STATE_FILE="$RESUME_RUN_FILE"
    resume_log_dir="$(dirname "$RESUME_RUN_FILE")"
    case "$resume_log_dir" in "$LOG_ROOT"/*) ;; *) echo "run state log directory is outside configured log_root" >&2; exit 1 ;; esac
    acquire_run_lock "$resume_log_dir" || exit 1
    RESUME_STATE_JSON="$(python3 "$RUN_STATE_HELPER" show --file "$RESUME_RUN_FILE")" || exit 1
    [ "$(jq -r '.repo' <<<"$RESUME_STATE_JSON")" = "$GH_REPO" ] || {
        echo "run state repository does not match $GH_REPO" >&2
        exit 1
    }
    [ "$(jq -r '.baseBranch' <<<"$RESUME_STATE_JSON")" = "$BASE_BRANCH" ] || {
        echo "run state base branch does not match $BASE_BRANCH" >&2
        exit 1
    }
    resume_worktree="$(jq -r '.worktree' <<<"$RESUME_STATE_JSON")"
    recorded_log_dir="$(jq -r '.logDir' <<<"$RESUME_STATE_JSON")"
    [ "$recorded_log_dir" = "$resume_log_dir" ] || {
        echo "run state file is outside its recorded log directory" >&2
        exit 1
    }
    case "$resume_worktree" in "$WORKTREE_ROOT"/*) ;; *) echo "run state worktree is outside configured worktree_root" >&2; exit 1 ;; esac
    resume_phase="$(jq -r '.phase' <<<"$RESUME_STATE_JSON")"
    case "$resume_phase" in
        draft-open|reviewing|converged|finalizing|finalized) ;;
        *) echo "run state is not resumable" >&2; exit 1 ;;
    esac
fi

recovery_message() {
    local reason="$1"
    RECOVERY_EMITTED=true
    echo -e "${RED}✗${NC} $reason" >&2
    if [ -n "$ACTIVE_WORKTREE" ] && [ -e "$ACTIVE_WORKTREE/.git" ]; then
        echo "Worktree preserved: $ACTIVE_WORKTREE" >&2
        echo "Inspect with: git -C '$ACTIVE_WORKTREE' status --short --branch" >&2
        echo "Recover commits with: git -C '$ACTIVE_WORKTREE' log --oneline --decorate -10" >&2
        echo "Do not reset, reuse, or remove it until the work is recovered." >&2
    elif [ -n "$ACTIVE_WORKTREE" ]; then
        echo "No worktree exists at $ACTIVE_WORKTREE (creation did not complete)." >&2
        echo "If issue #${SELECTED_ID:-?} was claimed, unassign it before it is re-selected." >&2
    fi
    if [ -n "$AGENT_LOOP_RUN_STATE_FILE" ] && [ -f "$AGENT_LOOP_RUN_STATE_FILE" ]; then
        echo "Resume review with: '$SCRIPT_DIR/agent-loop.sh' --resume-run '$AGENT_LOOP_RUN_STATE_FILE'" >&2
    fi
    if [ -n "$BATCH_STATE_FILE" ] && [ -f "$BATCH_STATE_FILE" ]; then
        echo "Resume batch with: '$SCRIPT_DIR/agent-loop.sh' --resume-batch '$BATCH_STATE_FILE'" >&2
    fi
}

print_batch_bail_command() {
    local issue="$1" expected_status="$2"
    echo "Explicit bail command: python3 '$RUN_STATE_HELPER' batch-update --file '$BATCH_STATE_FILE' --issue '$issue' --expected-status '$expected_status' --status bailed" >&2
}

update_run_state() {
    local phase="$1" round="$2" base_sha="$3" head_sha="$4"
    local codex_result_sha256="${5:-}" claude_result_sha256="${6:-}"
    local review_engine="${7:-}"
    local -a command
    [ -n "$AGENT_LOOP_RUN_STATE_FILE" ] || return 0
    command=(python3 "$RUN_STATE_HELPER" update --file "$AGENT_LOOP_RUN_STATE_FILE"
        --phase "$phase" --round "$round" --base-sha "$base_sha"
        --head-sha "$head_sha")
    if [ -n "$codex_result_sha256" ]; then
        command+=(--codex-result-sha256 "$codex_result_sha256")
    fi
    if [ -n "$claude_result_sha256" ]; then
        command+=(--claude-result-sha256 "$claude_result_sha256")
    fi
    if [ -n "$review_engine" ]; then
        command+=(--review-engine "$review_engine")
    fi
    "${command[@]}" >/dev/null
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

# Command substitution strips trailing newlines. jq appends this non-newline
# sentinel, which is removed after capture so the worker context and later
# publication attestation retain the issue text byte for byte. Text that itself
# ends in the sentinel still round-trips, because the strip removes exactly one
# trailing occurrence and jq always appended one. The one byte that does not
# survive is NUL, which command substitution drops; that predates the sentinel
# and fails closed at the publication compare rather than shipping a corrupted
# capture. One constant feeds the jq producer and the bash consumer, and both
# suffix strips quote the expansion, so a future sentinel value cannot drift
# between the two sites or be reinterpreted as a glob pattern.
readonly ISSUE_CONTEXT_SENTINEL=$'\x1e'

set_selected_issue_context() {
    local json="$1" title body
    # Keep -e. The sentinel guarantees a non-null, non-false last output on
    # every success path, so -e cannot fail one; what it still catches is jq
    # producing no output at all, which happens when the input carries no JSON
    # document and neither error() branch can observe. Without it the capture
    # would silently succeed as an empty title and body.
    title="$(jq -erj --arg sentinel "$ISSUE_CONTEXT_SENTINEL" '
        if ((.title | type) == "string" and (.title | length) > 0)
        then .title else error("invalid issue title") end,
        $sentinel
    ' <<<"$json")" || {
        echo "could not extract a non-empty issue title" >&2
        return 1
    }
    title="${title%"$ISSUE_CONTEXT_SENTINEL"}"
    body="$(jq -erj --arg sentinel "$ISSUE_CONTEXT_SENTINEL" '
        if .body == null then ""
        elif (.body | type) == "string" then .body
        else error("invalid issue body") end,
        $sentinel
    ' <<<"$json")" || {
        echo "could not extract the issue body" >&2
        return 1
    }
    body="${body%"$ISSUE_CONTEXT_SENTINEL"}"
    SELECTED_TITLE="$title"
    SELECTED_BODY="$body"
}

sha256_text() {
    python3 -c '
from hashlib import sha256
import sys
sys.stdout.write(sha256(sys.stdin.buffer.read()).hexdigest())
'
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
    [ "$INCLUDE_ASSIGNED" = true ] && [ "$mine" = true ] && [ "$count" -eq 1 ]
}

ready_queue_numbers() {
    jq -r '
        if type == "array" and all(.[];
            ((.number | type) == "number") and
            (.number > 0) and
            (.number == (.number | floor)))
        then .[].number
        else error("ready queue must be an array of positive integer issue numbers")
        end
    '
}

SELECTED_ID=""
SELECTED_TITLE=""
SELECTED_BODY=""
SELECTED_ASSIGNED=false

select_next_issue() {
    SELECTED_ID=""
    SELECTED_TITLE=""
    SELECTED_BODY=""
    SELECTED_ASSIGNED=false
    local json number
    if [ -n "$ISSUE_ALLOWLIST" ]; then
        local candidates allowlist_ready_json
        # An allowlist is a scope ceiling, not an eligibility bypass. Resolve the
        # same hard excludes, open blockers, and addressed-PR checks as the normal
        # ready queue, while retaining assigned-but-ready rows for --resume.
        allowlist_ready_json="$("$ISSUES_READY" --agent --limit 1000 --json)" || return 2
        ready_queue_numbers <<< "$allowlist_ready_json" >/dev/null || {
            echo "could not validate ready-queue data" >&2
            return 2
        }
        IFS=',' read -r -a candidates <<< "$ISSUE_ALLOWLIST"
        if [ -n "$BATCH_STATE_FILE" ]; then
            local batch_selection_json batch_selection_cursor batch_selection_count
            local batch_selection_status
            batch_selection_json="$(python3 "$RUN_STATE_HELPER" batch-show \
                --file "$BATCH_STATE_FILE")" || return 2
            batch_selection_cursor="$(jq -r '.cursor' <<<"$batch_selection_json")"
            batch_selection_count="$(jq -r '.issues | length' <<<"$batch_selection_json")"
            [ "$batch_selection_cursor" -lt "$batch_selection_count" ] || return 1
            number="$(jq -r --argjson cursor "$batch_selection_cursor" \
                '.issues[$cursor].issue' <<<"$batch_selection_json")"
            batch_selection_status="$(jq -r --argjson cursor "$batch_selection_cursor" \
                '.issues[$cursor].status' <<<"$batch_selection_json")"
            [ "$batch_selection_status" = pending ] || {
                echo "ordered batch cursor issue #$number is not pending" >&2
                return 2
            }
            if ! jq -e --argjson number "$number" 'any(.number == $number)' \
                <<< "$allowlist_ready_json" >/dev/null; then
                echo -e "${DIM}○${NC} Ordered batch cursor issue #$number is not ready." >&2
                print_batch_bail_command "$number" pending
                return 1
            fi
            json="$(issue_json "$number")" || return 2
            if ! issue_is_selectable "$number" "$json"; then
                echo -e "${DIM}○${NC} Ordered batch cursor issue #$number is not open, agent-labeled, or safely assignable." >&2
                print_batch_bail_command "$number" pending
                return 1
            fi
            SELECTED_ID="$number"
            set_selected_issue_context "$json" || return 2
            [ "$(jq '.assignees | length' <<<"$json")" -gt 0 ] && SELECTED_ASSIGNED=true
            return 0
        fi
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
            set_selected_issue_context "$json" || return 2
            [ "$(jq '.assignees | length' <<<"$json")" -gt 0 ] && SELECTED_ASSIGNED=true
            return 0
        done
        return 1
    fi

    local ready_json ready_numbers
    if [ "$INCLUDE_ASSIGNED" = true ]; then
        ready_json="$("$ISSUES_READY" --agent --limit 100 --json)" || return 2
    else
        ready_json="$("$ISSUES_READY" --unassigned --agent --limit 100 --json)" || return 2
    fi
    ready_numbers="$(ready_queue_numbers <<< "$ready_json")" || {
        echo "could not validate ready-queue data" >&2
        return 2
    }
    while IFS= read -r number; do
        [ -n "$number" ] || continue
        already_processed "$number" && continue
        json="$(issue_json "$number")" || return 2
        issue_is_selectable "$number" "$json" || continue
        SELECTED_ID="$number"
        set_selected_issue_context "$json" || return 2
        [ "$(jq '.assignees | length' <<<"$json")" -gt 0 ] && SELECTED_ASSIGNED=true
        return 0
    done <<< "$ready_numbers"
    return 1
}

pr_merged_to_base() {
    local pr="$1" data state base oid merge_status=0
    data="$(gh pr view "$pr" --json state,baseRefName,mergeCommit --jq '[.state,.baseRefName,(.mergeCommit.oid // "")] | @tsv' 2>/dev/null)" || return 2
    IFS=$'\t' read -r state base oid <<< "$data"
    [ "$state" = MERGED ] || return 1
    [ "$base" = "$BASE_BRANCH" ] || return 1
    if [ -z "$oid" ]; then
        echo "merged dependency PR #$pr has no merge commit" >&2
        return 2
    fi
    git merge-base --is-ancestor "$oid" "$BASE_REMOTE_REF" >/dev/null 2>&1 || merge_status=$?
    case "$merge_status" in
        0) return 0 ;;
        1) return 1 ;;
        *)
            echo "could not compare dependency PR #$pr with origin/$BASE_BRANCH" >&2
            return 2
            ;;
    esac
}

issue_dependency_merged() {
    local issue="$1" rows pr dependency_status=0 had_error=false
    rows="$(gh issue view "$issue" --json closedByPullRequestsReferences \
        --jq '.closedByPullRequestsReferences[]? | [.number,.state,.baseRefName,(.mergeCommit.oid // "")] | @tsv' 2>/dev/null)" || return 2
    while IFS=$'\t' read -r pr _; do
        [ -n "$pr" ] || continue
        dependency_status=0
        pr_merged_to_base "$pr" || dependency_status=$?
        case "$dependency_status" in
            0) return 0 ;;
            1) ;;
            *) had_error=true ;;
        esac
    done <<< "$rows"
    [ "$had_error" = false ] || return 2
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
    local body="$1" refs kind number found=false dependency_status
    if [ "$DEPENDENCY_GATE" = ready ]; then
        echo "   Dependency gate: ready-queue semantics"
        return 0
    fi
    refs="$(dependency_refs "$body")" || {
        echo "could not parse issue dependencies" >&2
        return 2
    }
    while IFS=$'\t' read -r kind number; do
        [ -n "$number" ] || continue
        found=true
        dependency_status=0
        if [ "$kind" = pr ]; then
            pr_merged_to_base "$number" || dependency_status=$?
        else
            issue_dependency_merged "$number" || dependency_status=$?
        fi
        case "$dependency_status" in
            0) echo "   Dependency $kind #$number: merged into origin/$BASE_BRANCH" ;;
            1)
                echo "   Dependency $kind #$number: NOT merged into origin/$BASE_BRANCH"
                return 1
                ;;
            *)
                echo "could not verify dependency $kind #$number" >&2
                return 2
                ;;
        esac
    done <<< "$refs"
    [ "$found" = true ] || echo "   Dependency gate: no declared dependencies"
}

ready_queue_contains_issue() {
    local number="$1" excluded_pr="${2:-}" ready_json status=0
    local -a ready_command=("$ISSUES_READY" --agent --limit 1000 --json)
    if [ -n "$excluded_pr" ]; then
        ready_command+=(--exclude-addressed-by-pr "$excluded_pr")
    fi
    ready_json="$("${ready_command[@]}")" || {
        echo "could not refresh ready-queue eligibility for issue #$number" >&2
        return 2
    }
    ready_queue_numbers <<< "$ready_json" >/dev/null || {
        echo "could not validate refreshed ready-queue data for issue #$number" >&2
        return 2
    }
    jq -e --argjson number "$number" 'any(.number == $number)' \
        <<< "$ready_json" >/dev/null || status=$?
    case "$status" in
        0) return 0 ;;
        1) return 1 ;;
        *)
            echo "could not validate refreshed ready-queue data for issue #$number" >&2
            return 2
            ;;
    esac
}

rollback_new_claim() {
    local number="$1"
    if ! gh issue edit "$number" --remove-assignee @me >/dev/null; then
        echo "could not release newly claimed issue #$number; operator action is required" >&2
        return 1
    fi
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

    # Refetch after selection/claim and verify identity, eligibility, and issue
    # context. A concurrent reassignment can otherwise duplicate another user's
    # work, while a concurrent edit can give the worker stale requirements.
    # Resumes need the same fresh check.
    claim_json="$(gh issue view "$number" --json number,title,body,state,labels,assignees)" || {
        if [ "$added" = true ] && ! rollback_new_claim "$number"; then
            return 2
        fi
        echo "could not refresh issue #$number after claiming it" >&2
        return 2
    }
    login="$(jq -r '
        if (.assignees | type) != "array" then error("invalid assignees")
        elif (.assignees | length) == 1 and
             ((.assignees[0].login | type) == "string")
        then .assignees[0].login else "" end
    ' <<< "$claim_json")" || {
        if [ "$added" = true ] && ! rollback_new_claim "$number"; then
            return 2
        fi
        echo "could not validate assignees after claiming issue #$number" >&2
        return 2
    }
    if [ "$login" != "$CURRENT_LOGIN" ] || ! jq -e '
        .state == "OPEN" and
        ((.labels | type) == "array") and
        (.labels | any(.name == "dev: agent"))
    ' <<<"$claim_json" >/dev/null 2>&1; then
        if [ "$added" = true ] && ! rollback_new_claim "$number"; then
            return 2
        fi
        echo "claim race, eligibility change, or verification failure for issue #$number" >&2
        return 1
    fi
    if ! set_selected_issue_context "$claim_json"; then
        if [ "$added" = true ] && ! rollback_new_claim "$number"; then
            return 2
        fi
        echo "could not refresh issue context after claiming issue #$number" >&2
        return 2
    fi
    if [ "$SELECTED_ASSIGNED" = true ]; then
        echo -e "${YELLOW}›${NC} Resuming issue #$number"
    fi
}

verify_issue_for_publication() {
    local number="$1" captured_pr="${2:-}" current_json issue_status=0 readiness_status=0
    current_json="$(issue_json "$number")" || {
        echo "could not refresh issue #$number before publication" >&2
        return 2
    }
    jq -e --argjson number "$number" --arg login "$CURRENT_LOGIN" \
        --arg title "$SELECTED_TITLE" --arg body "$SELECTED_BODY" '
        (.number == $number) and
        (.state == "OPEN") and
        ((.labels | type) == "array") and
        (.labels | any(.name == "dev: agent")) and
        ((.assignees | type) == "array") and
        ((.assignees | length) == 1) and
        (.assignees[0].login == $login) and
        (.title == $title) and
        ((.body // "") == $body)
    ' <<< "$current_json" >/dev/null || issue_status=$?
    case "$issue_status" in
        0) ;;
        1)
            echo "issue #$number changed or is no longer eligible before publication" >&2
            return 1
            ;;
        *)
            echo "could not validate issue #$number before publication" >&2
            return 2
            ;;
    esac

    ready_queue_contains_issue "$number" "$captured_pr" || readiness_status=$?
    if [ "$readiness_status" -eq 0 ] && [ "$DEPENDENCY_GATE" = merged-to-base ]; then
        check_dependencies "$SELECTED_BODY" || readiness_status=$?
    fi
    case "$readiness_status" in
        0) return 0 ;;
        1)
            echo "issue #$number is no longer ready before publication" >&2
            return 1
            ;;
        *)
            echo "could not re-evaluate issue #$number before publication" >&2
            return 2
            ;;
    esac
}

worktree_has_work() {
    local start_sha="$1" status head
    status="$(git status --porcelain)" || {
        echo "could not inspect worktree state after worker failure; preserving it" >&2
        return 0
    }
    head="$(git rev-parse HEAD)" || {
        echo "could not inspect HEAD after worker failure; preserving the worktree" >&2
        return 0
    }
    [ -n "$status" ] || [ "$head" != "$start_sha" ]
}

require_issue_branch_head() {
    local current_branch head_sha branch_sha
    current_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)" || return 1
    [ "$current_branch" = "$AGENT_LOOP_BRANCH" ] || return 1
    head_sha="$(git rev-parse HEAD)" || return 1
    branch_sha="$(git rev-parse "refs/heads/$AGENT_LOOP_BRANCH")" || return 1
    [ "$head_sha" = "$branch_sha" ]
}

run_bounded_hook() {
    local phase="$1" hook_command="$2" timeout_seconds="$3" log_file="$4"
    local allow_review_mutations="${5:-false}"
    local max_bytes=$((LOG_MAX_KB * 1024)) status=0
    local guard_bin="$AGENT_LOOP_LOG_DIR/hook-command-guards"
    echo -e "${BLUE}▸${NC} $phase"
    # Bound the captured log to its trailing LOG_MAX_KB with `tail -c`, NOT with a
    # process-wide `ulimit -f`: that rlimit is inherited by the worker and every hook
    # and would SIGXFSZ-kill (and truncate) any repo file they legitimately write
    # (lockfiles, build artifacts, generated code). `tail` drains all input, so the
    # hook is never signalled; keeping the tail preserves the failing output and any
    # capacity/overload marker the retry logic greps for; PIPESTATUS[0] keeps the
    # hook's real exit status.
    if [ -L "$guard_bin" ] || { [ -e "$guard_bin" ] && [ ! -d "$guard_bin" ]; }; then
        echo "hook command guard path is not a real directory: $guard_bin" >&2
        return 1
    fi
    mkdir -p "$guard_bin" || {
        echo "could not create hook command guard directory" >&2
        return 1
    }
    rm -f -- "$guard_bin/git" "$guard_bin/gh" || {
        echo "could not clear prior hook command guards" >&2
        return 1
    }
    cp "$HOOK_GIT_GUARD" "$guard_bin/git" || {
        echo "could not install hook Git guard" >&2
        return 1
    }
    cp "$HOOK_GH_GUARD" "$guard_bin/gh" || {
        echo "could not install hook gh guard" >&2
        return 1
    }
    chmod 700 "$guard_bin/git" "$guard_bin/gh" || {
        echo "could not secure hook command guards" >&2
        return 1
    }
    for guard in "$guard_bin/git" "$guard_bin/gh"; do
        if [ ! -f "$guard" ] || [ -L "$guard" ] || [ ! -x "$guard" ]; then
            echo "installed hook command guard is not a real executable file: $guard" >&2
            return 1
        fi
    done
    (
        set +e
        if [ -n "$AGENT_LOOP_RUN_LOCK_FD" ]; then
            exec {AGENT_LOOP_RUN_LOCK_FD}<&-
        fi
        if [ -n "$AGENT_LOOP_BATCH_LOCK_FD" ]; then
            exec {AGENT_LOOP_BATCH_LOCK_FD}<&-
            unset AGENT_LOOP_BATCH_LOCK_FD
        fi
        # Setup, worker, and validation hooks remain local-only. Review hooks run
        # after the wrapper opens a draft PR and must be able to post inline
        # comments, push fixes, reply, and resolve; their remote mutations are
        # attested by the wrapper after the hook returns.
        export AGENT_LOOP_REAL_GIT="$REAL_GIT_BIN"
        export AGENT_LOOP_REAL_GH="$REAL_GH_BIN"
        if [ "$allow_review_mutations" = true ]; then
            export AGENT_LOOP_REVIEW_CONTRACT_VERSION="$REVIEW_CONTRACT_VERSION"
            export AGENT_LOOP_ORIGIN_FETCH_URLS="$ORIGIN_FETCH_URLS"
            export AGENT_LOOP_ORIGIN_PUSH_URLS="$ORIGIN_PUSH_URLS"
        else
            unset AGENT_LOOP_REVIEW_CONTRACT_VERSION AGENT_LOOP_ORIGIN_FETCH_URLS \
                AGENT_LOOP_ORIGIN_PUSH_URLS
        fi
        export AGENT_LOOP_HOOK_COMMAND="$hook_command"
        export AGENT_LOOP_HOOK_GUARD_BIN="$guard_bin"
        export AGENT_LOOP_ALLOW_REVIEW_MUTATIONS="$allow_review_mutations"
        # shellcheck disable=SC2016 # expanded by the bounded login shell
        timeout --signal=TERM --kill-after=15 "${timeout_seconds}s" bash -lc \
            'unset -f git gh 2>/dev/null || true; unalias git gh 2>/dev/null || true; export PATH="$AGENT_LOOP_HOOK_GUARD_BIN:$PATH"; eval "$AGENT_LOOP_HOOK_COMMAND"' 2>&1 \
            | tail -c "$max_bytes"
        exit "${PIPESTATUS[0]}"
    ) >"$log_file" 2>&1 || status=$?
    if ! require_origin_identity; then
        echo "hook changed origin fetch/push identity" >>"$log_file"
        status=1
    fi
    if [ "$status" -ne 0 ]; then
        echo -e "${RED}✗${NC} $phase failed (exit $status); bounded tail follows:" >&2
        tail -n "$OUTPUT_MAX_LINES" "$log_file" >&2 || true
    else
        echo -e "${GREEN}✓${NC} $phase"
    fi
    return "$status"
}

worker_command() {
    local model="$1" claude_command model_arg
    if [ -n "$WORKER_HOOK" ]; then
        printf '%s' "$WORKER_HOOK"
        return
    fi
    printf -v claude_command '%q' "$DEFAULT_CLAUDE_BIN"
    # claude-cli-invocations:start
    local rendered_command="$claude_command --permission-mode bypassPermissions --print"
    if [ -n "$model" ]; then
        printf -v model_arg '%q' "$model"
        rendered_command+=" --model $model_arg"
    fi
    rendered_command+=" \"\$AGENT_LOOP_PROMPT\""
    printf '%s' "$rendered_command"
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
    local phase="$1" start_sha="$2" status
    status="$(git status --porcelain)" || {
        recovery_message "Could not inspect Git status after $phase."
        return 1
    }
    if [ -n "$status" ]; then
        recovery_message "$phase left a dirty worktree."
        return 1
    fi
    if ! require_issue_branch_head; then
        recovery_message "$phase moved HEAD away from the issue branch."
        return 1
    fi
    if [ "$(git rev-parse HEAD)" = "$start_sha" ]; then
        recovery_message "$phase produced no local commit."
        return 1
    fi
    if ! git merge-base --is-ancestor "$start_sha" HEAD; then
        recovery_message "$phase rewrote or dropped the starting history."
        return 1
    fi
}

run_validation() {
    local label="$1" before_sha after_sha status
    if ! require_issue_branch_head; then
        echo "$label validation did not start on the issue branch" >&2
        return 1
    fi
    before_sha="$(git rev-parse HEAD)" || return 1
    run_bounded_hook "$label validation" "$VALIDATION_HOOK" "$HOOK_TIMEOUT_SECONDS" \
        "$AGENT_LOOP_LOG_DIR/${label// /-}-validation.log" || return 1
    status="$(git status --porcelain)" || return 1
    after_sha="$(git rev-parse HEAD)" || return 1
    if [ -n "$status" ] || [ "$after_sha" != "$before_sha" ]; then
        echo "$label validation mutated the worktree or HEAD; validation hooks must be non-mutating" >&2
        return 1
    fi
    if ! require_issue_branch_head; then
        echo "$label validation moved HEAD away from the issue branch" >&2
        return 1
    fi
}

classify_review_result() {
    local engine="$1" before_sha="$2" after_sha="$3" outcome_file="$4"
    local classification=""

    if [ "$before_sha" = "$after_sha" ]; then
        if [ -e "$outcome_file" ] || [ -L "$outcome_file" ]; then
            recovery_message "$engine review wrote a fix classification without committing a fix."
            return 1
        fi
        REVIEW_FIX_CLASSIFICATION=clean
        return 0
    fi

    # Backward-compatible fail-safe: an existing hook that commits without using
    # the outcome file is material and therefore restarts at Codex.
    if [ ! -e "$outcome_file" ] && [ ! -L "$outcome_file" ]; then
        REVIEW_FIX_CLASSIFICATION=material
        return 0
    fi
    if [ ! -f "$outcome_file" ] || [ -L "$outcome_file" ] || [ ! -r "$outcome_file" ]; then
        recovery_message "$engine review outcome must be a readable regular file."
        return 1
    fi
    classification="$(python3 -c '
from pathlib import Path
import sys

data = Path(sys.argv[1]).read_bytes()
values = {b"minor": "minor", b"minor\n": "minor",
          b"material": "material", b"material\n": "material"}
if data not in values:
    raise SystemExit(2)
sys.stdout.write(values[data])
' "$outcome_file")" || {
        recovery_message "$engine review outcome must be exactly 'minor' or 'material'."
        return 1
    }
    REVIEW_FIX_CLASSIFICATION="$classification"
}

review_outcome_signature() {
    local outcome_file="$1"
    python3 -c '
from hashlib import sha256
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
try:
    metadata = os.lstat(path)
except FileNotFoundError:
    sys.stdout.write("missing")
    raise SystemExit(0)
if not stat.S_ISREG(metadata.st_mode):
    raise SystemExit(2)
sys.stdout.write("file:" + sha256(path.read_bytes()).hexdigest())
' "$outcome_file"
}

require_review_outcome_signature() {
    local engine="$1" outcome_file="$2" expected_signature="$3" phase="$4"
    local actual_signature
    actual_signature="$(review_outcome_signature "$outcome_file")" || {
        recovery_message "Could not re-read $engine review outcome $phase."
        return 1
    }
    if [ "$actual_signature" != "$expected_signature" ]; then
        recovery_message "$engine review outcome file changed $phase."
        return 1
    fi
}

verify_v3_result_attestation() {
    local engine="$1" slug="$2" outcome_file="$3" expected_signature="$4"
    local before_sha after_sha result_status classification fingerprints result_hash
    local marker bodies_file allowed_heads_file historical_comment_ids_file
    case "$expected_signature" in
        file:*) result_hash="${expected_signature#file:}" ;;
        *) recovery_message "$engine converged review result is missing."; return 1 ;;
    esac
    before_sha="$(jq -r '.beforeSha' "$outcome_file")" || return 1
    after_sha="$(jq -r '.afterSha' "$outcome_file")" || return 1
    node "$REVIEW_LEDGER" validate-result \
        --engine "$slug" --round "$REVIEW_ROUNDS_USED" --base "$REVIEWED_BASE_SHA" \
        --before "$before_sha" --head "$after_sha" \
        --result-file "$outcome_file" >/dev/null || {
        recovery_message "$engine converged review result is no longer schema-valid."
        return 1
    }
    result_status="$(jq -r '.status' "$outcome_file")" || return 1
    allowed_heads_file="$AGENT_LOOP_LOG_DIR/$slug-review-round-$REVIEW_ROUNDS_USED-heads.json"
    historical_comment_ids_file="$AGENT_LOOP_LOG_DIR/$slug-review-round-$REVIEW_ROUNDS_USED-historical-comment-ids.json"
    verify_v3_committed_pass_evidence "$slug" "$REVIEW_ROUNDS_USED" \
        "$before_sha" "$after_sha" "$outcome_file" "$allowed_heads_file" \
        "$REVIEWED_BASE_SHA" "$historical_comment_ids_file" || {
        recovery_message "$engine converged review result no longer has matching ledger evidence."
        return 1
    }
    if [ "$result_status" = clean ]; then
        marker="<!-- local-review-pass:v3 engine=$slug round=$REVIEW_ROUNDS_USED base=$REVIEWED_BASE_SHA head=$after_sha result-sha256=$result_hash -->"
    else
        classification="$(jq -r '.classification' "$outcome_file")" || return 1
        fingerprints="$(jq -r '.findingFingerprints | join(",")' "$outcome_file")" || return 1
        marker="<!-- local-review-complete:v3 engine=$slug round=$REVIEW_ROUNDS_USED base=$REVIEWED_BASE_SHA before=$before_sha head=$after_sha classification=$classification fingerprints=$fingerprints result-sha256=$result_hash -->"
    fi
    bodies_file="$AGENT_LOOP_LOG_DIR/$slug-review-round-$REVIEW_ROUNDS_USED-attestations.txt"
    fetch_review_attestation_bodies "$bodies_file" || return 1
    grep -Fqx -- "$marker" "$bodies_file" || {
        recovery_message "$engine converged review result lacks its exact authenticated PR attestation."
        return 1
    }
}

verify_converged_review_outcomes() {
    if [ -z "${CONVERGED_CODEX_OUTCOME_FILE:-}" ] || \
       [ -z "${CONVERGED_CLAUDE_OUTCOME_FILE:-}" ]; then
        recovery_message "Converged review outcome attestations are missing."
        return 1
    fi
    require_review_outcome_signature Codex "$CONVERGED_CODEX_OUTCOME_FILE" \
        "$CONVERGED_CODEX_OUTCOME_SIGNATURE" "before publication" || return 1
    require_review_outcome_signature Claude "$CONVERGED_CLAUDE_OUTCOME_FILE" \
        "$CONVERGED_CLAUDE_OUTCOME_SIGNATURE" "before publication" || return 1
    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        verify_v3_result_attestation Codex codex "$CONVERGED_CODEX_OUTCOME_FILE" \
            "$CONVERGED_CODEX_OUTCOME_SIGNATURE" || return 1
        verify_v3_result_attestation Claude "claude" "$CONVERGED_CLAUDE_OUTCOME_FILE" \
            "$CONVERGED_CLAUDE_OUTCOME_SIGNATURE" || return 1
    fi
}

recover_v3_review_pass() {
    local engine="$1" slug="$2" round="$3" base_sha="$4"
    local outcome_file outcome_signature classification boundary_status=0
    outcome_file="$AGENT_LOOP_LOG_DIR/$slug-review-round-$round.result.json"
    [ -f "$outcome_file" ] || return 2
    outcome_signature="$(review_outcome_signature "$outcome_file")" || return 1
    case "$outcome_signature" in
        file:*) ;;
        *) recovery_message "$engine interrupted review result is incomplete."; return 1 ;;
    esac
    REVIEW_ROUNDS_USED="$round"
    REVIEWED_BASE_SHA="$base_sha"
    require_review_outcome_signature "$engine" "$outcome_file" \
        "$outcome_signature" "during interrupted-pass recovery" || return 1
    verify_v3_result_attestation "$engine" "$slug" "$outcome_file" \
        "$outcome_signature" || return 1
    classification="$(jq -r 'if .status == "clean" then "clean" else .classification end' \
        "$outcome_file")" || return 1
    run_validation "$slug-review-round-$round" || {
        recovery_message "Validation after recovered $engine review failed in review round $round."
        return 1
    }
    require_review_outcome_signature "$engine" "$outcome_file" \
        "$outcome_signature" "during recovered validation in round $round" || return 1
    attest_review_head "after recovered $engine review validation in round $round" \
        "$base_sha" || boundary_status=$?
    if [ "$boundary_status" -ne 0 ]; then
        recovery_message "PR head attestation failed after recovered $engine review validation in round $round."
        return 1
    fi
    REVIEW_PASS_CLASSIFICATION="$classification"
    REVIEW_PASS_OUTCOME_FILE="$outcome_file"
    REVIEW_PASS_OUTCOME_SIGNATURE="$outcome_signature"
}

run_review_pass() {
    local engine="$1" slug="$2" hook="$3" round="$4"
    local hook_description="$5" hook_failure_description="$6"
    local review_description="$7" validation_description="$8"
    local before_sha after_sha status classification outcome_signature outcome_file
    local result_file result_json result_status result_hash allowed_heads_file blocker
    local pre_pass_threads_file historical_comment_ids_file review_push_state_file
    local historical_comment_ids_signature
    local boundary_status

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
    before_sha="$(git rev-parse HEAD)"
    export AGENT_LOOP_PR_HEAD_SHA="$before_sha"
    boundary_status=0
    attest_review_head "before $engine review round $round" \
        "$AGENT_LOOP_REVIEW_BASE_SHA" || boundary_status=$?
    if [ "$boundary_status" -eq 2 ]; then
        return 2
    elif [ "$boundary_status" -ne 0 ]; then
        recovery_message "PR head attestation failed before $engine review round $round."
        return 1
    fi
    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        pre_pass_threads_file="$(fetch_local_review_threads)" || {
            recovery_message "Could not snapshot review history before $engine round $round."
            return 1
        }
        historical_comment_ids_file="$AGENT_LOOP_LOG_DIR/$slug-review-round-$round-historical-comment-ids.json"
        jq '[.[].data.repository.pullRequest.reviewThreads.nodes[].comments.nodes[].databaseId | select(type == "number")] | unique | sort' \
            "$pre_pass_threads_file" > "$historical_comment_ids_file" || {
            recovery_message "Could not pin review history before $engine round $round."
            return 1
        }
        historical_comment_ids_signature="$(review_outcome_signature "$historical_comment_ids_file")" || {
            recovery_message "Could not seal review history before $engine round $round."
            return 1
        }
        export AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE="$historical_comment_ids_file"
        review_push_state_file="$AGENT_LOOP_LOG_DIR/$slug-review-round-$round-push-state"
        printf '%s\n' "$before_sha" > "$review_push_state_file" || {
            recovery_message "Could not initialize the $engine review push checkpoint."
            return 1
        }
        chmod 600 "$review_push_state_file" || {
            recovery_message "Could not secure the $engine review push checkpoint."
            return 1
        }
        export AGENT_LOOP_REVIEW_PUSH_STATE_FILE="$review_push_state_file"
    else
        unset AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE \
            AGENT_LOOP_REVIEW_PUSH_STATE_FILE
    fi
    run_bounded_hook "$hook_description (round $round)" "$hook" \
        "$HOOK_TIMEOUT_SECONDS" "$AGENT_LOOP_LOG_DIR/$slug-review-round-$round.log" true || {
        recovery_message "$hook_failure_description failed in review round $round."
        return 1
    }
    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        require_review_outcome_signature "$engine pre-pass history" \
            "$historical_comment_ids_file" "$historical_comment_ids_signature" \
            "after review round $round" || return 1
    fi
    status="$(git status --porcelain)" || {
        recovery_message "Could not inspect Git status after the $hook_description in round $round."
        return 1
    }
    [ -z "$status" ] || {
        recovery_message "$review_description left uncommitted findings/fixes in review round $round."
        return 1
    }
    require_issue_branch_head || {
        recovery_message "$review_description moved HEAD away from the issue branch in review round $round."
        return 1
    }
    after_sha="$(git rev-parse HEAD)"
    git merge-base --is-ancestor "$before_sha" "$after_sha" || {
        recovery_message "$review_description rewrote or dropped previously reviewed commits in round $round."
        return 1
    }
    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        [ "$(cat "$review_push_state_file")" = "$after_sha" ] || {
            recovery_message "$engine review push checkpoint did not match its final head in round $round."
            return 1
        }
    fi
    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        result_json="$(node "$REVIEW_LEDGER" validate-result \
            --engine "$slug" --round "$round" --base "$AGENT_LOOP_REVIEW_BASE_SHA" \
            --before "$before_sha" --head "$after_sha" --result-file "$result_file")" || {
            recovery_message "$engine review did not produce a valid contract v3 result in round $round."
            return 1
        }
        result_status="$(jq -r '.status' <<<"$result_json")"
        result_hash="$(jq -r '.resultSha256' <<<"$result_json")"
        if [ "$result_status" = blocked ]; then
            blocker="$(jq -r '.blocker' <<<"$result_json")"
            recovery_message "$engine review blocked in round $round: $blocker"
            return 1
        fi
    fi
    boundary_status=0
    attest_review_head "after $engine review round $round" \
        "$AGENT_LOOP_REVIEW_BASE_SHA" || boundary_status=$?
    if [ "$boundary_status" -eq 2 ]; then
        return 2
    elif [ "$boundary_status" -ne 0 ]; then
        recovery_message "$review_description did not leave local, remote, and PR heads aligned in round $round."
        return 1
    fi
    export AGENT_LOOP_PR_HEAD_SHA="$after_sha"
    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        classification="$(jq -r 'if .status == "clean" then "clean" else .classification end' <<<"$result_json")"
        allowed_heads_file="$AGENT_LOOP_LOG_DIR/$slug-review-round-$round-heads.json"
        write_review_transition_heads "$before_sha" "$after_sha" \
            "$allowed_heads_file" || {
            recovery_message "$engine review transition could not be pinned in round $round."
            return 1
        }
        if [ "$classification" != clean ]; then
            verify_v3_committed_pass_evidence "$slug" "$round" "$before_sha" \
                "$after_sha" "$result_file" "$allowed_heads_file" || {
                recovery_message "$engine review result lacks matching resolved v3 finding dispositions in round $round."
                return 1
            }
        fi
        attest_v3_review_result "$slug" "$round" "$AGENT_LOOP_REVIEW_BASE_SHA" \
            "$before_sha" "$after_sha" "$result_file" "$result_hash" \
            "$allowed_heads_file" || {
            recovery_message "$engine review result attestation failed in round $round."
            return 1
        }
        outcome_file="$result_file"
    else
        classify_review_result "$engine" "$before_sha" "$after_sha" "$outcome_file" || return 1
        classification="$REVIEW_FIX_CLASSIFICATION"
        if [ "$classification" = clean ]; then
            verify_clean_pass_attestation "$slug" "$round" "$before_sha" || {
                recovery_message "$engine review did not publish the required clean-pass attestation in round $round."
                return 1
            }
        else
            verify_review_completion_attestation "$slug" "$round" "$before_sha" "$after_sha" || {
                recovery_message "$engine review committed but posted no final-lane completion attestation in round $round."
                return 1
            }
            verify_committed_pass_evidence "$slug" "$round" "$after_sha" || {
                recovery_message "$engine review committed without a resolved same-round finding and structured fix disposition in round $round."
                return 1
            }
        fi
    fi
    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        outcome_signature="file:$result_hash"
        require_review_outcome_signature "$engine" "$outcome_file" \
            "$outcome_signature" "after attestation in round $round" || return 1
    else
        outcome_signature="$(review_outcome_signature "$outcome_file")" || {
            recovery_message "Could not snapshot $engine review outcome in round $round."
            return 1
        }
    fi
    run_validation "$slug-review-round-$round" || {
        recovery_message "Validation after $validation_description failed in review round $round."
        return 1
    }
    require_review_outcome_signature "$engine" "$outcome_file" \
        "$outcome_signature" "during validation in round $round" || return 1
    boundary_status=0
    attest_review_head "after $engine review validation in round $round" \
        "$AGENT_LOOP_REVIEW_BASE_SHA" || boundary_status=$?
    if [ "$boundary_status" -eq 2 ]; then
        return 2
    elif [ "$boundary_status" -ne 0 ]; then
        recovery_message "PR head attestation failed after $engine review validation in round $round."
        return 1
    fi

    REVIEW_PASS_CLASSIFICATION="$classification"
    REVIEW_PASS_OUTCOME_FILE="$outcome_file"
    REVIEW_PASS_OUTCOME_SIGNATURE="$outcome_signature"
}

require_fast_forward_base_advance() {
    local reviewed_base="$1" phase="$2" latest_base
    fetch_base || return 1
    latest_base="$(git rev-parse "$AGENT_LOOP_REVIEW_BASE")" || return 1
    if ! git merge-base --is-ancestor "$reviewed_base" "$latest_base"; then
        recovery_message "Base branch moved non-fast-forward $phase."
        return 1
    fi
}

run_review_convergence() {
    local round="${1:-1}" codex_classification claude_classification
    local resume_engine="${2:-codex}"
    local codex_outcome_signature claude_outcome_signature
    local codex_outcome_file claude_outcome_file
    local round_base_sha latest_base_sha pass_status

    while [ "$round" -le "$REVIEW_MAX_ROUNDS" ]; do
        echo -e "${CYAN}↻${NC} Local review convergence round $round/$REVIEW_MAX_ROUNDS"
        export AGENT_LOOP_REVIEW_ROUND="$round"

        fetch_base
        round_base_sha="$(git rev-parse "$AGENT_LOOP_REVIEW_BASE")"
        export AGENT_LOOP_REVIEW_BASE_SHA
        AGENT_LOOP_REVIEW_BASE_SHA="$round_base_sha"
        if ! git merge-base --is-ancestor "$round_base_sha" HEAD; then
            echo -e "${BLUE}▸${NC} Integrating fresh base before review round $round"
            if ! git merge --no-edit "$round_base_sha"; then
                git merge --abort >/dev/null 2>&1 || true
                recovery_message "Fresh-base merge conflicted before review round $round."
                return 1
            fi
            inspect_publication_diff "$round_base_sha" || {
                recovery_message "Publication diff inspection failed before review round $round."
                return 1
            }
            run_validation "fresh-base-round-$round" || {
                recovery_message "Fresh-base validation failed before review round $round."
                return 1
            }
            push_review_head "after fresh-base integration for round $round" \
                "$round_base_sha" || {
                recovery_message "Could not publish fresh-base integration before review round $round."
                return 1
            }
        fi
        if [ "$resume_engine" = claude ]; then
            recover_v3_review_pass Codex codex "$round" "$round_base_sha" || {
                recovery_message "Could not recover the completed Codex leg for final-round continuation."
                return 1
            }
            echo "   Recovered authenticated Codex evidence; resuming the interrupted Claude leg"
            resume_engine=codex
        else
            update_run_state reviewing "$round" "$round_base_sha" "$(git rev-parse HEAD)" \
                "" "" codex || {
                recovery_message "Could not checkpoint review round $round."
                return 1
            }
            pass_status=0
            run_review_pass Codex codex "$CODEX_REVIEW_HOOK" "$round" \
                "configured Codex review hook" "Configured Codex review hook" \
                "Configured Codex review hook" \
                "the configured Codex review hook" || pass_status=$?
            if [ "$pass_status" -eq 2 ]; then
                require_fast_forward_base_advance "$round_base_sha" \
                    "during review round $round (Codex boundary)" || return 1
                echo "   PR base advanced during Codex review; restart at Codex on the next round"
                round=$((round + 1))
                update_run_state reviewing "$round" \
                    "$(git rev-parse "$AGENT_LOOP_REVIEW_BASE")" "$(git rev-parse HEAD)" \
                    "" "" codex || {
                    recovery_message "Could not checkpoint the next review round."
                    return 1
                }
                continue
            elif [ "$pass_status" -ne 0 ]; then
                return 1
            fi
        fi
        codex_classification="$REVIEW_PASS_CLASSIFICATION"
        codex_outcome_file="$REVIEW_PASS_OUTCOME_FILE"
        codex_outcome_signature="$REVIEW_PASS_OUTCOME_SIGNATURE"
        update_run_state reviewing "$round" "$round_base_sha" \
            "$(git rev-parse HEAD)" "" "" claude || {
            recovery_message "Could not checkpoint the Claude leg after Codex completed round $round."
            return 1
        }

        pass_status=0
        run_review_pass Claude "claude" "$CLAUDE_REVIEW_HOOK" "$round" \
            "configured Claude review hook" "Claude review hook" "Claude review" \
            "Claude review" || pass_status=$?
        if [ "$pass_status" -eq 2 ]; then
            require_fast_forward_base_advance "$round_base_sha" \
                "during review round $round (Claude boundary)" || return 1
            echo "   PR base advanced during Claude review; restart at Codex on the next round"
            round=$((round + 1))
            update_run_state reviewing "$round" \
                "$(git rev-parse "$AGENT_LOOP_REVIEW_BASE")" "$(git rev-parse HEAD)" \
                "" "" codex || {
                recovery_message "Could not checkpoint the next review round."
                return 1
            }
            continue
        elif [ "$pass_status" -ne 0 ]; then
            return 1
        fi
        claude_classification="$REVIEW_PASS_CLASSIFICATION"
        claude_outcome_file="$REVIEW_PASS_OUTCOME_FILE"
        claude_outcome_signature="$REVIEW_PASS_OUTCOME_SIGNATURE"
        require_review_outcome_signature Codex "$codex_outcome_file" \
            "$codex_outcome_signature" "before the round decision" || return 1
        require_review_outcome_signature Claude "$claude_outcome_file" \
            "$claude_outcome_signature" "before the round decision" || return 1
        REVIEW_ROUNDS_USED="$round"

        fetch_base
        latest_base_sha="$(git rev-parse "$AGENT_LOOP_REVIEW_BASE")"
        if ! git merge-base --is-ancestor "$round_base_sha" "$latest_base_sha"; then
            recovery_message "Base branch moved non-fast-forward during review round $round."
            return 1
        fi

        if [ "$codex_classification" != material ] && \
           [ "$claude_classification" != material ] && \
           [ "$latest_base_sha" = "$round_base_sha" ]; then
            verify_local_review_threads || {
                recovery_message "Local review threads are incomplete after review round $round."
                return 1
            }
            REVIEWED_BASE_SHA="$AGENT_LOOP_REVIEW_BASE_SHA"
            CONVERGED_CODEX_OUTCOME_FILE="$codex_outcome_file"
            CONVERGED_CODEX_OUTCOME_SIGNATURE="$codex_outcome_signature"
            CONVERGED_CLAUDE_OUTCOME_FILE="$claude_outcome_file"
            CONVERGED_CLAUDE_OUTCOME_SIGNATURE="$claude_outcome_signature"
            update_run_state converged "$round" "$REVIEWED_BASE_SHA" \
                "$(git rev-parse HEAD)" \
                "${codex_outcome_signature#file:}" \
                "${claude_outcome_signature#file:}" || {
                recovery_message "Could not checkpoint converged review state."
                return 1
            }
            unset AGENT_LOOP_REVIEW_BASE AGENT_LOOP_REVIEW_ENGINE AGENT_LOOP_REVIEW_ROUND \
                AGENT_LOOP_REVIEW_BASE_SHA AGENT_LOOP_REVIEW_OUTCOME_FILE \
                AGENT_LOOP_REVIEW_RESULT_FILE AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE \
                AGENT_LOOP_REVIEW_PUSH_STATE_FILE
            echo -e "${GREEN}✓${NC} Configured Codex and Claude hooks reported no material fixes in a complete round after $round round(s)"
            return 0
        fi

        if [ "$latest_base_sha" != "$round_base_sha" ]; then
            echo "   Base advanced during the round; integrate it on the draft PR and restart at Codex"
        else
            echo "   Review outcomes: Codex=$codex_classification Claude=$claude_classification; material fixes restart at Codex"
        fi
        round=$((round + 1))
        update_run_state reviewing "$round" "$latest_base_sha" \
            "$(git rev-parse HEAD)" "" "" codex || {
            recovery_message "Could not checkpoint the next review round."
            return 1
        }
    done

    unset AGENT_LOOP_REVIEW_BASE AGENT_LOOP_REVIEW_ENGINE AGENT_LOOP_REVIEW_ROUND \
        AGENT_LOOP_REVIEW_BASE_SHA AGENT_LOOP_REVIEW_OUTCOME_FILE \
        AGENT_LOOP_REVIEW_RESULT_FILE AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE \
        AGENT_LOOP_REVIEW_PUSH_STATE_FILE
    recovery_message "Configured review hooks did not converge within $REVIEW_MAX_ROUNDS round(s)."
    return 1
}

inspect_publication_diff() {
    local base_sha="$1" file_count
    # This function is called in an `||` context, so `set -e` is disabled in its
    # body; check the gate explicitly or its non-zero exit (conflict markers left in
    # a committed file, whitespace errors) is silently ignored and publication
    # proceeds with a corrupt diff.
    if ! git diff --check "$base_sha..HEAD"; then
        echo "publication diff contains conflict markers or whitespace errors" >&2
        return 1
    fi
    file_count="$(git diff --name-only "$base_sha..HEAD" | wc -l | tr -d ' ')"
    [ "$file_count" -gt 0 ] || { echo "publication diff is empty" >&2; return 1; }
    echo "   Publication diff: $file_count file(s)"
    git diff --stat "$base_sha..HEAD" | tail -n "$OUTPUT_MAX_LINES"
}

attest_remote_branch() {
    local branch="$1" expected_sha="$2" phase="$3"
    local remote_row remote_sha remote_ref
    remote_row="$(git ls-remote --exit-code --heads origin "refs/heads/$branch")" || {
        echo "could not attest remote issue branch $phase" >&2
        return 1
    }
    IFS=$'\t' read -r remote_sha remote_ref <<< "$remote_row"
    if [ "$remote_sha" != "$expected_sha" ] || \
       [ "$remote_ref" != "refs/heads/$branch" ]; then
        echo "remote issue branch changed $phase" >&2
        return 1
    fi
}

attest_pr_state() {
    local expected_sha="$1" expected_base="$2" expected_draft="$3" phase="$4"
    local row state draft head_branch head_sha base_branch base_sha
    row="$(gh pr view "$AGENT_LOOP_PR_NUMBER" \
        --json state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid \
        --jq '[.state,(.isDraft|tostring),.headRefName,.headRefOid,.baseRefName,.baseRefOid] | @tsv')" || {
        echo "could not attest PR boundary $phase" >&2
        return 1
    }
    IFS=$'\t' read -r state draft head_branch head_sha base_branch base_sha <<< "$row"
    if [ "$state" != OPEN ] || [ "$draft" != "$expected_draft" ] || \
       [ "$head_branch" != "$AGENT_LOOP_BRANCH" ] || \
       [ "$base_branch" != "$BASE_BRANCH" ] || \
       { [ -n "$expected_sha" ] && [ "$head_sha" != "$expected_sha" ]; }; then
        echo "PR identity, state, head, or base branch changed $phase" >&2
        return 1
    fi
    if [ -n "$expected_base" ] && [ "$base_sha" != "$expected_base" ]; then
        echo "PR base advanced or diverged $phase" >&2
        return 2
    fi
}

attest_pr_head() {
    attest_pr_state "$1" "$2" true "$3"
}

attest_ready_pr_head() {
    attest_pr_state "$1" "$2" false "$3"
}

attest_review_head() {
    local phase="$1" expected_base="$2" local_sha status
    status="$(git status --porcelain)" || return 1
    [ -z "$status" ] || {
        echo "review worktree is dirty $phase" >&2
        return 1
    }
    require_issue_branch_head || {
        echo "review HEAD is not attached to the issue branch $phase" >&2
        return 1
    }
    local_sha="$(git rev-parse HEAD)" || return 1
    attest_remote_branch "$AGENT_LOOP_BRANCH" "$local_sha" "$phase" || return 1
    attest_pr_head "$local_sha" "$expected_base" "$phase"
}

push_review_head() {
    local phase="$1" expected_base="$2" sha
    require_issue_branch_head || return 1
    sha="$(git rev-parse HEAD)" || return 1
    git push origin "$sha:refs/heads/$AGENT_LOOP_BRANCH" || return 1
    attest_remote_branch "$AGENT_LOOP_BRANCH" "$sha" "$phase" || return 1
    attest_pr_head "$sha" "$expected_base" "$phase"
}

fetch_local_review_threads() {
    local owner="${GH_REPO%%/*}" name="${GH_REPO#*/}"
    local ledger_file="$AGENT_LOOP_LOG_DIR/local-review-threads.json"
    local query
    # shellcheck disable=SC2016 # GraphQL variables are expanded by GitHub, not Bash.
    query='
query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$endCursor) {
        nodes {
          id
          isResolved
          repository { nameWithOwner }
          pullRequest { number }
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
    gh api graphql --paginate --slurp -f query="$query" \
        -f owner="$owner" -f name="$name" -F number="$AGENT_LOOP_PR_NUMBER" \
        > "$ledger_file" || {
        echo "could not load PR review threads" >&2
        return 1
    }
    printf '%s\n' "$ledger_file"
}

fetch_review_attestation_bodies() {
    local output_file="$1"
    {
        gh api --paginate "repos/$GH_REPO/issues/$AGENT_LOOP_PR_NUMBER/comments" |
            jq -r --arg reviewer "$CURRENT_LOGIN" \
                '.[] | select(.user.login == $reviewer) | .body // empty' &&
        gh api --paginate "repos/$GH_REPO/pulls/$AGENT_LOOP_PR_NUMBER/comments" |
            jq -r --arg reviewer "$CURRENT_LOGIN" \
                '.[] | select(.user.login == $reviewer) | .body // empty' &&
        gh api --paginate "repos/$GH_REPO/pulls/$AGENT_LOOP_PR_NUMBER/reviews" |
            jq -r --arg reviewer "$CURRENT_LOGIN" \
                '.[] | select(.user.login == $reviewer) | .body // empty'
    } > "$output_file"
}

review_pass_identity_is_attested() {
    local engine="$1" round="$2" base_sha="$3" head_sha="$4"
    local bodies_file="$AGENT_LOOP_LOG_DIR/$engine-review-round-$round-resume-attestations.txt"
    local marker_pattern
    marker_pattern="^<!-- local-review-pass:v3 engine=$engine round=$round base=$base_sha head=$head_sha result-sha256=[0-9a-f]{64} -->$"
    fetch_review_attestation_bodies "$bodies_file" || return 2
    grep -Eq -- "$marker_pattern" "$bodies_file"
}

verify_review_completion_attestation() {
    local engine="$1" round="$2" before_sha="$3" after_sha="$4"
    local bodies_file="$AGENT_LOOP_LOG_DIR/$engine-review-round-$round-complete.txt"
    local marker
    marker="<!-- local-review-complete:v1 engine=$engine round=$round before=$before_sha head=$after_sha -->"
    fetch_review_attestation_bodies "$bodies_file" || return 1
    grep -Fq -- "$marker" "$bodies_file"
}

verify_committed_pass_evidence() {
    local engine="$1" round="$2" after_sha="$3" ledger_file
    ledger_file="$(fetch_local_review_threads)" || return 1
    jq -e --arg reviewer "$CURRENT_LOGIN" --arg engine "$engine" \
        --argjson round "$round" --arg after "$after_sha" '
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

verify_v3_committed_pass_evidence() {
    local engine="$1" round="$2" before_sha="$3" after_sha="$4" result_file="$5"
    local allowed_heads_file="$6" base_sha="${7:-${AGENT_LOOP_REVIEW_BASE_SHA:-}}"
    local historical_comment_ids_file="${8:-${AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE:-}}"
    local ledger_file ledger_signature live_head
    [ -n "$base_sha" ] || return 1
    [ -n "$historical_comment_ids_file" ] || return 1
    live_head="$(git rev-parse HEAD)" || return 1
    git merge-base --is-ancestor "$after_sha" "$live_head" || return 1
    ledger_file="$(fetch_local_review_threads)" || return 1
    ledger_signature="$(review_outcome_signature "$ledger_file")" || return 1
    node "$REVIEW_LEDGER" verify-ledger \
        --repo "$GH_REPO" --pr "$AGENT_LOOP_PR_NUMBER" --head "$live_head" \
        --result-head "$after_sha" \
        --threads-file "$ledger_file" --actor "$CURRENT_LOGIN" \
        --expected-threads-sha256 "${ledger_signature#file:}" \
        --engine "$engine" --round "$round" \
        --base "$base_sha" --before "$before_sha" \
        --result-file "$result_file" --allowed-heads-file "$allowed_heads_file" \
        --historical-comment-ids-file "$historical_comment_ids_file" >/dev/null
}

write_review_transition_heads() {
    local before_sha="$1" after_sha="$2" output_file="$3"
    git merge-base --is-ancestor "$before_sha" "$after_sha" || return 1
    {
        printf '%s\n' "$before_sha"
        if [ "$before_sha" != "$after_sha" ]; then
            git rev-list --reverse --ancestry-path "$before_sha..$after_sha"
        fi
    } | jq -R -s 'split("\n") | map(select(length > 0))' > "$output_file"
}

attest_v3_review_result() {
    local engine="$1" round="$2" base_sha="$3" before_sha="$4" after_sha="$5" result_file="$6"
    local expected_result_hash="$7" allowed_heads_file="$8"
    local ledger_file ledger_signature attestation_json observed_result_hash
    ledger_file="$(fetch_local_review_threads)" || return 1
    ledger_signature="$(review_outcome_signature "$ledger_file")" || return 1
    attestation_json="$(node "$REVIEW_LEDGER" attest \
        --repo "$GH_REPO" --pr "$AGENT_LOOP_PR_NUMBER" --head "$after_sha" \
        --engine "$engine" --round "$round" --base "$base_sha" \
        --before "$before_sha" --result-file "$result_file" \
        --threads-file "$ledger_file" --actor "$CURRENT_LOGIN" \
        --expected-threads-sha256 "${ledger_signature#file:}" \
        --allowed-heads-file "$allowed_heads_file" \
        --expected-result-sha256 "$expected_result_hash")" || return 1
    observed_result_hash="$(jq -r '.result_sha256' <<<"$attestation_json")" || return 1
    [ "$observed_result_hash" = "$expected_result_hash" ] || {
        echo "$engine review result changed before attestation" >&2
        return 1
    }
}

verify_local_review_threads() {
    local ledger_file ledger_signature historical_comment_ids_file=""
    local -a historical_args=()
    ledger_file="$(fetch_local_review_threads)" || return 1
    ledger_signature="$(review_outcome_signature "$ledger_file")" || return 1
    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        if [ "${REVIEW_ROUNDS_USED:-0}" -gt 0 ]; then
            historical_comment_ids_file="$AGENT_LOOP_LOG_DIR/codex-review-round-$REVIEW_ROUNDS_USED-historical-comment-ids.json"
        else
            historical_comment_ids_file="${AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE:-}"
        fi
        [ -n "$historical_comment_ids_file" ] || return 1
        historical_args=(--historical-comment-ids-file "$historical_comment_ids_file")
    fi
    if [ "$REVIEW_CONTRACT_VERSION" = 2 ]; then
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
        ' "$ledger_file" >/dev/null || {
            echo "local-review threads must contain a disposition reply and be resolved" >&2
            return 1
        }
        return
    fi
    # Check comment pagination across every thread *before* filtering by marker.
    # A thread whose marker sits past the first comment page carries no marker in
    # `comments.nodes`, so filtering first would silently drop exactly the threads
    # whose disposition cannot be established.
    #
    # Disposition is then anchored to the newest marker rather than to thread
    # length: the ledger requires a recurring root cause to reuse its existing
    # thread, so a resolved round-1 thread that gains an unanswered round-2
    # finding still has two or more comments and would pass a length check.
    node "$REVIEW_LEDGER" verify-ledger \
        --repo "$GH_REPO" --pr "$AGENT_LOOP_PR_NUMBER" \
        --head "$(git rev-parse HEAD)" --threads-file "$ledger_file" \
        --expected-threads-sha256 "${ledger_signature#file:}" \
        --actor "$CURRENT_LOGIN" "${historical_args[@]}" >/dev/null || {
        echo "local-review threads must contain a disposition reply and be resolved" >&2
        return 1
    }
}

verify_clean_pass_attestation() {
    local engine="$1" round="$2" reviewed_head="$3"
    local reviews_file="$AGENT_LOOP_LOG_DIR/$engine-review-round-$round-reviews.json"
    local marker
    marker="<!-- local-review-pass:v1 engine=$engine round=$round head=$reviewed_head -->"
    gh api --paginate --slurp \
        "repos/$GH_REPO/pulls/$AGENT_LOOP_PR_NUMBER/reviews?per_page=100" \
        > "$reviews_file" || {
        echo "could not load PR reviews for the $engine clean-pass attestation" >&2
        return 1
    }
    # Only the actor running this loop can attest its own pass. The marker text is
    # fully derivable from public PR state, so a review posted by any other
    # account is context, not evidence that the configured hook ran.
    jq -e --arg marker "$marker" --arg head "$reviewed_head" \
        --arg reviewer "$CURRENT_LOGIN" '
      any(.[][]?;
        (.user.login == $reviewer) and
        (.commit_id == $head) and
        ((.body // "") | contains($marker)) and
        ((.body // "") | contains("no new material findings")))
    ' "$reviews_file" >/dev/null || {
        echo "$engine review must attest its round, exact head, and no new material findings" >&2
        return 1
    }
}

close_unattested_pr() {
    local pr_url="$1" reason="$2"
    echo "$reason: $pr_url" >&2
    if gh pr close "$pr_url" >/dev/null; then
        echo "closed draft PR whose head could not be attested: $pr_url" >&2
    else
        echo "could not close unverified draft PR; operator action is required: $pr_url" >&2
    fi
}

open_draft_pr() {
    local number="$1" branch="$2" publication_sha="$3" publication_base_sha="$4"
    local body_file pr_url pr_number
    require_origin_identity || {
        echo "origin identity changed before draft PR publication" >&2
        return 1
    }
    [ "$(git rev-parse "refs/heads/$branch")" = "$publication_sha" ] || {
        echo "issue branch changed after publication snapshot" >&2
        return 1
    }
    # Create-only lease: fail if the branch appeared after the absence check.
    # This never rewrites an existing remote ref.
    git push --force-with-lease="refs/heads/$branch:" origin \
        "$publication_sha:refs/heads/$branch"
    attest_remote_branch "$branch" "$publication_sha" "after draft publication push" || return 1
    git branch --set-upstream-to="origin/$branch" "$branch"
    body_file="$AGENT_LOOP_LOG_DIR/pr-body.md"
    {
        echo "## Summary"
        echo
        echo "Implementation complete. Local Codex and Claude review is running on this draft PR."
        echo
        echo "## Test plan"
        echo
        if [ -n "$SETUP_HOOK" ]; then echo "- [x] configured setup hook completed"; fi
        echo "- [ ] local Codex and Claude review ledger converged"
        echo "- [ ] every local-review thread replied to and resolved"
        echo "- [x] configured non-mutating local validation hook"
        echo
        echo "Closes #$number"
    } > "$body_file"
    pr_url="$(gh pr create --draft --base "$BASE_BRANCH" --head "$branch" \
        --title "agent-loop: resolve #$number" --body-file "$body_file")" || {
        echo "could not create draft PR after publishing remote branch $branch" >&2
        return 1
    }
    pr_number="$(gh pr view "$pr_url" --json number --jq .number)" || return 1
    export AGENT_LOOP_PR_NUMBER="$pr_number"
    export AGENT_LOOP_PR_URL="$pr_url"
    export AGENT_LOOP_PR_HEAD_SHA="$publication_sha"
    if ! attest_pr_head "$publication_sha" "$publication_base_sha" \
        "after draft PR creation"; then
        close_unattested_pr "$pr_url" "created draft PR head could not be attested"
        return 1
    fi
    echo -e "${GREEN}✓${NC} Opened draft review ledger $pr_url"
}

restore_draft_after_finalization_failure() {
    local expected_sha="$1" expected_base="$2" reason="$3"
    if ! gh pr ready "$AGENT_LOOP_PR_NUMBER" --undo >/dev/null 2>&1; then
        echo "$reason; rollback to draft failed and operator action is required" >&2
        return 1
    fi
    if ! attest_pr_state "" "" true "after finalization rollback"; then
        echo "$reason; rollback could not be attested and operator action is required" >&2
        return 1
    fi
    echo "$reason; PR was attested back in draft state" >&2
    return 0
}

finalize_pr() {
    local body_file="$AGENT_LOOP_LOG_DIR/pr-body-final.md"
    local final_sha
    final_sha="$(git rev-parse HEAD)" || return 1
    attest_review_head "before marking ready" "$REVIEWED_BASE_SHA" || return 1
    verify_local_review_threads || return 1
    {
        echo "## Summary"
        echo
        echo "Configured Codex and Claude review hooks reported no material fixes in a complete round after"
        echo "$REVIEW_ROUNDS_USED round(s) against fresh \`origin/$BASE_BRANCH\`."
        echo
        echo "## Test plan"
        echo
        if [ -n "$SETUP_HOOK" ]; then echo "- [x] configured setup hook completed"; fi
        echo "- [x] configured Codex and Claude hooks reported no material fixes in a complete round ($REVIEW_ROUNDS_USED round(s))"
        echo "- [x] every local-review thread contains a disposition reply and is resolved"
        echo "- [x] fresh-base integration and publication-diff inspection"
        echo "- [x] configured non-mutating local validation hook"
        echo
        echo "Closes #$AGENT_LOOP_ISSUE_ID"
    } > "$body_file"
    gh pr edit "$AGENT_LOOP_PR_NUMBER" --body-file "$body_file" || {
        recovery_message "Could not update the PR body; remote PR mutation state is uncertain."
        return 1
    }
    attest_review_head "immediately before marking ready" "$REVIEWED_BASE_SHA" || return 1
    verify_local_review_threads || return 1
    verify_converged_review_outcomes || return 1
    update_run_state finalizing "$REVIEW_ROUNDS_USED" "$REVIEWED_BASE_SHA" \
        "$final_sha" || {
        recovery_message "Could not checkpoint the finalizing review state."
        return 1
    }
    if ! gh pr ready "$AGENT_LOOP_PR_NUMBER"; then
        if attest_pr_head "$final_sha" "$REVIEWED_BASE_SHA" \
            "after failed ready mutation"; then
            echo "could not mark PR ready; it remains in the attested draft state" >&2
            return 1
        fi
        restore_draft_after_finalization_failure "$final_sha" "$REVIEWED_BASE_SHA" \
            "ready mutation failed after changing or obscuring PR state" || return 1
        return 1
    fi
    if ! attest_ready_pr_head "$final_sha" "$REVIEWED_BASE_SHA" \
        "after marking ready"; then
        restore_draft_after_finalization_failure "$final_sha" "$REVIEWED_BASE_SHA" \
            "ready PR boundary changed during finalization" || return 1
        return 1
    fi
    if ! verify_converged_review_outcomes; then
        restore_draft_after_finalization_failure "$final_sha" "$REVIEWED_BASE_SHA" \
            "review result ledger evidence changed during finalization" || return 1
        return 1
    fi
    if ! verify_local_review_threads; then
        restore_draft_after_finalization_failure "$final_sha" "$REVIEWED_BASE_SHA" \
            "local-review ledger changed during finalization" || return 1
        return 1
    fi
    if ! update_run_state finalized "$REVIEW_ROUNDS_USED" "$REVIEWED_BASE_SHA" \
        "$final_sha"; then
        restore_draft_after_finalization_failure "$final_sha" "$REVIEWED_BASE_SHA" \
            "finalized run-state checkpoint failed" || return 1
        update_run_state finalizing "$REVIEW_ROUNDS_USED" "$REVIEWED_BASE_SHA" \
            "$final_sha" || {
            recovery_message "Could not restore a resumable finalizing checkpoint after rollback."
            return 1
        }
        return 1
    fi
    echo -e "${GREEN}✓${NC} Review converged; PR ready: $AGENT_LOOP_PR_URL ($final_sha)"
}

resume_review_run() {
    local issue_json_value state_head state_phase state_round current_head branch_status
    local resume_boundary_status=0 checkpoint_base latest_base attestation_status
    local issue_title_sha256 issue_body_sha256
    local ready_finalization=false pr_draft_state state_review_engine
    local restart_after_interrupted_pass=false resume_review_engine=codex
    local finalizing_head_drift=false
    SELECTED_ID="$(jq -r '.issue' <<<"$RESUME_STATE_JSON")"
    issue_json_value="$(issue_json "$SELECTED_ID")" || {
        recovery_message "Could not reload issue #$SELECTED_ID for review recovery."
        return 1
    }
    INCLUDE_ASSIGNED=true
    issue_is_selectable "$SELECTED_ID" "$issue_json_value" || {
        recovery_message "Issue #$SELECTED_ID is no longer open, agent-labeled, and assigned only to the current user."
        return 1
    }
    set_selected_issue_context "$issue_json_value" || return 1
    issue_title_sha256="$(printf '%s' "$SELECTED_TITLE" | sha256_text)" || return 1
    issue_body_sha256="$(printf '%s' "$SELECTED_BODY" | sha256_text)" || return 1
    if [ "$issue_title_sha256" != "$(jq -r '.issueTitleSha256' <<<"$RESUME_STATE_JSON")" ] || \
       [ "$issue_body_sha256" != "$(jq -r '.issueBodySha256' <<<"$RESUME_STATE_JSON")" ]; then
        recovery_message "Issue title or body changed since the review checkpoint."
        return 1
    fi
    SELECTED_ASSIGNED=true
    AGENT_LOOP_BRANCH="$(jq -r '.branch' <<<"$RESUME_STATE_JSON")"
    ACTIVE_WORKTREE="$(jq -r '.worktree' <<<"$RESUME_STATE_JSON")"
    AGENT_LOOP_LOG_DIR="$(jq -r '.logDir' <<<"$RESUME_STATE_JSON")"
    state_head="$(jq -r '.headSha' <<<"$RESUME_STATE_JSON")"
    state_phase="$(jq -r '.phase' <<<"$RESUME_STATE_JSON")"
    state_round="$(jq -r '.round' <<<"$RESUME_STATE_JSON")"
    state_review_engine="$(jq -r '.reviewEngine // empty' <<<"$RESUME_STATE_JSON")"
    if [ ! -d "$ACTIVE_WORKTREE" ] || [ -L "$ACTIVE_WORKTREE" ]; then
        recovery_message "Recorded recovery worktree is unavailable or unsafe."
        return 1
    fi
    if [ ! -d "$AGENT_LOOP_LOG_DIR" ] || [ -L "$AGENT_LOOP_LOG_DIR" ]; then
        recovery_message "Recorded recovery log directory is unavailable or unsafe."
        return 1
    fi
    branch_status="$(git -C "$ACTIVE_WORKTREE" status --porcelain)" || return 1
    [ -z "$branch_status" ] || {
        recovery_message "Recorded recovery worktree is dirty."
        return 1
    }
    [ "$(git -C "$ACTIVE_WORKTREE" branch --show-current)" = "$AGENT_LOOP_BRANCH" ] || {
        recovery_message "Recorded recovery worktree is not on its issue branch."
        return 1
    }
    current_head="$(git -C "$ACTIVE_WORKTREE" rev-parse HEAD)" || return 1
    git -C "$ACTIVE_WORKTREE" merge-base --is-ancestor "$state_head" "$current_head" || {
        recovery_message "Recovery worktree no longer contains its checkpointed head."
        return 1
    }
    cd "$ACTIVE_WORKTREE"
    require_origin_identity || {
        recovery_message "Recovery worktree origin identity does not match the run."
        return 1
    }
    export AGENT_LOOP_ISSUE_ID="$SELECTED_ID"
    export AGENT_LOOP_ISSUE_TITLE="$SELECTED_TITLE"
    export AGENT_LOOP_ISSUE_BODY="$SELECTED_BODY"
    export AGENT_LOOP_BASE_BRANCH="$BASE_BRANCH"
    export AGENT_LOOP_BRANCH
    export AGENT_LOOP_WORKTREE="$ACTIVE_WORKTREE"
    export AGENT_LOOP_LOG_DIR
    export AGENT_LOOP_PROMPT="${PROMPT_TEMPLATE//\{ISSUE_ID\}/$SELECTED_ID}"
    export AGENT_LOOP_REVIEW_PUSH_HELPER="$REVIEW_PUSH_HELPER"
    AGENT_LOOP_PR_NUMBER="$(jq -r '.prNumber' <<<"$RESUME_STATE_JSON")" || return 1
    AGENT_LOOP_PR_URL="$(jq -r '.prUrl' <<<"$RESUME_STATE_JSON")" || return 1
    export AGENT_LOOP_PR_NUMBER AGENT_LOOP_PR_URL
    export AGENT_LOOP_PR_HEAD_SHA="$current_head"
    export AGENT_LOOP_REVIEW_BASE="$BASE_REMOTE_REF"
    REVIEW_ROUNDS_USED=0
    REVIEWED_BASE_SHA=""
    CONVERGED_CODEX_OUTCOME_FILE=""
    CONVERGED_CODEX_OUTCOME_SIGNATURE=""
    CONVERGED_CLAUDE_OUTCOME_FILE=""
    CONVERGED_CLAUDE_OUTCOME_SIGNATURE=""

    checkpoint_base="$(jq -r '.baseSha' <<<"$RESUME_STATE_JSON")"
    if [ "$state_phase" = finalized ]; then
        [ "$current_head" = "$state_head" ] || {
            recovery_message "Finalized recovery worktree head no longer matches its exact checkpoint."
            return 1
        }
        REVIEW_ROUNDS_USED="$state_round"
        REVIEWED_BASE_SHA="$checkpoint_base"
        CONVERGED_CODEX_OUTCOME_FILE="$AGENT_LOOP_LOG_DIR/codex-review-round-$state_round.result.json"
        CONVERGED_CLAUDE_OUTCOME_FILE="$AGENT_LOOP_LOG_DIR/claude-review-round-$state_round.result.json"
        CONVERGED_CODEX_OUTCOME_SIGNATURE="file:$(jq -r '.codexResultSha256' <<<"$RESUME_STATE_JSON")"
        CONVERGED_CLAUDE_OUTCOME_SIGNATURE="file:$(jq -r '.claudeResultSha256' <<<"$RESUME_STATE_JSON")"
        attest_ready_pr_head "$state_head" "$checkpoint_base" \
            "before finalized batch recovery" || {
            recovery_message "Finalized PR no longer matches its exact ready checkpoint."
            return 1
        }
        verify_converged_review_outcomes || return 1
        verify_local_review_threads || return 1
        verify_issue_for_publication "$SELECTED_ID" "$AGENT_LOOP_PR_NUMBER" || {
            recovery_message "Issue requirements or readiness changed after the finalized checkpoint."
            return 1
        }
        inspect_publication_diff "$checkpoint_base" || {
            recovery_message "Finalized reviewed diff inspection failed during batch recovery."
            return 1
        }
        run_validation "finalized-batch-recovery" || {
            recovery_message "Finalized reviewed-head validation failed during batch recovery."
            return 1
        }
        attest_ready_pr_head "$state_head" "$checkpoint_base" \
            "after finalized batch recovery validation" || {
            recovery_message "Finalized PR changed during batch recovery validation."
            return 1
        }
        cd "$PROJECT_DIR"
        if [ -z "${AGENT_LOOP_BATCH_PARENT_STATE_FILE:-}" ]; then
            git worktree remove "$ACTIVE_WORKTREE"
            ACTIVE_WORKTREE=""
        else
            echo "   Finalized worktree preserved for parent batch checkpointing: $ACTIVE_WORKTREE"
        fi
        echo -e "${GREEN}✓${NC} Re-attested finalized issue #$SELECTED_ID; local branch retained at $AGENT_LOOP_BRANCH"
        return 0
    fi
    if [ "$state_phase" = reviewing ] && [ "$state_review_engine" = codex ] && \
       [ "$current_head" = "$state_head" ]; then
        attestation_status=0
        review_pass_identity_is_attested codex "$state_round" \
            "$checkpoint_base" "$state_head" || attestation_status=$?
        if [ "$attestation_status" -eq 0 ]; then
            restart_after_interrupted_pass=true
        elif [ "$attestation_status" -ne 1 ]; then
            recovery_message "Could not reconcile the interrupted Codex pass attestation."
            return 1
        fi
    fi
    if [ "$state_phase" = reviewing ] && \
       { [ "$state_review_engine" = claude ] || [ "$current_head" != "$state_head" ]; }; then
        restart_after_interrupted_pass=true
    fi
    if [ "$restart_after_interrupted_pass" = true ]; then
        if [ "$state_round" -lt "$REVIEW_MAX_ROUNDS" ]; then
            state_round=$((state_round + 1))
        else
            if [ -f "$AGENT_LOOP_LOG_DIR/codex-review-round-$state_round.result.json" ]; then
                resume_review_engine=claude
                echo "   Final configured review round was interrupted; resuming its remaining leg"
            else
                echo "   Final configured review round was interrupted; replaying it without consuming another round"
            fi
        fi
    fi
    if [ "$state_phase" = finalizing ]; then
        pr_draft_state="$(gh pr view "$AGENT_LOOP_PR_NUMBER" --json isDraft --jq '.isDraft')" || {
            recovery_message "Could not inspect the finalizing PR state."
            return 1
        }
        case "$pr_draft_state" in
            true) ;;
            false) ready_finalization=true ;;
            *) recovery_message "Finalizing PR draft state is invalid."; return 1 ;;
        esac
        if [ "$current_head" != "$state_head" ]; then
            finalizing_head_drift=true
            if [ "$ready_finalization" = true ]; then
                restore_draft_after_finalization_failure "$current_head" "$checkpoint_base" \
                    "ready PR head moved beyond the finalizing checkpoint" || return 1
                ready_finalization=false
                pr_draft_state=true
            fi
        fi
        attest_pr_state "$current_head" "$checkpoint_base" "$pr_draft_state" \
            "before finalization recovery" || resume_boundary_status=$?
    else
        attest_pr_state "$current_head" "$checkpoint_base" \
            true "before review recovery" || resume_boundary_status=$?
    fi
    if [ "$resume_boundary_status" -eq 2 ]; then
        if [ "$ready_finalization" = true ]; then
            restore_draft_after_finalization_failure "$current_head" "$checkpoint_base" \
                "base advanced after the PR was marked ready" || return 1
            ready_finalization=false
        fi
        fetch_base || return 1
        latest_base="$(git rev-parse "$BASE_REMOTE_REF")" || return 1
        git merge-base --is-ancestor "$checkpoint_base" "$latest_base" || {
            recovery_message "PR base moved non-fast-forward since the recovery checkpoint."
            return 1
        }
        if [ "$state_phase" = converged ] || [ "$state_phase" = finalizing ]; then
            state_round=$((state_round + 1))
        fi
        state_phase=reviewing
        update_run_state reviewing "$state_round" "$latest_base" "$current_head" \
            "" "" codex || {
            recovery_message "Could not checkpoint the resumed review round."
            return 1
        }
        echo "   Base advanced since the checkpoint; the resumed round will integrate and review it."
    elif [ "$resume_boundary_status" -ne 0 ]; then
        if [ "$ready_finalization" = true ]; then
            restore_draft_after_finalization_failure "$current_head" "$checkpoint_base" \
                "ready PR boundary changed during finalization recovery" || return 1
            ready_finalization=false
        fi
        recovery_message "Draft PR state does not match the recovery checkpoint."
        return 1
    fi
    if [ "$finalizing_head_drift" = true ] && [ "$state_phase" = finalizing ]; then
        state_round=$((state_round + 1))
        state_phase=reviewing
        update_run_state reviewing "$state_round" "$checkpoint_base" "$current_head" \
            "" "" codex || {
            recovery_message "Could not checkpoint review restart after finalizing head drift."
            return 1
        }
        echo "   PR head moved since finalization; the resumed round will review the new draft head."
    elif [ "$state_phase" = converged ] && [ "$current_head" != "$state_head" ]; then
        state_round=$((state_round + 1))
        state_phase=reviewing
        update_run_state reviewing "$state_round" "$checkpoint_base" "$current_head" \
            "" "" codex || {
            recovery_message "Could not checkpoint review restart after converged head drift."
            return 1
        }
        echo "   PR head moved since convergence; the resumed round will review the new draft head."
    fi
    if { [ "$state_phase" = converged ] || [ "$state_phase" = finalizing ]; } && \
       [ "$current_head" = "$state_head" ]; then
        REVIEW_ROUNDS_USED="$state_round"
        REVIEWED_BASE_SHA="$(jq -r '.baseSha' <<<"$RESUME_STATE_JSON")"
        CONVERGED_CODEX_OUTCOME_FILE="$AGENT_LOOP_LOG_DIR/codex-review-round-$state_round.result.json"
        CONVERGED_CLAUDE_OUTCOME_FILE="$AGENT_LOOP_LOG_DIR/claude-review-round-$state_round.result.json"
        CONVERGED_CODEX_OUTCOME_SIGNATURE="file:$(jq -r '.codexResultSha256' <<<"$RESUME_STATE_JSON")"
        CONVERGED_CLAUDE_OUTCOME_SIGNATURE="file:$(jq -r '.claudeResultSha256' <<<"$RESUME_STATE_JSON")"
        if ! verify_converged_review_outcomes; then
            if [ "$ready_finalization" = true ]; then
                restore_draft_after_finalization_failure "$current_head" "$REVIEWED_BASE_SHA" \
                    "recovered review result evidence changed" || return 1
            fi
            return 1
        fi
        echo -e "${GREEN}✓${NC} Recovered converged review checkpoint at round $state_round"
    else
        run_review_convergence "$state_round" "$resume_review_engine"
    fi

    inspect_publication_diff "$REVIEWED_BASE_SHA" || {
        if [ "$ready_finalization" = true ]; then
            restore_draft_after_finalization_failure "$current_head" "$REVIEWED_BASE_SHA" \
                "recovered final diff inspection failed" || return 1
        fi
        recovery_message "Final reviewed diff inspection failed during recovery."
        return 1
    }
    run_validation "final-reviewed-head" || {
        if [ "$ready_finalization" = true ]; then
            restore_draft_after_finalization_failure "$current_head" "$REVIEWED_BASE_SHA" \
                "recovered final validation failed" || return 1
        fi
        recovery_message "Final reviewed-head validation failed during recovery."
        return 1
    }
    if [ "$ready_finalization" = true ]; then
        attest_ready_pr_head "$(git rev-parse HEAD)" "$REVIEWED_BASE_SHA" \
            "after recovered reviewed-head validation" || {
            restore_draft_after_finalization_failure "$current_head" "$REVIEWED_BASE_SHA" \
                "ready reviewed-head attestation failed during recovery" || return 1
            recovery_message "Ready reviewed-head attestation failed during recovery."
            return 1
        }
    else
        attest_review_head "after recovered reviewed-head validation" "$REVIEWED_BASE_SHA" || {
            recovery_message "Final reviewed-head attestation failed during recovery."
            return 1
        }
    fi
    if ! verify_converged_review_outcomes; then
        if [ "$ready_finalization" = true ]; then
            restore_draft_after_finalization_failure "$current_head" "$REVIEWED_BASE_SHA" \
                "recovered review result evidence changed" || return 1
        fi
        return 1
    fi
    if ! verify_local_review_threads; then
        if [ "$ready_finalization" = true ]; then
            restore_draft_after_finalization_failure "$current_head" "$REVIEWED_BASE_SHA" \
                "recovered local-review ledger changed" || return 1
        fi
        return 1
    fi
    verify_issue_for_publication "$SELECTED_ID" "$AGENT_LOOP_PR_NUMBER" || {
        if [ "$ready_finalization" = true ]; then
            restore_draft_after_finalization_failure "$current_head" "$REVIEWED_BASE_SHA" \
                "issue readiness changed during recovered finalization" || return 1
        fi
        recovery_message "Issue requirements or readiness changed before recovered publication."
        return 1
    }
    if [ "$ready_finalization" = true ]; then
        update_run_state finalized "$REVIEW_ROUNDS_USED" "$REVIEWED_BASE_SHA" \
            "$(git rev-parse HEAD)" || {
            restore_draft_after_finalization_failure "$current_head" "$REVIEWED_BASE_SHA" \
                "recovered finalized checkpoint failed" || return 1
            update_run_state finalizing "$REVIEW_ROUNDS_USED" "$REVIEWED_BASE_SHA" \
                "$(git rev-parse HEAD)" || {
                recovery_message "Could not restore a resumable finalizing checkpoint after recovered rollback."
                return 1
            }
            recovery_message "Could not complete the recovered finalized checkpoint."
            return 1
        }
        echo -e "${GREEN}✓${NC} Recovered ready PR finalization: $AGENT_LOOP_PR_URL ($(git rev-parse HEAD))"
    else
        finalize_pr
    fi
    cd "$PROJECT_DIR"
    if [ -z "${AGENT_LOOP_BATCH_PARENT_STATE_FILE:-}" ]; then
        git worktree remove "$ACTIVE_WORKTREE"
        ACTIVE_WORKTREE=""
    else
        echo "   Finalized worktree preserved for parent batch checkpointing: $ACTIVE_WORKTREE"
    fi
    echo -e "${GREEN}✓${NC} Issue #$SELECTED_ID recovery complete; local branch retained at $AGENT_LOOP_BRANCH"
}

if [ -n "$RESUME_RUN_FILE" ]; then
    echo -e "${CYAN}→${NC} resuming agent-loop review from $AGENT_LOOP_RUN_STATE_FILE"
    resume_review_run
    echo -e "${GREEN}■${NC} agent-loop recovery finished"
    exit 0
fi

if [ -n "$RESUME_BATCH_FILE" ]; then
    BATCH_STATE_FILE="$(realpath -- "$RESUME_BATCH_FILE")" || exit 1
    case "$BATCH_STATE_FILE" in "$LOG_ROOT"/*) ;; *) echo "batch state is outside configured log_root" >&2; exit 1 ;; esac
    acquire_batch_lock "$BATCH_STATE_FILE" || exit 1
    batch_json="$(python3 "$RUN_STATE_HELPER" batch-show --file "$BATCH_STATE_FILE")" || exit 1
    [ "$(jq -r '.repo' <<<"$batch_json")" = "$GH_REPO" ] || { echo "batch state repository mismatch" >&2; exit 1; }
    [ "$(jq -r '.baseBranch' <<<"$batch_json")" = "$BASE_BRANCH" ] || { echo "batch state base branch mismatch" >&2; exit 1; }
    batch_cursor="$(jq -r '.cursor' <<<"$batch_json")"
    batch_count="$(jq -r '.issues | length' <<<"$batch_json")"
    if [ "$batch_cursor" -lt "$batch_count" ] && \
       [ "$(jq -r --argjson cursor "$batch_cursor" '.issues[$cursor].status' <<<"$batch_json")" = active ]; then
        batch_issue="$(jq -r --argjson cursor "$batch_cursor" '.issues[$cursor].issue' <<<"$batch_json")"
        child_state="$(jq -r --argjson cursor "$batch_cursor" '.issues[$cursor].childRunState // empty' <<<"$batch_json")"
        [ -n "$child_state" ] || {
            recovery_message "Batch issue #$batch_issue is active without a child review checkpoint; inspect its worktree, remote branch, PR, and ledger before explicitly bailing it."
            echo "Explicit bail command: python3 '$RUN_STATE_HELPER' batch-update --file '$BATCH_STATE_FILE' --issue '$batch_issue' --expected-status active --status bailed" >&2
            exit 1
        }
        child_json="$(python3 "$RUN_STATE_HELPER" show --file "$child_state")" || exit 1
        [ "$(jq -r '.issue' <<<"$child_json")" = "$batch_issue" ] && \
        [ "$(jq -r '.repo' <<<"$child_json")" = "$GH_REPO" ] && \
        [ "$(jq -r '.baseBranch' <<<"$child_json")" = "$BASE_BRANCH" ] || {
            recovery_message "Batch issue #$batch_issue does not match its child review checkpoint."
            exit 1
        }
        child_worktree="$(jq -r '.worktree' <<<"$child_json")"
        AGENT_LOOP_BATCH_PARENT_STATE_FILE="$BATCH_STATE_FILE" \
            "$SCRIPT_DIR/agent-loop.sh" --resume-run "$child_state" || {
            recovery_message "Current batch issue #$batch_issue did not resume to a safely finalized state."
            exit 1
        }
        python3 "$RUN_STATE_HELPER" batch-update --file "$BATCH_STATE_FILE" \
            --issue "$batch_issue" --expected-status active \
            --status finalized >/dev/null || exit 1
        git worktree remove "$child_worktree" || {
            echo "warning: finalized batch child worktree cleanup failed and was preserved: $child_worktree" >&2
        }
        batch_json="$(python3 "$RUN_STATE_HELPER" batch-show --file "$BATCH_STATE_FILE")" || exit 1
        batch_cursor="$(jq -r '.cursor' <<<"$batch_json")"
    fi
    ISSUE_ALLOWLIST="$(jq -r --argjson cursor "$batch_cursor" '.allowlist[$cursor:] | map(tostring) | join(",")' <<<"$batch_json")"
    if [ -z "$ISSUE_ALLOWLIST" ]; then
        echo -e "${GREEN}■${NC} agent-loop batch already complete"
        exit 0
    fi
    remaining_count="$(jq -r --argjson cursor "$batch_cursor" '.allowlist[$cursor:] | length' <<<"$batch_json")"
    [ "$MAX_ITERATIONS" -le "$remaining_count" ] || MAX_ITERATIONS="$remaining_count"
elif [[ "$ISSUE_ALLOWLIST" == *,* ]] && [ "$DRY_RUN" = false ] && \
     [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
    mkdir -p "$LOG_ROOT"
    chmod 700 "$LOG_ROOT"
    safe_repo="${REPO_NAME//[^A-Za-z0-9._-]/-}"
    BATCH_STATE_FILE="$LOG_ROOT/$safe_repo-batch-$RUN_TAG.json"
    python3 "$RUN_STATE_HELPER" batch-create --file "$BATCH_STATE_FILE" \
        --run-id "$RUN_TAG" --repo "$GH_REPO" --base-branch "$BASE_BRANCH" \
        --issues "$ISSUE_ALLOWLIST" >/dev/null || exit 1
    acquire_batch_lock "$BATCH_STATE_FILE" || exit 1
    echo "   Batch recovery state: $BATCH_STATE_FILE"
fi

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
echo "     Claude review hook: $CLAUDE_REVIEW_HOOK"
echo "     Codex review hook: $CODEX_REVIEW_HOOK"
echo "     convergence cap: $REVIEW_MAX_ROUNDS round(s)"

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
    dependency_status=0
    check_dependencies "$SELECTED_BODY" || dependency_status=$?
    if [ "$dependency_status" -ne 0 ]; then
        if [ "$dependency_status" -eq 1 ]; then
            echo -e "${YELLOW}○${NC} Issue #$SELECTED_ID blocked by dependency gate"
            ACTIVE_WORKTREE=""
            if [ -n "$BATCH_STATE_FILE" ]; then
                recovery_message "Ordered batch stopped at dependency-blocked issue #$SELECTED_ID."
                print_batch_bail_command "$SELECTED_ID" pending
                exit 1
            fi
            continue
        fi
        echo "dependency gate failed for issue #$SELECTED_ID" >&2
        ACTIVE_WORKTREE=""
        exit 1
    fi
    echo "   Worktree: $ACTIVE_WORKTREE"
    echo "   Branch: $branch"
    echo "   Setup hook: ${SETUP_HOOK:-<none>}"
    echo "   Publication: open draft PR before review; PR base $BASE_BRANCH"
    echo "   Review order: configured Codex hook -> configured Claude hook -> repeat only after material fixes"

    if [ "$DRY_RUN" = true ]; then
        echo -e "${GREEN}✓${NC} Dry-run only: no claim, worktree, hook, push, or PR mutation"
        ACTIVE_WORKTREE=""
        continue
    fi

    if [ -n "$BATCH_STATE_FILE" ]; then
        python3 "$RUN_STATE_HELPER" batch-update --file "$BATCH_STATE_FILE" \
            --issue "$SELECTED_ID" --expected-status pending \
            --status active >/dev/null || exit 1
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

    claim_status=0
    claim_issue "$SELECTED_ID" || claim_status=$?
    if [ "$claim_status" -ne 0 ]; then
        if [ -n "$BATCH_STATE_FILE" ]; then
            recovery_message "Batch claim for issue #$SELECTED_ID did not complete safely."
            exit 1
        fi
        echo -e "${YELLOW}○${NC} Issue #$SELECTED_ID could not be claimed; skipping"
        rmdir "$proposed_log_dir" 2>/dev/null || true
        ACTIVE_WORKTREE=""
        [ "$claim_status" -eq 2 ] && exit 1
        continue
    fi

    refreshed_status=0
    ready_queue_contains_issue "$SELECTED_ID" || refreshed_status=$?
    if [ "$refreshed_status" -eq 0 ] && [ "$DEPENDENCY_GATE" = merged-to-base ]; then
        check_dependencies "$SELECTED_BODY" || refreshed_status=$?
    fi
    if [ "$refreshed_status" -ne 0 ]; then
        if [ "$refreshed_status" -eq 1 ]; then
            echo -e "${YELLOW}○${NC} Issue #$SELECTED_ID is no longer ready after claim verification"
        else
            echo "could not re-evaluate issue #$SELECTED_ID after claim verification" >&2
        fi
        if [ "$SELECTED_ASSIGNED" = false ] && ! rollback_new_claim "$SELECTED_ID"; then
            rmdir "$proposed_log_dir" 2>/dev/null || true
            ACTIVE_WORKTREE=""
            exit 1
        fi
        rmdir "$proposed_log_dir" 2>/dev/null || true
        ACTIVE_WORKTREE=""
        if [ -n "$BATCH_STATE_FILE" ]; then
            recovery_message "Batch issue #$SELECTED_ID lost eligibility after claim verification; explicitly bail it before continuing."
            print_batch_bail_command "$SELECTED_ID" active
            exit 1
        fi
        [ "$refreshed_status" -eq 2 ] && exit 1
        continue
    fi
    AGENT_LOOP_LOG_DIR="$proposed_log_dir"
    # Never let the issue branch inherit origin/<base> as its upstream. With
    # push.default=upstream, a bare `git push` from a worker/reviewer would
    # otherwise target the integration branch and bypass local review.
    git worktree add --no-track -b "$branch" "$ACTIVE_WORKTREE" "$BASE_REMOTE_REF"
    cd "$ACTIVE_WORKTREE"

    export AGENT_LOOP_ISSUE_ID="$SELECTED_ID"
    # Ordinary gh commands are masked, so the worker cannot fetch its own issue
    # over the API. Hand the title and body to it directly instead.
    export AGENT_LOOP_ISSUE_TITLE="$SELECTED_TITLE"
    export AGENT_LOOP_ISSUE_BODY="$SELECTED_BODY"
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
        setup_status="$(git status --porcelain)" || { recovery_message "Could not inspect Git status after setup."; exit 1; }
        [ -z "$setup_status" ] || { recovery_message "Setup hook left Git-visible worktree changes."; exit 1; }
        require_issue_branch_head || { recovery_message "Setup hook moved HEAD away from the issue branch."; exit 1; }
        setup_after_sha="$(git rev-parse HEAD)" || { recovery_message "Could not inspect HEAD after setup."; exit 1; }
        [ "$setup_after_sha" = "$start_sha" ] || { recovery_message "Setup hook changed HEAD; setup hooks must not commit."; exit 1; }
    fi

    run_worker "$start_sha" || exit 1
    export AGENT_LOOP_REVIEW_PUSH_HELPER="$REVIEW_PUSH_HELPER"
    require_clean_committed_tree "Worker" "$start_sha" || exit 1
    run_validation "worker" || { recovery_message "Worker validation failed."; exit 1; }

    echo -e "${BLUE}▸${NC} Initial fresh-base integration"
    fetch_base
    initial_base_sha="$(git rev-parse "$BASE_REMOTE_REF")"
    if ! git merge --no-edit "$initial_base_sha"; then
        git merge --abort >/dev/null 2>&1 || true
        recovery_message "Initial fresh-base merge conflicted; original commits were preserved."
        exit 1
    fi
    inspect_publication_diff "$initial_base_sha" || {
        recovery_message "Initial publication diff inspection failed."
        exit 1
    }
    run_validation "initial-fresh-base" || {
        recovery_message "Initial fresh-base validation failed."
        exit 1
    }

    publication_readiness_status=0
    verify_issue_for_publication "$SELECTED_ID" || publication_readiness_status=$?
    if [ "$publication_readiness_status" -ne 0 ]; then
        recovery_message "Issue requirements or readiness changed before draft PR creation; completed work was preserved and the claim was retained."
        exit 1
    fi
    if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
        recovery_message "Remote branch existed before draft PR creation."
        exit 1
    fi
    initial_pr_sha="$(git rev-parse HEAD)"
    open_draft_pr "$SELECTED_ID" "$branch" "$initial_pr_sha" "$initial_base_sha"

    if [ "$REVIEW_CONTRACT_VERSION" = 3 ]; then
        AGENT_LOOP_RUN_STATE_FILE="$AGENT_LOOP_LOG_DIR/run-state.json"
        python3 "$RUN_STATE_HELPER" create --file "$AGENT_LOOP_RUN_STATE_FILE" \
            --run-id "$RUN_TAG-issue-$SELECTED_ID" --repo "$GH_REPO" \
            --issue "$SELECTED_ID" --base-branch "$BASE_BRANCH" \
            --issue-title-sha256 "$(printf '%s' "$SELECTED_TITLE" | sha256_text)" \
            --issue-body-sha256 "$(printf '%s' "$SELECTED_BODY" | sha256_text)" \
            --branch "$branch" --worktree "$ACTIVE_WORKTREE" \
            --log-dir "$AGENT_LOOP_LOG_DIR" --pr "$AGENT_LOOP_PR_NUMBER" \
            --pr-url "$AGENT_LOOP_PR_URL" --base-sha "$initial_base_sha" \
            --head-sha "$initial_pr_sha" >/dev/null || {
            recovery_message "Could not create the private review run-state checkpoint."
            exit 1
        }
        acquire_run_lock "$AGENT_LOOP_LOG_DIR" || {
            recovery_message "Could not acquire the private review run lock."
            exit 1
        }
        if [ -n "$BATCH_STATE_FILE" ]; then
            python3 "$RUN_STATE_HELPER" batch-update --file "$BATCH_STATE_FILE" \
                --issue "$SELECTED_ID" --expected-status active --status active \
                --child-run-state "$AGENT_LOOP_RUN_STATE_FILE" >/dev/null || {
                recovery_message "Could not attach the child review checkpoint to batch state."
                exit 1
            }
        fi
        echo "   Review recovery state: $AGENT_LOOP_RUN_STATE_FILE"
    fi

    export AGENT_LOOP_REVIEW_BASE="$BASE_REMOTE_REF"
    REVIEW_ROUNDS_USED=0
    REVIEWED_BASE_SHA=""
    CONVERGED_CODEX_OUTCOME_FILE=""
    CONVERGED_CODEX_OUTCOME_SIGNATURE=""
    CONVERGED_CLAUDE_OUTCOME_FILE=""
    CONVERGED_CLAUDE_OUTCOME_SIGNATURE=""
    # Keep this as a simple command instead of placing the function in an `||`
    # context. Bash disables `errexit` throughout a function invoked from an
    # AND/OR list, which would let an unguarded fetch or git query fail open.
    run_review_convergence 1

    inspect_publication_diff "$REVIEWED_BASE_SHA" || {
        recovery_message "Final reviewed diff inspection failed."
        exit 1
    }
    run_validation "final-reviewed-head" || {
        recovery_message "Final reviewed-head validation failed."
        exit 1
    }
    attest_review_head "after final reviewed-head validation" "$REVIEWED_BASE_SHA" || {
        recovery_message "Final reviewed-head attestation failed."
        exit 1
    }
    verify_converged_review_outcomes || exit 1
    verify_local_review_threads || exit 1

    publication_readiness_status=0
    verify_issue_for_publication "$SELECTED_ID" "$AGENT_LOOP_PR_NUMBER" || publication_readiness_status=$?
    if [ "$publication_readiness_status" -ne 0 ]; then
        recovery_message "Issue requirements or readiness changed before publication; completed work was preserved and the claim was retained."
        exit 1
    fi
    finalize_pr

    cd "$PROJECT_DIR"
    if [ "${AGENT_INTERRUPT_AFTER_CHILD_FINALIZED:-0}" = 1 ]; then
        recovery_message "Synthetic interruption after child finalization checkpoint."
        exit 92
    fi
    if [ -n "$BATCH_STATE_FILE" ]; then
        python3 "$RUN_STATE_HELPER" batch-update --file "$BATCH_STATE_FILE" \
            --issue "$SELECTED_ID" --expected-status active \
            --status finalized >/dev/null || {
            recovery_message "Issue finalized but the batch cursor could not be checkpointed; the worktree was preserved."
            exit 1
        }
    fi
    git worktree remove "$ACTIVE_WORKTREE" || {
        echo "warning: finalized issue worktree cleanup failed and was preserved: $ACTIVE_WORKTREE" >&2
    }
    ACTIVE_WORKTREE=""
    echo -e "${GREEN}✓${NC} Issue #$SELECTED_ID complete; local branch retained at $branch"
done

if [ -n "$BATCH_STATE_FILE" ]; then
    batch_json="$(python3 "$RUN_STATE_HELPER" batch-show --file "$BATCH_STATE_FILE")" || exit 1
    batch_cursor="$(jq -r '.cursor' <<<"$batch_json")"
    batch_count="$(jq -r '.issues | length' <<<"$batch_json")"
    if [ "$batch_cursor" -lt "$batch_count" ]; then
        if [ "$ITERATION" -ge "$MAX_ITERATIONS" ]; then
            echo -e "${YELLOW}○${NC} Ordered batch paused cleanly at the $MAX_ITERATIONS-issue iteration cap."
            echo "Resume batch with: '$SCRIPT_DIR/agent-loop.sh' --resume-batch '$BATCH_STATE_FILE'"
            exit 0
        fi
        recovery_message "Ordered batch stopped before every issue reached a finalized or explicitly bailed state."
        exit 1
    fi
fi

if [ "$ITERATION" -eq 0 ]; then
    echo -e "${DIM}○${NC} No selectable issues."
fi
echo -e "${GREEN}■${NC} agent-loop finished after $ITERATION issue(s)"
