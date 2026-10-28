#!/usr/bin/env bash
set -euo pipefail
# Bash imports `monitor` from an exported SHELLOPTS. With job control on,
# each background job leads its own process group, so the `setsid` below
# forks instead of exec'ing: `$!` would name a wrapper that exits at once,
# leaving the real reviewer detached and unreachable by the cancellation path.
set +m
umask 077

usage() {
    echo "usage: $0 --repo OWNER/REPO --pr NUMBER --base SHA --head SHA --round NUMBER [--preflight-only] [--repair-surface]" >&2
    exit 2
}

repo=""
pr=""
base=""
head=""
round=""
preflight_only=false
repair_surface=false

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) [ "$#" -ge 2 ] || usage; repo="$2"; shift 2 ;;
        --pr) [ "$#" -ge 2 ] || usage; pr="$2"; shift 2 ;;
        --base) [ "$#" -ge 2 ] || usage; base="$2"; shift 2 ;;
        --head) [ "$#" -ge 2 ] || usage; head="$2"; shift 2 ;;
        --round) [ "$#" -ge 2 ] || usage; round="$2"; shift 2 ;;
        --preflight-only) preflight_only=true; shift ;;
        --repair-surface) repair_surface=true; shift ;;
        *) usage ;;
    esac
done

[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage
[[ "$pr" =~ ^[1-9][0-9]*$ ]] || usage
[[ "$base" =~ ^[0-9a-f]{40}$ ]] || usage
[[ "$head" =~ ^[0-9a-f]{40}$ ]] || usage
[[ "$round" =~ ^[1-9][0-9]*$ ]] || usage
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
launch_state() { python3 -I "$script_dir/review-launch-state.py" "$@"; }
launch_state preflight missing_tool

review_timeout_seconds="${LOCAL_REVIEW_PASS_TIMEOUT_SECONDS:-3600}"
[[ "$review_timeout_seconds" =~ ^[1-9][0-9]*$ ]] && \
    [ "$review_timeout_seconds" -le 3600 ] || {
    echo "LOCAL_REVIEW_PASS_TIMEOUT_SECONDS must be an integer from 1 through 3600" >&2
    exit 2
}

for tool in git gh python3 setsid timeout; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done

# Pinned loomantix/activeloom commit whose `.agents` tree is the only
# relay surface this launcher exposes to the unattended reviewer. Bumping it
# also requires updating AGY_SURFACE_SHA in tests/test_agy_review_launcher.py.
agy_surface_sha="bc93151fa8becd6b4986cb6a1d959a7afaf16565"

# The companion surface must speak this engine's ledger protocol. Reading the
# expectation from the vendored version file beside this script keeps the two
# in step through a ledger bump instead of pinning the number twice.
ledger_version_file="$script_dir/review-ledger.version"
[ -f "$ledger_version_file" ] || { echo "vendored review-ledger.version is missing" >&2; exit 1; }
ledger_version="$(tr -d '[:space:]' <"$ledger_version_file")"
[ -n "$ledger_version" ] || { echo "vendored review-ledger.version is empty" >&2; exit 1; }

launch_state preflight pr_boundary
current_repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
actor="$(gh api user --jq .login)"
pr_row="$(
    gh pr view "$pr" --repo "$repo" \
        --json author,headRefName,headRefOid,headRepository \
        --jq '[.headRefOid,.headRefName,.headRepository.nameWithOwner,.author.login] | @tsv'
)"
IFS=$'\t' read -r pr_head pr_branch pr_head_repo pr_author <<< "$pr_row"
local_head="$(git rev-parse HEAD)"
remote_row="$(git ls-remote --exit-code origin "refs/heads/$pr_branch")"
remote_head="${remote_row%%[[:space:]]*}"

[ "$current_repo" = "$repo" ] || { echo "current repository does not match --repo" >&2; exit 1; }
[ "$pr_head_repo" = "$repo" ] || { echo "PR head must be in the requested repository" >&2; exit 1; }
[ "$pr_author" = "$actor" ] || { echo "PR must be authored by the authenticated GitHub actor" >&2; exit 1; }
[ "$local_head" = "$head" ] || { echo "local HEAD does not match --head" >&2; exit 1; }
[ "$pr_head" = "$head" ] || { echo "PR head does not match --head" >&2; exit 1; }
[ "$remote_head" = "$head" ] || { echo "remote branch head does not match --head" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "review worktree must be clean" >&2; exit 1; }

launch_state preflight missing_tool
agy_review_cli="${AGY_REVIEW_CLI:-agy}"
command -v "$agy_review_cli" >/dev/null 2>&1 || { echo "the Antigravity CLI is required" >&2; exit 1; }

# Model and effort come from the user's review profile, or from the settings a
# review-chain run pinned when it started. The launch region below resolves
# them again under its hash.
launch_state preflight review_profile
agy_settings="$(python3 -I "$script_dir/review-profile.py" launch-args --engine gemini --repo "$repo")"
agy_model="${agy_settings%%$'\n'*}"
agy_effort="${agy_settings#*$'\n'}"

temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT
chmod 700 "$temp_dir"
result_file="$temp_dir/result.json"
agy_pid=""

forward_signal() {
    local signal="$1"
    local exit_code="$2"
    local target_pid="$agy_pid"
    trap - INT TERM HUP
    # Bash registers an asynchronous job before returning from the launch
    # command, but a pending trap may run before the following `$!` assignment.
    # In that narrow window the jobs table is the authoritative PID source.
    if [ -z "$target_pid" ]; then
        target_pid="$(jobs -pr | head -n 1)"
    fi
    if [ -n "$target_pid" ] && kill -0 -- "-$target_pid" 2>/dev/null; then
        kill -s "$signal" -- "-$target_pid" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 -- "-$target_pid" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 -- "-$target_pid" 2>/dev/null; then
            kill -KILL -- "-$target_pid" 2>/dev/null || true
        fi
        wait "$target_pid" 2>/dev/null || true
    fi
    exit "$exit_code"
}

run_agy_managed() {
    local output_file="$1"
    local limit="$2"
    shift 2
    set +e
    env -u ACTIVELOOM_AGY_USAGE_FILE -u ACTIVELOOM_ATTEMPT_ID \
        setsid timeout --signal=TERM --kill-after=5s "$limit" "$agy_review_cli" "$@" >"$output_file" &
    agy_pid="$!"
    wait "$agy_pid"
    local child_exit="$?"
    agy_pid=""
    set -e
    return "$child_exit"
}

trap 'forward_signal INT 130' INT
trap 'forward_signal TERM 143' TERM
trap 'forward_signal HUP 129' HUP

# Git runs programs named by a repository's own configuration, such as
# core.fsmonitor. A relay surface is untrusted until the provenance checks
# below pass, so neutralize that configuration on every command: the pin
# decides, not the candidate's own config.
surface_git() {
    local dir="$1"
    shift
    git -c core.fsmonitor= -c core.hooksPath=/dev/null \
        -c core.excludesFile=/dev/null --no-optional-locks \
        -C "$dir" "$@"
}

# The review-chain runner nominates its own clean checkout. Any other caller,
# such as /hreview, gets the same kind of checkout: a standalone clone of the
# canonical upstream at the pin, kept under this repository's Git directory
# and built once. Agy's own skill discovery is not consulted, because in a
# consumer it finds the review worktree's .agents copy, whose remote is the
# consumer. Building the checkout grants it no trust: the provenance checks
# below apply to it exactly as to a nominated surface.
build_managed_surface() {
    local dir="$1"
    surface_git "$dir" init -q &&
        surface_git "$dir" remote add origin "$agy_surface_upstream" &&
        surface_git "$dir" fetch -q --no-tags --depth=1 origin "$agy_surface_sha" &&
        surface_git "$dir" checkout -q --detach "$agy_surface_sha"
}

launch_state preflight managed_surface
agy_surface_upstream="https://github.com/loomantix/activeloom.git"
agy_review_surface="${ACTIVELOOM_REVIEW_SURFACE:-}"
managed_checkout=""
if [ -z "$agy_review_surface" ]; then
    git_common_dir="$(git rev-parse --git-common-dir)"
    managed_parent="$(cd -- "$git_common_dir" && pwd -P)/activeloom-review"
    for managed_dir in "$managed_parent" "$managed_parent/agy-surface"; do
        [ ! -L "$managed_dir" ] || { echo "managed relay surface path cannot be a symlink: $managed_dir" >&2; exit 1; }
        [ -d "$managed_dir" ] || mkdir -m 700 -- "$managed_dir" 2>/dev/null || [ -d "$managed_dir" ]
    done
    managed_parent="$managed_parent/agy-surface"
    managed_checkout="$managed_parent/$agy_surface_sha"
    if "$repair_surface" && { [ -e "$managed_checkout" ] || [ -L "$managed_checkout" ]; }; then
        preserved="$(mktemp -d "$managed_parent/$agy_surface_sha.preserved.XXXXXX")"
        mv -- "$managed_checkout" "$preserved/"
        echo "preserved the previous managed relay surface in $preserved" >&2
    fi
    if [ ! -e "$managed_checkout" ] && [ ! -L "$managed_checkout" ]; then
        staging="$(mktemp -d "$managed_parent/.staging.XXXXXX")"
        if ! build_managed_surface "$staging"; then
            rm -rf -- "$staging"
            echo "cannot build the managed relay surface at activeloom commit $agy_surface_sha" \
                "from $agy_surface_upstream; the first build needs network access" >&2
            exit 1
        fi
        # A concurrent launcher may have finished first. Its checkout faces
        # the same provenance checks, so losing the rename is not an error.
        python3 -I -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' \
            "$staging" "$managed_checkout" 2>/dev/null || rm -rf -- "$staging"
    fi
    [ ! -L "$managed_checkout" ] || { echo "managed relay surface cannot be a symlink: $managed_checkout" >&2; exit 1; }
    agy_review_surface="$managed_checkout/.agents"
fi

# Name the managed checkout and how to rebuild it whenever it is refused. The
# Python preflight prints its own reason, so it passes an empty message.
refuse_surface() {
    [ -z "$1" ] || echo "$1" >&2
    if [ -n "$managed_checkout" ]; then
        echo "The managed relay surface is $managed_checkout. Re-run with --repair-surface" \
            "to preserve it and rebuild it at the pin." >&2
    fi
    exit 1
}

agy_surface_root="$(python3 - "$agy_review_surface" "$ledger_version" <<'PY'
import json
import pathlib
import sys

nominated = pathlib.Path(sys.argv[1])
if not nominated.is_absolute() or nominated.is_symlink():
    raise SystemExit("the relay surface must be an absolute regular directory")
expected_ledger_version = sys.argv[2]

try:
    path = (nominated / "skills/deepcritique/SKILL.md").resolve(strict=True)
except OSError as error:
    raise SystemExit(f"the deepcritique skill path cannot be resolved: {error}")
if path.name != "SKILL.md" or path.parent.name != "deepcritique" or any(".bak." in part for part in path.parts):
    raise SystemExit(f"the reviewer resolved a stale or unexpected deepcritique skill: {path}")

surface = path.parent.parent.parent
required = [
    surface / "REVIEW_WORKFLOW.md",
    surface / "references/local-review-ledger.md",
    surface / "references/roles/code-reviewer.md",
    surface / "references/roles/silent-failure-hunter.md",
    surface / "references/roles/type-design-analyzer.md",
    surface / "references/roles/comment-analyzer.md",
    surface / "references/roles/pr-test-analyzer.md",
    surface / "references/roles/security-reviewer.md",
    surface / "skills/critique/SKILL.md",
    surface / "skills/critique/scripts/package.json",
    surface / "skills/critique/scripts/review-ledger.js",
    surface / "skills/critique/scripts/review-ledger.version",
    surface / "skills/critique/scripts/review-ledger.integrity",
    surface / "skills/refactorpass/SKILL.md",
]
missing = [str(candidate) for candidate in required if not candidate.is_file() or candidate.is_symlink()]
if missing:
    raise SystemExit(f"the deepcritique relay surface is incomplete: {', '.join(missing)}")

try:
    package = json.loads((surface / "skills/critique/scripts/package.json").read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"the relay review-ledger package manifest is invalid: {error}")
if package.get("type") != "module":
    raise SystemExit("the relay review-ledger package manifest must declare type=module")

surface_ledger_version = (
    (surface / "skills/critique/scripts/review-ledger.version").read_text(encoding="utf-8").strip()
)
if surface_ledger_version != expected_ledger_version:
    raise SystemExit(
        "the relay surface speaks review-ledger "
        f"{surface_ledger_version or 'an unreadable version'}, this engine vendors {expected_ledger_version}"
    )

deep_contract = path.read_text(encoding="utf-8")
critique_contract = (surface / "skills/critique/SKILL.md").read_text(encoding="utf-8")
for capability in ("AGENT_LOOP_REVIEW_ENGINE", "AGENT_LOOP_REVIEW_BASE_SHA", "write-result", "gemini", "antigravity"):
    if capability not in deep_contract:
        raise SystemExit(f"the relay deepcritique skill lacks required capability: {capability}")
for capability in ("write-result", ".agents/references/local-review-ledger.md"):
    if capability not in critique_contract:
        raise SystemExit(f"the relay critique skill lacks required capability: {capability}")

surface_text = str(surface)
if not surface.is_absolute() or "\n" in surface_text or "\r" in surface_text:
    raise SystemExit("the relay surface path is unsafe")
print(surface_text)
PY
)" || refuse_surface ""

launch_state preflight surface_provenance
agy_surface_repo="$(surface_git "$agy_surface_root" rev-parse --show-toplevel)"
[ "$agy_surface_root" = "$agy_surface_repo/.agents" ] || {
    refuse_surface "the relay surface must be the .agents directory of its trusted checkout"
}
agy_surface_remote="$(surface_git "$agy_surface_repo" remote get-url origin)"
case "$agy_surface_remote" in
    https://github.com/loomantix/activeloom.git|git@github.com:loomantix/activeloom.git) ;;
    *) refuse_surface "the relay surface has an untrusted Git remote" ;;
esac
[ "$(surface_git "$agy_surface_repo" rev-parse HEAD)" = "$agy_surface_sha" ] || {
    refuse_surface "the relay surface is not at the pinned activeloom commit $agy_surface_sha"
}
launch_state preflight dirty_surface
surface_changes="$(surface_git "$agy_surface_repo" status --porcelain)"
[ -z "$surface_changes" ] || {
    refuse_surface "$(printf 'the relay surface checkout must be clean: %s\n%s\n' "$agy_surface_repo" "$surface_changes")"
}
launch_state ready
if "$preflight_only"; then exit 0; fi

prompt="Read ${agy_surface_root}/skills/deepcritique/SKILL.md and follow it for this pass.

Continue review on PR #${pr} in ${repo}.

This is automatic local-convergence mode. Run a fresh Gemini deepcritique pass
for round ${round} against the pinned base ${base} and exact reviewed head
${head}. Use gemini as the active local-review engine identity. The compatible
relay surface is ${agy_surface_root}; use absolute paths under that root for
every .agents skill, reference, role, and ledger helper instead of assuming the
review worktree contains a .agents directory. Reconstruct context from the PR
description, commits, diff, checks, and complete local-review ledger, including
resolved threads and prior attestations. Post verified findings inline before
edits, then validate, push, reply, resolve, and publish the normal review result.
Write every scratch artifact under your own agent artifact directory rather than
a path outside it. Do not invoke Claude Code or Codex; return control to the
calling session when the Gemini pass is complete.
Do not spawn subagents or background review lanes in this Agy print-mode pass. Run each review lane sequentially in series within the primary session. In each lane pass, post verified findings inline, apply fixes, and validate before proceeding to the next lane so subsequent lanes review the updated code and prior findings. Run all commands and tests synchronously in the foreground; never leave background tasks running. Write the canonical result and end the turn only after all lanes and validation are complete."


export AGENT_LOOP_REVIEW_BASE_SHA="$base"
export AGENT_LOOP_REVIEW_ROUND="$round"
export AGENT_LOOP_REVIEW_ENGINE="gemini"

agy_exit=0
# Keep the outer bound above the --print-timeout below, for the same reason as
# the skill preflight: let the CLI time out and report rather than be killed.
# claude-cli-invocations:start
# The ceiling is re-asserted here, inside the locked region, deliberately.
# The fail-fast copy near the top of the script sits outside the hash, so an
# edit widening or deleting it would leave this launch unbounded without
# breaking the region hash. Re-checking here puts the bound itself under the
# fence; the duplication is the point, not an oversight.
[[ "$review_timeout_seconds" =~ ^[1-9][0-9]*$ ]] && \
    [ "$review_timeout_seconds" -le 3600 ] || {
    echo "LOCAL_REVIEW_PASS_TIMEOUT_SECONDS must be an integer from 1 through 3600" >&2
    exit 2
}
agy_outer_timeout_seconds=$((review_timeout_seconds + 30))
agy_settings="$(python3 -I "$script_dir/review-profile.py" launch-args --engine gemini --repo "$repo")"
agy_model="${agy_settings%%$'\n'*}"
agy_effort="${agy_settings#*$'\n'}"
launch_state execution
run_agy_managed "$result_file" "${agy_outer_timeout_seconds}s" \
    --model "$agy_model" \
    --effort "$agy_effort" \
    --mode accept-edits \
    --dangerously-skip-permissions \
    --add-dir "$agy_surface_root" \
    --output-format json \
    --print-timeout "${review_timeout_seconds}s" \
    --print "$prompt" || agy_exit="$?"
# claude-cli-invocations:end

python3 - "$result_file" "$agy_exit" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
exit_code = int(sys.argv[2])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"the review returned invalid JSON (exit {exit_code}): {error}")

if not isinstance(payload, dict):
    raise SystemExit(f"the review returned non-dict JSON (exit {exit_code})")

status = payload.get("status")
if exit_code != 0 or status != "SUCCESS":
    message = payload.get("error") or payload.get("response") or "no error detail"
    raise SystemExit(
        f"the review failed (exit {exit_code}, status {status!r}): {message}\n"
        "A narrated success, or a ledger comment the reviewer already posted, is not a passing "
        "result. This CLI reports a turn-level ERROR when any single tool call failed, so read "
        "the PR ledger directly to decide what actually landed, and re-run the round."
    )

response = payload.get("response")
if not isinstance(response, str) or not response.strip():
    raise SystemExit("the review succeeded without a text response")
print(response)
PY

# The final envelope exists only after the worker can no longer emit telemetry.
launch_state usage "$result_file"
