---
name: deepgrill
description: High-fidelity PR-first review chain that opens or reuses a draft PR, posts verified findings inline before fixes, and runs /grill deep — preceded by /refactorpass on this engine's first pass only. Rounds 3+ run in convergence mode. Use on complex or high-risk changes such as auth/crypto, schema migrations, sync-propagating work, and large refactors.
argument-hint: (optional PR number)
---

# /deepgrill — PR-first deep chain

Run `/grill deep` against an open draft PR and its durable local-review ledger,
preceded by `/refactorpass` only on this engine's first pass over that PR.

The chain gets cheaper as it repeats, deliberately. Cleanup runs once; the
adversarial stance holds for two rounds and then gives way to landing the change.

Use this path for `.claude/skills/**`, sync scripts, GitHub Actions, auth,
crypto, secrets, sensitive-data paths, schema/data-shape changes, large
refactors, recurring defects, or an explicit high-risk review request.

## Phase 0: Pre-flight

### Context-window check

This chain invokes `/simplify` and up to six adversarial sub-agents. If this
session authored the change or carries dense implementation context, stop and
recommend a fresh Claude session. A larger context window does not relax this
gate: authoring rationale anchors the reviewer and is expensive to fan out. See
[`../../MODEL_NOTES.md`](../../MODEL_NOTES.md) §8.

Proceed in the current session only after an explicit override.

### PR-first boundary

1. Load [`../../references/local-review-ledger.md`](../../references/local-review-ledger.md).
2. Require a clean, committed feature branch, not `main`, `master`, or
   `staging`.
3. Reuse the open PR whose head is the branch. If none exists, push normally
   and open a draft PR before invoking a review lane.
4. Require local HEAD, remote head, and PR head to match.
5. Record the PR number and exact base SHA. Read every prior review thread,
   including resolved and outdated threads.
6. Apply the docs/config-only skip, per the ledger's changeset
   classification.
7. Resolve the changed-file list once and pass it to both lanes, so the refactor
   pass and the deep grill share one resolution instead of each rebuilding the
   changeset. The ledger's diff-delivery rules govern both.
8. Resolve this engine's round number per the ledger — `$AGENT_LOOP_REVIEW_ROUND`
   when the runner set it, otherwise one past the count of `local-review-pass:v1`
   and `local-review-complete:v1` markers naming `engine=claude`. Rounds 1–2 are
   adversarial; round 3 and later are convergence rounds. State which applies
   before running a lane.

## Phase 1: Refactor pass — first Claude pass only

Search the PR for `local-review-refactor:v1 engine=claude`. If it is present,
skip this phase entirely, report `refactor pass: already spent at <sha>`, and go
to Phase 2. A convergence round never runs cleanup, marker or not.

Otherwise invoke `Skill(skill="refactorpass", args="<pr-number>")` and wait for
it to return. Do not stop when the sub-skill returns.

## Phase 2: Deep grill

Reload the PR head and ledger, then invoke
`Skill(skill="grill", args="<pr-number> deep")`, passing the resolved round so
the lane selects the matching stance.

In an adversarial round the deep matrix uses the relevant lenses from code
review, silent failures, type/API design, comments/docs, tests, security, and
conditional tenant-coupling. Keep the matrix bounded per `MODEL_NOTES.md`.

In a convergence round the matrix narrows to correctness, silent failure, and
security when its signal is present, and the PR changes only for a blocking
defect. `/grill` owns those rules; do not restate or relax them here.

Every confirmed finding must be posted inline before editing. A completed fix
must be pushed, replied to with its SHA, validation, and structured disposition,
then resolved. When the combined hook committed, the final `/grill` lane posts
the completion marker for the enclosing before/final head pair.

## Phase 3: Handoff

Print:

```text
✅ /deepgrill complete on PR #<pr-number>.
- Reviewed head: <sha>
- Round: <n> (<adversarial | convergence>)
- Refactor pass: <ran | already spent at <sha> | docs-config skip>
- Findings: <posted/replied/resolved counts>
- Review depth: <agents run>
- Classification: <clean | minor | material>
- Product code changed: <yes | no>

Next local step:
  If this pass made a material fix, restart at /codex-review <pr-number>.
  If it changed no product code, the PR has converged — recommend this repo's
  ship step, whatever it uses to merge the PR, instead of a new round.
  Otherwise this completes the Claude half of the current local round.
```

A convergence round that found no blocking defect ends the loop. Say so and name
the ship step; do not report the remaining rounds as owed.

Classify by what the fix **changes**, not by how severe the finding sounded.
Only a change to product code is material; tests, fixtures, comments, and docs
are minor, and they leave the other engine's attestation valid for the new head.

A round that finds only test and comment work is the signal to ship. The product
has converged and the review has moved on to auditing its own artifacts — a
surface that regenerates each time it is hardened, so the findings never run out
and their volume says nothing about whether more review is warranted. Say so
plainly and recommend shipping; the caller cannot see that the fixes stopped
touching product code.

When the hosted fallback was explicitly selected, hand off to
`/reviewit <pr-number> deep` instead.

## Boundaries

- Do not force-push or merge.
- Do not invoke hosted reviewers on the local convergence path.
- Do not silently override the user's finding dispositions.

## Source of truth

This skill lives upstream at `.claude/skills/deepgrill/` and is synced to
consumer repos.
