#!/usr/bin/env bash
set -euo pipefail
# Bash imports `monitor` from an exported SHELLOPTS. With job control on,
# each background job leads its own process group, so the `setsid` below
# forks instead of exec'ing: `$!` would name a wrapper that exits at once,
# leaving the real reviewer detached and unreachable by the cancellation path.
set +m
umask 077

usage() {
    echo "usage: $0 --repo OWNER/REPO --pr NUMBER --base SHA --head SHA --round NUMBER" >&2
    exit 2
}

repo=""
pr=""
base=""
head=""
round=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) [ "$#" -ge 2 ] || usage; repo="$2"; shift 2 ;;
        --pr) [ "$#" -ge 2 ] || usage; pr="$2"; shift 2 ;;
        --base) [ "$#" -ge 2 ] || usage; base="$2"; shift 2 ;;
        --head) [ "$#" -ge 2 ] || usage; head="$2"; shift 2 ;;
        --round) [ "$#" -ge 2 ] || usage; round="$2"; shift 2 ;;
        *) usage ;;
    esac
done

[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage
[[ "$pr" =~ ^[1-9][0-9]*$ ]] || usage
[[ "$base" =~ ^[0-9a-f]{40}$ ]] || usage
[[ "$head" =~ ^[0-9a-f]{40}$ ]] || usage
[[ "$round" =~ ^[1-9][0-9]*$ ]] || usage

for tool in git gh python3 setsid timeout; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done

# Pinned loomantix/gemini-platform commit whose `.agents` tree is the only
# relay surface this launcher exposes to the unattended reviewer. Bumping it
# also requires updating AGY_SURFACE_SHA in tests/test_agy_review_launcher.py.
agy_surface_sha="3d7ad7c6d1e088faca88d52490bda1f45ce7e1fd"

# The companion surface must speak this engine's ledger protocol. Reading the
# expectation from the vendored version file beside this script keeps the two
# in step through a ledger bump instead of pinning the number twice.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ledger_version_file="$script_dir/review-ledger.version"
[ -f "$ledger_version_file" ] || { echo "vendored review-ledger.version is missing" >&2; exit 1; }
ledger_version="$(tr -d '[:space:]' <"$ledger_version_file")"
[ -n "$ledger_version" ] || { echo "vendored review-ledger.version is empty" >&2; exit 1; }

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

agy_review_cli="${AGY_REVIEW_CLI:-agy}"
command -v "$agy_review_cli" >/dev/null 2>&1 || { echo "the Antigravity CLI is required" >&2; exit 1; }

temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT
chmod 700 "$temp_dir"
skills_file="$temp_dir/skills.json"
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

# The outer bound stays above --print-timeout so the CLI's own timeout fires
# first and still writes a structured payload for the parser below.
skills_exit=0
run_agy_managed "$skills_file" 2m \
    --model gemini-3.7-flash-high \
    --effort high \
    --output-format json \
    --print-timeout 90s \
    --print '/skills' || skills_exit="$?"

agy_surface_root="$(python3 - "$skills_file" "$ledger_version" "$skills_exit" <<'PY'
import json
import pathlib
import sys

exit_code = int(sys.argv[3])
try:
    payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"skill preflight returned invalid JSON (exit {exit_code}): {error}")

if not isinstance(payload, dict):
    raise SystemExit(f"skill preflight returned non-dict JSON (exit {exit_code})")

if exit_code != 0 or payload.get("status") != "SUCCESS":
    status = payload.get("status", "missing status")
    raise SystemExit(f"skill preflight did not succeed (exit {exit_code}): {status}")

expected_ledger_version = sys.argv[2]
skills = payload.get("command", {}).get("data", {}).get("skills", [])
matches = [row for row in skills if row.get("name") == "deepcritique"]
if len(matches) != 1:
    raise SystemExit("the reviewer must resolve exactly one deepcritique skill")

try:
    path = pathlib.Path(str(matches[0].get("path", ""))).resolve(strict=True)
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
)"

# Git runs programs named by a repository's own configuration, such as
# core.fsmonitor. The surface path is nominated by the reviewer CLI and is
# untrusted until the provenance checks below pass, so neutralize that
# configuration on every command: the pin decides, not the candidate's own
# config.
surface_git() {
    local dir="$1"
    shift
    git -c core.fsmonitor= -c core.hooksPath=/dev/null \
        -c core.excludesFile=/dev/null --no-optional-locks \
        -C "$dir" "$@"
}

agy_surface_repo="$(surface_git "$agy_surface_root" rev-parse --show-toplevel)"
[ "$agy_surface_root" = "$agy_surface_repo/.agents" ] || {
    echo "the relay surface must be the .agents directory of its trusted checkout" >&2
    exit 1
}
agy_surface_remote="$(surface_git "$agy_surface_repo" remote get-url origin)"
case "$agy_surface_remote" in
    https://github.com/loomantix/gemini-platform.git|git@github.com:loomantix/gemini-platform.git) ;;
    *) echo "the relay surface has an untrusted Git remote" >&2; exit 1 ;;
esac
[ "$(surface_git "$agy_surface_repo" rev-parse HEAD)" = "$agy_surface_sha" ] || {
    echo "the relay surface is not at the pinned gemini-platform commit $agy_surface_sha" >&2
    exit 1
}
[ -z "$(surface_git "$agy_surface_repo" status --porcelain)" ] || {
    echo "the relay surface checkout must be clean" >&2
    exit 1
}

prompt="/deepcritique ${pr}

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
calling Claude session when the Gemini pass is complete."

export AGENT_LOOP_REVIEW_BASE_SHA="$base"
export AGENT_LOOP_REVIEW_ROUND="$round"
export AGENT_LOOP_REVIEW_ENGINE="gemini"

agy_exit=0
# 61m outer bound against the 60m --print-timeout below, for the same reason as
# the skill preflight: let the CLI time out and report rather than be killed.
# claude-cli-invocations:start
run_agy_managed "$result_file" 61m \
    --model gemini-3.7-flash-high \
    --effort high \
    --mode accept-edits \
    --dangerously-skip-permissions \
    --add-dir "$agy_surface_root" \
    --output-format json \
    --print-timeout 60m \
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
    message = payload.get("response") or payload.get("error") or "no error detail"
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
