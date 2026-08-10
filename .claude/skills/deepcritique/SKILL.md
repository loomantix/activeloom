---
name: deepcritique
description: High-fidelity PR-first review chain that opens or reuses a draft PR, posts verified findings inline before fixes, and runs /critique deep — preceded by /refactorpass on this engine's first pass only. Rounds 3+ run in convergence mode. Use on complex or high-risk changes such as auth/crypto, schema migrations, sync-propagating work, and large refactors.
argument-hint: (optional PR number)
---

# /deepcritique — PR-first deep chain

Run `/critique deep` against an open draft PR and its durable local-review ledger,
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
6. Apply the docs/config-only skip, per the ledger's changeset classification.
   On a skip, finalize a clean v3 result using the ledger's wrapper/standalone
   ownership rule, report `docs-config skip`, and exit without spending the
   refactor latch.
7. Resolve the changed-file list once for the initial packet. If refactorpass
   commits, that packet ends with its reviewed head: reload the PR head and
   build a new immutable packet before deep critique. If refactorpass is a no-op,
   both lanes may reuse the initial packet. The ledger's diff-delivery rules
   govern both.
8. Resolve this engine's round number per the ledger — `$AGENT_LOOP_REVIEW_ROUND`
   when the runner set it, otherwise one past the count of `local-review-pass:v3`
   and `local-review-complete:v3` markers naming `engine=claude`. Rounds 1–2 are
   adversarial; round 3 and later are convergence rounds. State which applies
   before running a lane.

## Phase 1: Refactor pass — first Claude pass only

Search the PR for `local-review-refactor:v1 engine=claude`. If it is present,
skip this phase entirely, report `refactor pass: already spent at <sha>`, and go
to Phase 2. A convergence round never runs cleanup, marker or not.

Otherwise invoke `Skill(skill="refactorpass", args="<pr-number>")` and wait for
it to return. Do not stop when the sub-skill returns.

## Phase 2: Deep critique

Reload the PR head and ledger. When refactorpass moved the head, rebuild the
immutable review packet from the same pinned base through that new head. Then
`Skill(skill="critique", args="<pr-number> deep")`, passing the resolved round so
the lane selects the matching stance.

In an adversarial round the deep matrix uses the relevant lenses from code
review, silent failures, type/API design, comments/docs, tests, security, and
conditional tenant-coupling. Keep the matrix bounded per `MODEL_NOTES.md`.

In a convergence round the matrix narrows to correctness, silent failure, and
security when its signal is present, and the PR changes only for a blocking
defect. `/critique` owns those rules; do not restate or relax them here.

Every confirmed finding must be posted inline before editing. A completed fix
must be pushed, replied to with its SHA, validation, and structured disposition,
then resolved through the deterministic helper. The final `/critique` lane always
finalizes the v3 structured result: under agent-loop the wrapper posts the
completion attestation after validating it, while a standalone pass attests
through the helper before reporting completion.

The final result covers the entire enclosing deepcritique transition, beginning
at the head recorded before refactorpass. If refactorpass committed and critique made
no later fix, serialize `changed` with classification `minor`, an empty finding
set, and that original before SHA; the committed refactor latch supplies the
evidence. Do not emit `clean` for a cleanup-moved enclosing hook.

## Phase 3: Handoff

Print:

```text
✅ /deepcritique complete on PR #<pr-number>.
- Reviewed head: <sha>
- Round: <n> (<adversarial | convergence>)
- Refactor pass: <ran | already spent at <sha> | docs-config skip>
- Findings: <posted/replied/resolved counts>
- Review depth: <agents run>
- Classification: <clean | minor | material>

Next local step:
  If this pass made a material fix, restart at /codex-review <pr-number>.
  Otherwise this completes the Claude half of the current local round; the
  outer runner decides convergence from both exact-head v3 results.
```

A convergence round that found no blocking defect ends the loop. Say so and name
the ship step; do not report the remaining rounds as owed.

Classify by effect, not path or finding severity. A correctness, security,
deployment/sync, or review-integrity fix may be material even when it touches a
test or workflow. Minor means low-risk non-behavioral cleanup or polish. Every
attestation stays exact to its reviewed head; the outer round owns any explicit
minor-transition convergence decision.

When the hosted fallback was explicitly selected, hand off to
`/reviewit <pr-number> deep` instead.

## Boundaries

- Do not force-push or merge.
- Do not invoke hosted reviewers on the local convergence path.
- Do not silently override the user's finding dispositions.

## Source of truth

This skill lives upstream at `.claude/skills/deepcritique/` and is synced to
consumer repos.
