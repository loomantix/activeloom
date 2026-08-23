---
name: refactorpass
description: PR-first refactor pass that runs /simplify once against an open draft PR, verifies and commits the surviving cleanups, pushes, and records them in the PR ledger. Runs at most once per PR for this engine.
argument-hint: (optional PR number, optional "force"; always single-pass)
---

# Refactor pass — PR-first wrapper

Run one behavior-preserving cleanup pass on an open draft PR before adversarial
review. This is the Claude engine's **one** cleanup pass on that PR, not a step
that repeats each review round.

## Context-window check

`/simplify` reads and edits the full changeset. If this session authored the
change or carries dense implementation context, recommend a fresh Claude
session. Continue only after an explicit override.

## PR-first pre-flight

1. Load [`../../references/local-review-ledger.md`](../../references/local-review-ledger.md).
2. Require a clean, committed feature branch, not `main`, `master`, or
   `staging`.
3. Reuse its open PR. If none exists, push normally and open a draft PR before
   cleanup starts.
4. Require local HEAD, remote head, and PR head to match.
5. Resolve the exact base SHA once and use its literal `<base-sha>..HEAD` range.
   Read the actor-owned issue comments needed for the tier, round, stance, and
   latch, excluding every comment whose marker begins
   `local-review-telemetry:`.
6. Resolve the enclosing review round and stance from the caller when supplied.
   Otherwise take the round from the ledger's standalone rule, and take the
   stance from the effective tier marker already on the PR — that rule yields a
   round only, and stance follows the tier's schedule. Fall back to
   `adversarial` when no tier marker exists, which is correct because a PR
   carrying none has had no prior round. Retain both values through every
   terminal branch because `emit-telemetry` requires them and offers no way to
   omit `--stance`.
7. Take the pass telemetry snapshot now that its mandatory repository, PR,
   base, head, round, and stance identity exists, per
   [`../../REVIEW_WORKFLOW.md`](../../REVIEW_WORKFLOW.md) "Pass Telemetry". The
   helper is a no-op when extraction is not enabled for this repository, and it
   reports the separate emission gate that decides whether this pass may publish
   a record at all. The skill and identity-resolution setup above is outside the
   measurement boundary.
8. Read all prior review threads. Telemetry markers are not review context:
   exclude them by marker prefix and never carry one into context assembly.
   Apply the docs/config-only classification. On a skip, set the telemetry
   status to `skipped` and continue directly to Output without spending the
   refactor latch.
9. **Check the once-per-engine latch.** Search the PR's comments for
   `local-review-refactor:v1 engine=claude`, authored by the actor running this
   review. If it is present, this PR has already had its Claude cleanup pass:
   set the telemetry status to `clean` and continue directly to Output. Do not
   run `/simplify`. Continue to cleanup only when the marker is absent or
   `$ARGUMENTS` contains `force`, and say which of the two applied.

   The rule exists because the second pass over an already-simplified diff
   returns naming and shape churn, not cleanups. That churn moves the head and
   re-stales the other engine's attestation for no shipped benefit.

10. Resolve the changed-file list once and follow the ledger's diff-delivery
    rules. If this pass fans cleanup angles out to agents, scope each to the
    files it reviews rather than giving every angle the same whole-diff
    artifact.

## Single `/simplify` pass

Record the exact HEAD and clean worktree status. Invoke
`Skill(skill="simplify", args="Analyze the PR diff and propose
behavior-preserving cleanups. Do not commit.")` once. Do not run a second pass.

`/simplify` applies the cleanups it finds — that is its contract, and asking it
for a proposal-only run is not reliable. Handle whichever outcome you get:

- **It left the tree clean and HEAD unmoved.** It found nothing, or it only
  reported. Nothing to commit.
- **It edited the worktree.** Normal. Review every edit before keeping it.
- **It committed.** Verify the commit is behavior-preserving and in scope; keep
  it rather than rewriting history. Never resolve this by force-push or
  `git stash`.

Verify each cleanup against the source and drop any that:

1. changes behavior, or reaches outside the changed code apart from a tiny
   adjacent edit required to complete it safely;
2. is a broad rewrite, unrelated style churn, or speculative abstraction.

Revert what you drop (`git checkout -- <path>` for uncommitted edits, a follow-up
edit for committed ones) before validating.

Cleanups are not adversarial findings, so the ledger's post-before-editing rule
does not apply here: there is no defect to disposition and no thread to resolve,
and `/simplify` has already edited by the time you could post one.

If anything survived: run the smallest relevant formatter or test, stage the
remaining edits, create one `refactor: /simplify pass — <summary>` commit, and
push normally. Then post **one** informational PR comment naming the cleanup
lane, the exact reviewed head, the resulting commit SHA, and a one-line list of
what was consolidated. Stop if any step fails.

If nothing survived, the branch is unchanged: post the same informational
comment with no commit SHA and move on. Do not push.

Either way, that comment closes the latch for this engine and must carry the
ledger's marker:

```text
<!-- local-review-refactor:v1 engine=claude head=<reviewed-sha> outcome=<committed|no-op> -->
```

Post it only for a pass that actually ran `/simplify`. A docs/config-only skip
leaves the latch open, so a later round whose changeset contains source can still
spend the one pass.

Do not write a `local-review-pass:v3` result or open `local-review:v3` finding
threads for cleanups: only the final adversarial `critique` lane may certify the
enclosing Claude review hook, and the outer wrapper owns its attestation.

## Output

Take the prompt-stack digests and emit this pass's telemetry record per
[`../../REVIEW_WORKFLOW.md`](../../REVIEW_WORKFLOW.md) "Pass Telemetry" with
`--pass-type refactor`, whichever of the three outcomes above applied. A record
that cannot name the prompt generation it ran on cannot be compared against the
next one, so the two digests are part of emitting, not an optional extra. A pass
that committed is `changed`; one that found nothing is `clean`. A pass that
stopped on a spent latch is also `clean`, not `skipped` — its changeset was
reviewable, this engine had simply already spent its one pass, and the record
rejects a `skipped` pass carrying review-significant files. A docs/config-only
skip is the case that genuinely reports `skipped`.

Emission exits zero whether or not it succeeded. Report the outcome and move on.
When a failure terminates the pass after a snapshot was taken, emit
`status=blocked` before returning; telemetry failure itself remains nonfatal and
is not retried. A failure before the snapshot boundary reports
`telemetry not emitted: boundary unresolved` because the mandatory record
identity does not yet exist.

Report:

- PR number and reviewed head;
- latch state: `first pass for this engine`, `skipped — already spent at <sha>`,
  or `forced re-run`;
- whether cleanup changed the branch and the commit SHA;
- cleanups kept and cleanups dropped on verification;
- validation run;
- next step: `/critique <pr-number>` or return to `/deepcritique <pr-number>`.

## Boundaries

- Do not force-push or merge.
- Do not invoke adversarial or hosted reviewers.

## Source of truth

This skill lives upstream at `.claude/skills/refactorpass/` and is synced to
consumer repos.
