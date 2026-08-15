#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: $0 --repo OWNER/REPO --pr NUMBER --base SHA --round NUMBER" >&2
    exit 2
}

repo=""
pr=""
base=""
round=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) [ "$#" -ge 2 ] || usage; repo="$2"; shift 2 ;;
        --pr) [ "$#" -ge 2 ] || usage; pr="$2"; shift 2 ;;
        --base) [ "$#" -ge 2 ] || usage; base="$2"; shift 2 ;;
        --round) [ "$#" -ge 2 ] || usage; round="$2"; shift 2 ;;
        *) usage ;;
    esac
done

[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage
[[ "$pr" =~ ^[1-9][0-9]*$ ]] || usage
[[ "$base" =~ ^[0-9a-f]{40}$ ]] || usage
[[ "$round" =~ ^[1-9][0-9]*$ ]] || usage

claude_review_cli="${CLAUDE_REVIEW_CLI:-claude}"
prompt="/deepcritique ${pr}

Continue review on PR #${pr} in ${repo}.

This is automatic local-convergence mode. Run a fresh Claude deepcritique pass
for round ${round} against the pinned base ${base}. Reconstruct context from the
PR description, commits, diff, checks, and complete local-review ledger,
including resolved threads and prior attestations. Post verified findings inline
before edits, then validate, push, reply, resolve, and publish the normal review
result. Do not invoke Codex; return control to the calling Codex session when
the Claude pass is complete."

export AGENT_LOOP_REVIEW_BASE_SHA="$base"
export AGENT_LOOP_REVIEW_ROUND="$round"
export AGENT_LOOP_REVIEW_ENGINE="claude"

exec "$claude_review_cli" \
    --effort low \
    --permission-mode bypassPermissions \
    --no-session-persistence \
    --print \
    "$prompt"
