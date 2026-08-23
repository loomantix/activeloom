---
name: refactorpass
description: PR-first cleanup pass for Codex. Use when the user asks for refactoring, cleanup, simplification, or the platform review chain on an open draft PR. Posts verified cleanup suggestions inline before editing, skips docs/config-only changesets, runs a structured cleanup matrix, and pushes, replies, and resolves when appropriate. Runs at most once per PR for this engine.
---

# Refactor Pass

Run a structured, behavior-preserving cleanup pass on an open draft PR before
adversarial review. This is not a broad refactor, and it is not a step that
repeats each review round: it is the Codex engine's **one** cleanup pass on that
PR.

## Context Window Check

Run this check before anything else. `refactorpass` (and the `critique` that typically follows) does diff-reading, multi-lane reviewing, and edit application — all cache-hungry. If the current Codex session has already been heavily used for feature implementation, the cache is largely spent on context the cleanup pass does not need, and the downstream `critique` (especially `critique deep`'s six independent lanes) will be measurably slower and more expensive.

Assess honestly:

- Has this session been writing/editing the feature about to be cleaned up? Long conversation, many file edits, dense planning?
- Is the conversation about to brush against compaction territory?

If either is yes, briefly tell the user:

> This session already contains substantial implementation context. A fresh Codex session may make the cleanup and review lanes cheaper, but I can continue here if that is the authorized task.

This is cost and quality advice, not a workflow gate. Do not stop, defer the
authorized task, or require a new session solely because context is heavy or
compaction is approaching. Continue in the current session when the user has
already authorized the cleanup or asked you to proceed. Pause only when the user
requested a fresh-session boundary, the runtime cannot continue safely, or a
separate-session protocol transition requires another reviewer.

## Fix Bias

Apply every valid cleanup `refactorpass` surfaces in this pass. Skip suggestions only when they are wrong: would change behavior, would make the code worse, would introduce speculative abstraction, or are based on a misread of the diff. Do not defer valid cleanups to a "follow-up PR" — the only legitimate defer is a major architectural rework (roughly 300+ lines or a cross-cutting redesign), and in that case file a GitHub issue at deferral time rather than leaving the suggestion as an undocumented todo. Reason: every valid cleanup that ships becomes the floor for the next PR in this area, and letting them accrue as "deferred" turns the backlog into review noise and makes future cleanups more expensive.

## Cleanup Matrix

Refactorpass must cover three lanes:

1. **Simplicity/DRY lane** — remove fresh duplication, collapse awkward control flow, inline one-use abstractions, delete dead code, and simplify names when the diff makes intent clearer.
2. **Correctness-preserving lane** — look for cleanup that reduces bug risk without changing behavior: narrower conditions, safer defaults, clearer error paths, less state mutation, and tighter async/resource cleanup.
3. **Convention/API lane** — align fresh code with local patterns, package boundaries, exports, dependency placement, and documented repo conventions.

Run these lanes as independently as the active runtime permits:

- If subagents/delegation are available and permitted by the active Codex instructions, spawn independent cleanup reviewers for the three lanes using the ledger's immutable review packet and scoped diff-delivery contract. Keep the packet prefix byte-identical, append only the cleanup lens and exact file scope, use no inherited conversation history when supported, and impose a concise output ceiling. Tell each reviewer to suggest behavior-preserving cleanup and avoid broad rewrites.
- If subagents are unavailable or not permitted, perform three separate local passes using the lane prompts above. Do not present that as equivalent to independent subagents.
- If refactorpass could not use independent subagents, explicitly say so in the output under `cleanup depth`.

## Process

1. Load `.codex/references/local-review-ledger.md`.
   1a. Take the pass telemetry snapshot before reading or classifying anything, per
   `.codex/REVIEW_WORKFLOW.md` "Pass Telemetry". A cleanup pass spends tokens
   and moves lines; leaving it out would attribute its churn to nobody while
   its cost vanished, which corrupts the per-line denominator directly. The
   helper is a no-op when extraction is not enabled for this repository, and
   it reports the separate emission gate that decides whether this pass may
   publish a record at all.
2. Verify the branch is not `main`, `master`, or `staging`. Resolve or create
   its draft PR before running cleanup lanes, and require local, remote, and PR
   heads to match. Read all prior review threads.
3. Use the exact base SHA supplied by an invoking `deepcritique` or caller. Only
   when run standalone without a supplied base, resolve `@{u}` when available,
   otherwise the default branch, once. Resolve the reviewed head, changed-file
   list, and diff stat once and build the ledger's immutable review packet. Pass
   the literal `<base-sha>..<head-sha>` range to every cleanup lane; never let
   lanes re-resolve a mutable ref or rebuild the packet independently.
4. Skip if the changeset is docs/config-only. Treat source files such as `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`, `.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, and `.bash` as review-worthy. Emit the step 14
   record with `--status skipped` before stopping — the classification read that
   reached this decision is itself part of what the pass cost.
5. Check the once-per-engine latch. Search the PR's comments for
   `local-review-refactor:v1 engine=codex`, authored by the actor running this
   review. If it is present, this PR has already had its Codex cleanup pass:
   report the skip with the head the earlier pass ran on, emit the step 14
   record with `--status clean`, and stop without running a lane. Continue only
   when the marker is absent or the caller explicitly asked to force a re-run,
   and say which of the two applied.

   The rule exists because the second pass over an already-simplified diff
   returns naming and shape churn, not cleanups. That churn moves the head and
   re-stales the other engine's attestation for no shipped benefit.

6. Assign each lane the exact changed source paths its lens needs and execute
   every lane in the Cleanup Matrix. Follow the ledger's scoped-read contract;
   do not hand every lane a whole-diff artifact.
7. Consolidate lane suggestions, verify them, and deduplicate them against the
   complete PR ledger.
8. Post each confirmed cleanup inline before editing, then apply only cleanup
   that is behavior-preserving and clearly improves the fresh diff.
9. Keep scope tight: touch only code changed by the current branch unless a tiny adjacent edit is required to finish the cleanup safely.
10. Do not introduce feature behavior, broad rewrites, unrelated style churn, formatting-only commits, or speculative abstraction.
11. Run the smallest relevant formatter/test command if the repo documents one.
12. If changes were made, commit them as `refactor: codex cleanup pass - <summary>`.
    Publish through `$AGENT_LOOP_REVIEW_PUSH_HELPER` when it is set; otherwise
    push without force. Reply to each cleanup thread with the commit and
    validation, then resolve it.
13. Whether or not the lanes produced changes, post one informational PR comment
    closing the latch for this engine, carrying the ledger's marker:

    ```text
    <!-- local-review-refactor:v1 engine=codex head=<reviewed-sha> outcome=<committed|no-op> -->
    ```

    Post it only for a pass that actually ran the cleanup lanes. A docs/config-only
    skip leaves the latch open, so a later round whose changeset contains source
    can still spend the one pass.

14. Take the prompt-stack digests and emit this pass's telemetry record per
    `.codex/REVIEW_WORKFLOW.md` "Pass Telemetry" with `--pass-type refactor`. A
    record that cannot name the prompt generation it ran on cannot be compared
    against the next one, so the two digests are part of emitting, not an
    optional extra. A pass that committed is `changed`;
    one that found nothing is `clean`. A pass that stopped on a spent latch is
    also `clean`, not `skipped` — its changeset was reviewable, this engine had
    simply already spent its one pass, and the record rejects a `skipped` pass
    carrying review-significant files. A docs/config-only skip is the case that
    genuinely reports `skipped`. A pass that could not complete at all reports
    `blocked`. Emission exits zero whether or not it succeeded: report the
    outcome and move on.

    Steps 4 and 5 return before reaching this step, so each names the record it
    emits rather than relying on the pass reaching the end. Skip emission
    entirely when the telemetry helper reports `emit: false`.

## Output

Report:

- cleanup depth: independent subagents, local three-pass fallback, docs/config-only skip, or no source changes
- latch state: first pass for this engine, skipped because already spent at `<sha>`, or forced re-run
- whether changes were made
- commit SHA if created
- validation run
- PR number plus comments, replies, and resolved-thread counts
- recommended next step from the selected path: if invoked by `deepcritique`,
  return so it can run `critique deep`; if run standalone, run `deepcritique`
  next and let it follow the selected auto or handoff mode before handing to the
  next declared reviewer; add `reviewit <pr-number>` whenever a hosted pass is
  wanted
