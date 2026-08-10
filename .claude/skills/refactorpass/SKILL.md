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
4. Require local HEAD, remote head, and PR head to match. Read all prior review
   threads.
5. Resolve the exact base SHA once and use its literal `<base-sha>..HEAD` range.
6. Skip docs/config-only changesets, per the ledger's changeset
   classification.
7. **Check the once-per-engine latch.** Search the PR's comments for
   `local-review-refactor:v1 engine=claude`, authored by the actor running this
   review. If it is present, this PR has already had its Claude cleanup pass:
   report the skip with the head the earlier pass ran on and stop. Do not run
   `/simplify`. Continue only when the marker is absent or `$ARGUMENTS` contains
   `force`, and say which of the two applied.

   The rule exists because the second pass over an already-simplified diff
   returns naming and shape churn, not cleanups. That churn moves the head and
   re-stales the other engine's attestation for no shipped benefit.

8. Resolve the changed-file list once and follow the ledger's diff-delivery
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
