---
name: pr-grill
description: Cross-engine deep review of an existing PR. Run when you want a second engine's deep adversarial pass on a PR another engine (or you) already opened — typically after the authoring engine's own grill/reviewit. Runs the deep review matrix on the PR diff, applies fixes, and pushes them back to the PR head branch so the originating engine can re-review.
---

# PR Grill — cross-engine relay review

Run the deep review matrix against an **already-open PR** as a different engine
from the one that authored it, fix what you find, and push the fixes back so the
originating engine can re-review. The value is engine diversity: a second model
catches design-level blind spots the authoring engine baked in and would not
question on its own. The hand-back is the point — `pr-grill` is one leg of a
round trip, not a terminal review.

Load `.codex/references/local-review-ledger.md`. The originating engine's
resolved threads are required input to this pass, not optional background.

This is **not** `deepgrill`. Both are PR-first and use the same thread ledger,
but `deepgrill` runs `refactorpass` plus the current engine's deep matrix.
`pr-grill` is the cross-engine relay leg: it skips `refactorpass`, scrutinizes
the prior engine's design decisions, and pushes signed fix commits back to the
PR head branch.

## Safety preconditions — verify before doing anything

1. **Own-branch only.** This skill pushes. Refuse to run if the checked-out
   branch is `main`, `master`, or `staging`, or is the PR's **base** branch.
   You may only push to the PR's **head** branch.
2. **Confirm the head branch is the current branch and tracks a remote.** If the
   working tree is not on the PR head (e.g. the PR was not fetched into this
   worktree), stop and print the fetch recipe in Phase 0 rather than guessing.
3. **Never force-push.** A plain push only. If the push is rejected because the
   remote head moved, stop and report — do not `--force`.
4. **Never bypass commit signing.** Commit with the repo's normal signing
   config. Do not pass `--no-gpg-sign` or disable `commit.gpgsign`.

## Phase 0: Resolve the PR target

Take the PR number from the invocation (`pr-grill <pr-number>`).

If the PR is not already checked out in this worktree, stop and tell the user to
fetch it in isolation (do not switch branches in a shared checkout):

```bash
git fetch origin <base>                       # the PR's base branch, kept fresh
git fetch origin pull/<pr-number>/head:pr-<pr-number>
git worktree add ../review-pr-<pr-number> pr-<pr-number>
cd ../review-pr-<pr-number>
# re-run pr-grill <pr-number> here
```

Determine the review scope as the PR's net diff:

```bash
BASE=$(gh pr view <pr-number> --json baseRefName --jq .baseRefName)   # always read from the PR — some repos use a non-default base such as staging
git fetch -q origin "$BASE"
RANGE="$(git merge-base "origin/$BASE" HEAD)..HEAD"
```

Skip docs/config-only changesets (same heuristic as `grill`): if `git diff
--name-only "$RANGE"` contains no source files, report the skip and exit — there
is nothing for the matrix to find.

## Phase 1: Deep matrix on the PR diff

Run `grill`'s **deep** matrix against `$RANGE`: the six core lanes plus the
conditional tenant-coupling lane when customer-variable behavior is present. Load the lane prompts
from `grill`'s role references — do not re-author them:

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

Invoking `pr-grill` is an explicit request to use independent subagents for the
six core review lanes, plus the conditional tenant-coupling lane when signaled,
whenever the active runtime exposes subagent/delegation tools. Do not require the
user to separately say "use subagents" before spawning those lane reviewers.

**Cross-engine emphasis.** You are reviewing another engine's work. Beyond
line-level bugs, scrutinize the _design decisions_ the author made and did not
question: chosen abstractions, latency/UX tradeoffs, removed or added special
cases, and whether a "fix" traded away a property the original code protected.
These are the findings a same-engine grill misses and the reason this pass
exists.

## Phase 2: Publish findings, then apply fixes

Verify and deduplicate every finding against the full PR ledger. Post one inline
comment per confirmed root cause before editing, using the local-review marker
and an exact diff anchor. Apply `grill`'s fix bias to `$RANGE`: fix every valid
finding, including nits.
Dismiss invalid findings or suggestions that would make the code worse, with the
evidence that disproves them. Defer only valid but extremely large follow-ups
(roughly 300+ lines or cross-cutting rewrites) and open or link a GitHub issue
for each deferral. Critical correctness/security findings must not be silently
dropped. Run the smallest relevant formatter/test command the repo documents for
the files you touched.

## Phase 3: Commit and push (automatic)

If fixes were applied, commit and push without a confirmation gate — this skill
is for your own PR branches.

1. **Label the relay commit** so the cross-engine leg is auditable in `git log`:

   ```bash
   git add -A
   git commit -m "fix(pr-grill): <one-line summary of what the cross-engine pass changed>" \
     --trailer "Cross-engine-review: pr-grill"
   ```

   Use the repo's normal signing config (do not disable it).

2. **Push to the PR head branch** (plain push, no force):

   ```bash
   git push
   ```

   If the push is rejected, stop and report the rejection — do not force-push or
   rebase silently.

3. Reply to every posted thread with the fix commit and validation result, then
   resolve it. For a dismissal or tracked deferral, reply with the evidence or
   issue link before resolving. Stop if any thread cannot be replied to or
   resolved.

If no fixes were applied (clean, or everything deferred/dismissed), do not
commit or push. Report the clean result.

## Phase 4: Hand back

Print a summary aimed at the **originating engine's re-review** — it needs to
know what changed and what to scrutinize:

```text
pr-grill complete on PR #<pr-number> (cross-engine pass).
review depth: <deep with independent subagents | deep local multi-pass fallback>
findings fixed:    <count + one-line each>
design tradeoffs flagged: <any decisions the re-review should adjudicate — e.g. a fix
                           that simplified logic but changed a latency/UX property>
deferred / dismissed: <count + rationale>
threads:            <posted/replied/resolved counts>
validation run:    <commands + result>
pushed:            <yes: SHA on <head-branch> | no fixes — nothing pushed>

Hand back to the authoring engine for re-review of the new HEAD
(e.g. the next local convergence pass, `reviewit <pr-number>` on the hosted
fallback path, or a fresh `grill` on the pushed commit).
```

Always surface the design tradeoffs explicitly — the round trip only works if
the engine reviewing next knows where to look.

## What this skill does NOT do

- **Does not run `refactorpass`.** No cleanup-churn on a PR under cross-review.
- **Does not open or merge the PR**, and does not push to a base branch.
- **Does not force-push or rebase.** A rejected push is reported, not forced.
- **Does not replace the selected review path.** `reviewit` remains the hosted
  Gemini + Copilot fallback; local Codex/Claude convergence is the alternative.
  `pr-grill` is an optional cross-engine relay, not a terminal review.

## Source of truth

This skill lives upstream in `codex-platform` at `.codex/skills/pr-grill/`.
Synced into consumer repos; consumer edits are overwritten on the next sync —
make changes upstream.
