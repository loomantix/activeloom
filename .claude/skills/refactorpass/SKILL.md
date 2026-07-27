---
name: refactorpass
description: PR-first refactor pass that runs /simplify once, posts verified cleanup suggestions inline before editing, then pushes, replies, and resolves.
argument-hint: (optional PR number; always single-pass)
---

# Refactor pass — PR-first wrapper

Run one behavior-preserving cleanup pass on an open draft PR before adversarial
review.

## Context-window check

`/simplify` reads and edits the full changeset. If this session authored the
change or carries dense implementation context, recommend a fresh Claude
session. Continue only after an explicit override.

## PR-first pre-flight

1. Load `.claude/references/local-review-ledger.md`.
2. Require a clean, committed feature branch, not `main`, `master`, or
   `staging`.
3. Reuse its open PR. If none exists, push normally and open a draft PR before
   cleanup starts.
4. Require local HEAD, remote head, and PR head to match. Read all prior review
   threads.
5. Resolve the exact base SHA once and use its literal `<base-sha>..HEAD` range.
6. Skip docs/config-only changesets.

## Single `/simplify` pass

Invoke `Skill(skill="simplify")` once. Do not run a second pass.

Consolidate its suggestions, verify each against the source, and deduplicate
against the complete PR ledger. For every confirmed cleanup:

1. Post one inline local-review comment before accepting the edit.
2. Keep the cleanup behavior-preserving and inside changed code, apart from a
   tiny adjacent edit required to complete it safely.
3. Reject broad rewrites, feature behavior, unrelated style churn, and
   speculative abstraction.

Run the smallest relevant formatter or test. If edits remain uncommitted, stage
them and create one `refactor: /simplify pass — <summary>` commit. Push normally,
reply to every cleanup thread with the commit SHA and validation, then resolve
it. Stop if any ledger step fails.

If no cleanup survives verification, leave a clean-pass PR review comment with
Claude, the exact reviewed head, and `no new material findings`.

## Output

Report:

- PR number and reviewed head;
- whether cleanup changed the branch and the commit SHA;
- comments posted, replies posted, and threads resolved;
- validation run;
- next step: `/grill <pr-number>` or return to `/deepgrill <pr-number>`.

## Boundaries

- Do not force-push or merge.
- Do not invoke adversarial or hosted reviewers.

## Source of truth

This skill lives upstream at `.claude/skills/refactorpass/` and is synced to
consumer repos.
