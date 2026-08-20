---
name: pr-critique
description: Cross-engine deep review of an existing PR. Run when you want a second engine's deep adversarial pass on a PR another engine (or you) already opened — typically after the authoring engine's own critique/reviewit. Runs the deep review matrix on the PR diff, applies fixes, and pushes them back to the PR head branch so the originating engine can re-review.
---

# PR Critique — cross-engine relay review

Run the deep review matrix against an **already-open PR** as a different engine
from the one that authored it, fix what you find, and push the fixes back so the
originating engine can re-review. The value is engine diversity: a second model
catches design-level blind spots the authoring engine baked in and would not
question on its own. The hand-back is the point — `pr-critique` is one leg of a
round trip, not a terminal review.

Load `.codex/references/local-review-ledger.md`. The originating engine's
resolved threads are required input to this pass, not optional background.

This is **not** `deepcritique`. Both are PR-first and use the same thread ledger,
but `deepcritique` runs the current engine's deep matrix, preceded by `refactorpass`
on that engine's first pass over the PR. `pr-critique` is the cross-engine relay
leg: it never runs `refactorpass`, scrutinizes the prior engine's design
decisions, and pushes signed fix commits back to the PR head branch.

A relay leg is still a round for the engine running it. Resolve the round and
stance per the ledger's "Resolve the round, then pick the stance" — from round 3
on, a relay pass narrows to the deploy-blocking lanes, changes the PR only for a
blocking defect, and defers the rest to linked issues.

## Safety preconditions — verify before doing anything

1. **Own-branch only.** This skill pushes. Refuse to run if the checked-out
   branch is `main`, `master`, or `staging`, or is the PR's **base** branch.
   You may only push to the PR's **head** branch.
2. **Confirm the head branch is the current branch at the exact PR head.** If
   the working tree is not on the PR head (e.g. the PR was not fetched into this
   worktree), stop and print the fetch recipe in Phase 0 rather than guessing.
3. **Never force-push.** A plain push only. If the push is rejected because the
   remote head moved, stop and report — do not `--force`.
4. **Never bypass commit signing.** Commit with the repo's normal signing
   config. Do not pass `--no-gpg-sign` or disable `commit.gpgsign`.

## Phase 0: Resolve the PR target

Take the PR number from the invocation (`pr-critique <pr-number>`).

If the PR is not already checked out in this worktree, stop and tell the user to
fetch it in isolation (do not switch branches in a shared checkout):

```bash
HEAD_BRANCH=$(gh pr view <pr-number> --json headRefName --jq .headRefName)
HEAD_REPO=$(gh pr view <pr-number> --json headRepository --jq .headRepository.nameWithOwner)
HEAD_REPO_URL="https://github.com/$HEAD_REPO.git"
git fetch "$HEAD_REPO_URL" \
  "refs/heads/$HEAD_BRANCH:refs/remotes/pr-<pr-number>/head"
git worktree add -b pr-<pr-number>-review ../review-pr-<pr-number> \
  refs/remotes/pr-<pr-number>/head
cd ../review-pr-<pr-number>
# re-run pr-critique <pr-number> here
```

Resolve the PR identity and review scope once. The immutable base SHA may be
supplied by the convergence wrapper as `$AGENT_LOOP_REVIEW_BASE_SHA`, as an
explicit `$PR_CRITIQUE_REVIEW_BASE_SHA`, or — during the rename transition — as
the legacy `$PR_GRILL_REVIEW_BASE_SHA`. There is no precedence between them:
resolve every non-empty override and fail closed when any pair names different
commits. Only a standalone invocation without an override may snapshot the
PR's current `baseRefOid`. Never let individual lanes re-resolve a mutable ref:

```bash
PR_DATA=$(gh pr view <pr-number> \
  --json baseRefName,baseRefOid,headRefName,headRefOid,headRepository)
BASE=$(jq -r .baseRefName <<<"$PR_DATA")
PR_HEAD_SHA=$(jq -r .headRefOid <<<"$PR_DATA")
HEAD_BRANCH=$(jq -r .headRefName <<<"$PR_DATA")
HEAD_REPO=$(jq -r .headRepository.nameWithOwner <<<"$PR_DATA")
HEAD_REPO_URL="https://github.com/$HEAD_REPO.git"
test "$(git rev-parse HEAD)" = "$PR_HEAD_SHA" || {
  echo "worktree is not at the PR head — fetch it in isolation first" >&2
  exit 1
}

REVIEW_BASE_SHA=
for candidate in "${AGENT_LOOP_REVIEW_BASE_SHA:-}" \
                 "${PR_CRITIQUE_REVIEW_BASE_SHA:-}" \
                 "${PR_GRILL_REVIEW_BASE_SHA:-}"; do
  [ -n "$candidate" ] || continue
  candidate=$(git rev-parse --verify "$candidate^{commit}") || exit 1
  if [ -n "$REVIEW_BASE_SHA" ] && [ "$REVIEW_BASE_SHA" != "$candidate" ]; then
    echo "review base overrides are set to different commits" >&2
    exit 1
  fi
  REVIEW_BASE_SHA=$candidate
done
if [ -z "$REVIEW_BASE_SHA" ]; then
  REVIEW_BASE_SHA=$(jq -r '.baseRefOid // empty' <<<"$PR_DATA")
  [ -n "$REVIEW_BASE_SHA" ] || { echo "PR baseRefOid is unavailable" >&2; exit 1; }
  git fetch -q origin "$BASE"
fi
REVIEW_BASE_SHA=$(git rev-parse --verify "$REVIEW_BASE_SHA^{commit}") || exit 1
RANGE="$REVIEW_BASE_SHA..HEAD"
```

Skip docs/config-only changesets (same heuristic as `critique`): if `git diff
--name-only "$RANGE"` contains no source files, report the skip and exit — there
is nothing for the matrix to find.

## Phase 1: Deep matrix on the PR diff

Run `critique`'s **deep** matrix against `$RANGE`: the six core lanes plus the
conditional tenant-coupling lane when customer-variable behavior is present. Load the lane prompts
from `critique`'s role references — do not re-author them:

- `.codex/references/roles/code-reviewer.md`
- `.codex/references/roles/silent-failure-hunter.md`
- `.codex/references/roles/type-design-analyzer.md`
- `.codex/references/roles/comment-analyzer.md`
- `.codex/references/roles/pr-test-analyzer.md`
- `.codex/references/roles/security-reviewer.md`

Run the lanes as independently as the active runtime permits; if subagents are
unavailable, run separate local passes and disclose the downgrade under `review
depth` in the output. Keep lane findings separate until all lanes complete, then
deduplicate by root cause.

Invoking `pr-critique` is an explicit request to use independent subagents for the
six core review lanes, plus the conditional tenant-coupling lane when signaled,
whenever the active runtime exposes subagent/delegation tools. Do not require the
user to separately say "use subagents" before spawning those lane reviewers.

Review lanes are read-only analysis workers. Every spawned lane prompt must say
that the lane may inspect source, diffs, existing tests, and existing CI results,
but must not run test suites, linters, formatters, builds, coverage, package
installation, or CI polling. If dynamic evidence is necessary, the lane returns
the smallest proposed probe to the orchestrator instead of executing it. The
orchestrator alone runs one consolidated validation pass against the final head
after findings are deduplicated and any fixes are complete.

**Cross-engine emphasis.** You are reviewing another engine's work. Beyond
line-level bugs, scrutinize the _design decisions_ the author made and did not
question: chosen abstractions, latency/UX tradeoffs, removed or added special
cases, and whether a "fix" traded away a property the original code protected.
These are the findings a same-engine critique misses and the reason this pass
exists.

## Phase 2: Publish findings, then apply fixes

Verify and deduplicate every finding against the full PR ledger. Post one inline
comment per confirmed root cause before editing, using the local-review marker
and an exact diff anchor. Apply `critique`'s round-matched fix bias to `$RANGE`:
in adversarial rounds, fix every valid finding including nits and defer only
extremely large follow-ups; in convergence rounds, fix only blocking defects
and defer every confirmed non-blocker to a linked issue. Dismiss invalid
findings or suggestions that would make the code worse with the evidence that
disproves them. Critical correctness/security findings must not be silently
dropped. Run the smallest relevant formatter/test command the repo documents
for the files you touched.

## Phase 3: Commit and push (automatic)

If fixes were applied, commit and push without a confirmation gate — this skill
is for your own PR branches.

1. **Label the relay commit** so the cross-engine leg is auditable in `git log`:

   ```bash
   git add -A
   git commit -m "fix(pr-critique): <one-line summary of what the cross-engine pass changed>" \
     --trailer "Cross-engine-review: pr-critique"
   ```

   Use the repo's normal signing config (do not disable it).

2. **Push explicitly to the PR head repository and branch** (plain push, no
   force). Reuse the immutable `HEAD_REPO_URL` and `HEAD_BRANCH` resolved in
   Phase 0:

   ```bash
   git push "$HEAD_REPO_URL" "HEAD:refs/heads/$HEAD_BRANCH"
   ```

   If the push is rejected, stop and report the rejection — do not force-push or
   rebase silently. After a successful push, require both `git ls-remote` for
   that exact branch and `gh pr view --json headRefOid` to equal local `HEAD`.

3. Reply to every posted thread with the fix commit and validation result, then
   resolve it. For a dismissal or tracked deferral, reply with the evidence or
   issue link before resolving. Stop if any thread cannot be replied to or
   resolved.

If no fixes were applied (clean, or everything deferred/dismissed), do not
commit or push. Report the clean result.

## Phase 4: Hand back

Follow the selected local session mode before returning. In auto mode, return to
the calling orchestrator so it can continue the bounded chain. In handoff mode,
post a `local-review-handoff:v1` comment with the `local-review-handoff.py`
helper. The comment
targets the originating engine, pins the current head and review base, and
contains the fresh-session prompt; the user starts it in a new terminal.

Print a summary aimed at the **originating engine's re-review** — it needs to
know what changed and what to scrutinize:

```text
pr-critique complete on PR #<pr-number> (cross-engine pass).
review depth: <deep with independent subagents | deep local multi-pass fallback>
findings fixed:    <count + one-line each>
design tradeoffs flagged: <any decisions the re-review should adjudicate — e.g. a fix
                           that simplified logic but changed a latency/UX property>
deferred / dismissed: <count + rationale>
threads:            <posted/replied/resolved counts>
validation run:    <commands + result>
pushed:            <yes: SHA on <head-branch> | no fixes — nothing pushed>
pinned review base: <full REVIEW_BASE_SHA>

If this pass pushed a fix it moved the head, invalidating every attestation
naming the superseded commit, including the authoring engine's. Those engines
re-run against this HEAD; engines that had not yet attested are unaffected. If
nothing was pushed, no attestation changed.
```

Always surface the design tradeoffs explicitly — the round trip only works if
the engine reviewing next knows where to look.

## What this skill does NOT do

- **Does not run `refactorpass`.** No cleanup-churn on a PR under cross-review.
- **Does not open or merge the PR**, and does not push to a base branch.
- **Does not force-push or rebase.** A rejected push is reported, not forced.
- **Does not replace the relay.** `reviewit` drives the hosted Gemini + Copilot
  lane, which runs alongside the local relay rather than instead of it.
  `pr-critique` is one reviewer's lane, not a terminal review.

## Source of truth

This skill lives upstream in `codex-platform` at `.codex/skills/pr-critique/`.
Synced into consumer repos; consumer edits are overwritten on the next sync —
make changes upstream.
