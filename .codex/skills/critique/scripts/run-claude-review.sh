#!/usr/bin/env bash
set -euo pipefail

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
review_timeout_seconds="${LOCAL_REVIEW_PASS_TIMEOUT_SECONDS:-2700}"
[[ "$review_timeout_seconds" =~ ^[1-9][0-9]*$ ]] && \
    [ "$review_timeout_seconds" -le 3600 ] || {
    echo "LOCAL_REVIEW_PASS_TIMEOUT_SECONDS must be an integer from 1 through 3600" >&2
    exit 2
}

command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "gh is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
command -v timeout >/dev/null 2>&1 || { echo "timeout is required" >&2; exit 1; }

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

script_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -I "$script_dir/local-review-handoff.py" authorize-pass \
    --repo "$repo" --pr "$pr" --base "$base" --head "$head" \
    --engine claude --round "$round" >/dev/null

claude_review_cli="${CLAUDE_REVIEW_CLI:-claude}"
prompt="/deepcritique ${pr}

Continue review on PR #${pr} in ${repo}.

This is automatic local-convergence mode. Run a fresh Claude deepcritique pass
for round ${round} against the pinned base ${base} and exact reviewed head
${head}. Reconstruct context from the PR description, commits, diff, checks,
and complete local-review ledger,
including resolved threads and prior attestations. Post verified findings inline
before edits, then validate, push, reply, resolve, and publish the normal review
result. This invocation owns exactly one Claude pass: do not invoke Codex,
Gemini, another reviewer, or any review launcher. Return control to the calling
Codex session when the Claude pass is complete."

export AGENT_LOOP_REVIEW_BASE_SHA="$base"
export AGENT_LOOP_REVIEW_ROUND="$round"
export AGENT_LOOP_REVIEW_ENGINE="claude"

exec timeout --signal=TERM --kill-after=30s "${review_timeout_seconds}s" \
    "$claude_review_cli" \
    --effort low \
    --permission-mode bypassPermissions \
    --no-session-persistence \
    --print \
    "$prompt"
