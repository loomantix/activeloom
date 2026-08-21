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

command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "gh is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

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
command -v "$agy_review_cli" >/dev/null 2>&1 || { echo "agy is required" >&2; exit 1; }

temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT
chmod 700 "$temp_dir"
skills_file="$temp_dir/skills.json"
result_file="$temp_dir/result.json"

"$agy_review_cli" \
    --model gemini-3.7-flash-high \
    --effort high \
    --output-format json \
    --print '/skills' >"$skills_file"

agy_surface_root="$(python3 - "$skills_file" <<'PY'
import json
import pathlib
import sys

try:
    payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"agy skill preflight returned invalid JSON: {error}")

if payload.get("status") != "SUCCESS":
    raise SystemExit(f"agy skill preflight did not succeed: {payload.get('status', 'missing status')}")

skills = payload.get("command", {}).get("data", {}).get("skills", [])
matches = [row for row in skills if row.get("name") == "deepcritique"]
if len(matches) != 1:
    raise SystemExit("agy must resolve exactly one deepcritique skill")

display_path = pathlib.Path(str(matches[0].get("path", "")))
try:
    path = display_path.resolve(strict=True)
except OSError as error:
    raise SystemExit(f"agy deepcritique skill path cannot be resolved: {error}")
if path.name != "SKILL.md" or path.parent.name != "deepcritique" or any(".bak." in part for part in path.parts):
    raise SystemExit(f"agy resolved a stale or unexpected deepcritique skill: {path}")

surface = path.parent.parent.parent
required = [
    surface / "references/local-review-ledger.md",
    surface / "references/roles/code-reviewer.md",
    surface / "references/roles/silent-failure-hunter.md",
    surface / "references/roles/type-design-analyzer.md",
    surface / "references/roles/comment-analyzer.md",
    surface / "references/roles/pr-test-analyzer.md",
    surface / "references/roles/security-reviewer.md",
    surface / "skills/critique/SKILL.md",
    surface / "skills/critique/scripts/review-ledger.js",
    surface / "skills/refactorpass/SKILL.md",
]
missing = [str(candidate) for candidate in required if not candidate.is_file()]
if missing:
    raise SystemExit(f"agy deepcritique relay surface is incomplete: {', '.join(missing)}")

deep_contract = path.read_text(encoding="utf-8")
critique_contract = (surface / "skills/critique/SKILL.md").read_text(encoding="utf-8")
for capability in ("AGENT_LOOP_REVIEW_ENGINE", "AGENT_LOOP_REVIEW_BASE_SHA", "write-result", "gemini", "antigravity"):
    if capability not in deep_contract:
        raise SystemExit(f"agy deepcritique skill lacks required capability: {capability}")
for capability in ("AGENT_LOOP_REVIEW_ENGINE", "write-result", ".agents/references/local-review-ledger.md"):
    if capability not in critique_contract:
        raise SystemExit(f"agy critique skill lacks required capability: {capability}")

surface_text = str(surface)
if not surface.is_absolute() or "\n" in surface_text or "\r" in surface_text:
    raise SystemExit("agy relay surface path is unsafe")
print(surface_text)
PY
)"

prompt="/deepcritique ${pr}

Continue review on PR #${pr} in ${repo}.

This is automatic local-convergence mode. Run a fresh Gemini deepcritique pass
for round ${round} against the pinned base ${base} and exact reviewed head
${head}. Use gemini as the active local-review engine identity. The compatible
Agy relay surface is ${agy_surface_root}; use absolute paths under that root for
every .agents skill, reference, role, and ledger helper instead of assuming the
review worktree contains a .agents directory. Reconstruct context from the PR
description, commits, diff, checks, and complete local-review ledger, including
resolved threads and prior attestations. Post verified findings inline before
edits, then validate, push, reply, resolve, and publish the normal review result.
Do not invoke Codex; return control to the calling Codex session when the Gemini
pass is complete."

export AGENT_LOOP_REVIEW_BASE_SHA="$base"
export AGENT_LOOP_REVIEW_ROUND="$round"
export AGENT_LOOP_REVIEW_ENGINE="gemini"

set +e
"$agy_review_cli" \
    --model gemini-3.7-flash-high \
    --effort high \
    --mode accept-edits \
    --dangerously-skip-permissions \
    --add-dir "$agy_surface_root" \
    --output-format json \
    --print-timeout 60m \
    --print "$prompt" >"$result_file"
agy_exit="$?"
set -e

python3 - "$result_file" "$agy_exit" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
exit_code = int(sys.argv[2])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"agy review returned invalid JSON (exit {exit_code}): {error}")

status = payload.get("status")
if exit_code != 0 or status != "SUCCESS":
    message = payload.get("response") or payload.get("error") or "no error detail"
    raise SystemExit(f"agy review failed (exit {exit_code}, status {status!r}): {message}")

response = payload.get("response")
if not isinstance(response, str):
    raise SystemExit("agy review succeeded without a text response")
print(response)
PY
