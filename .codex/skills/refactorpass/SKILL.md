---
name: refactorpass
description: Pre-push cleanup pass for Codex. Use when the user asks for refactoring, cleanup, simplification, or the platform review chain before pushing source-code changes. Skips docs/config-only changesets, runs a structured cleanup matrix, and commits the result when appropriate.
---

# Refactor Pass

Run a structured, behavior-preserving cleanup pass on the current branch before review. This is not a broad refactor. The goal is to make the fresh diff simpler, easier to review, and less brittle without changing feature behavior.

## Context Window Check

Run this check before anything else. `refactorpass` (and the `grill` that typically follows) does diff-reading, multi-lane reviewing, and edit application — all cache-hungry. If the current Codex session has already been heavily used for feature implementation, the cache is largely spent on context the cleanup pass does not need, and the downstream `grill` (especially `grill deep`'s six independent lanes) will be measurably slower and more expensive.

Assess honestly:

- Has this session been writing/editing the feature about to be cleaned up? Long conversation, many file edits, dense planning?
- Is the conversation about to brush against compaction territory?

If either is yes, stop and tell the user:

> Your context is heavy from the implementation work. Start a new Codex session and run `refactorpass` (and `grill` / `deepgrill`) there. The downstream lanes need cache headroom and a fresh session makes the chain materially cheaper.

Do not proceed in the current session unless the user explicitly overrides.

## Fix Bias

Apply every valid cleanup `refactorpass` surfaces in this pass. Skip suggestions only when they are wrong: would change behavior, would make the code worse, would introduce speculative abstraction, or are based on a misread of the diff. Do not defer valid cleanups to a "follow-up PR" — the only legitimate defer is a major architectural rework (roughly 300+ lines or a cross-cutting redesign), and in that case file a GitHub issue at deferral time rather than leaving the suggestion as an undocumented todo. Reason: every valid cleanup that ships becomes the floor for the next PR in this area, and letting them accrue as "deferred" turns the backlog into review noise and makes future cleanups more expensive.

## Cleanup Matrix

Refactorpass must cover three lanes:

1. **Simplicity/DRY lane** — remove fresh duplication, collapse awkward control flow, inline one-use abstractions, delete dead code, and simplify names when the diff makes intent clearer.
2. **Correctness-preserving lane** — look for cleanup that reduces bug risk without changing behavior: narrower conditions, safer defaults, clearer error paths, less state mutation, and tighter async/resource cleanup.
3. **Convention/API lane** — align fresh code with local patterns, package boundaries, exports, dependency placement, and documented repo conventions.

Run these lanes as independently as the active runtime permits:

- If subagents/delegation are available and permitted by the active Codex instructions, spawn independent cleanup reviewers for the three lanes. Tell each reviewer to inspect only the local diff, suggest behavior-preserving cleanup, and avoid broad rewrites.
- If subagents are unavailable or not permitted, perform three separate local passes using the lane prompts above. Do not present that as equivalent to independent subagents.
- If refactorpass could not use independent subagents, explicitly say so in the output under `cleanup depth`.

## Process

1. Verify the branch is not `main`, `master`, or `staging`.
2. Use the exact base SHA supplied by an invoking `deepgrill` or caller. Only
   when run standalone without a supplied base, resolve `@{u}` when available,
   otherwise the default branch, once. Pass the literal `<base-sha>..HEAD` range
   to every cleanup lane; never let lanes re-resolve a mutable ref.
3. Skip if the changeset is docs/config-only. Treat source files such as `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`, `.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, and `.bash` as review-worthy.
4. Read the changed source files and execute every lane in the Cleanup Matrix.
5. Consolidate lane suggestions, deduplicate by root cause, and apply only cleanup that is behavior-preserving and clearly improves the fresh diff.
6. Keep scope tight: touch only code changed by the current branch unless a tiny adjacent edit is required to finish the cleanup safely.
7. Do not introduce feature behavior, broad rewrites, unrelated style churn, formatting-only commits, or speculative abstraction.
8. Run the smallest relevant formatter/test command if the repo documents one.
9. If changes were made, commit them as `refactor: codex cleanup pass - <summary>`.

## Output

Report:

- cleanup depth: independent subagents, local three-pass fallback, docs/config-only skip, or no source changes
- whether changes were made
- commit SHA if created
- validation run
- recommended next step from the selected path: if invoked by `deepgrill`,
  return so it can run `grill deep`; if run standalone on the local path, run
  `deepgrill` next and hand off to Claude only after that full chain; on the
  hosted fallback path, run `grill`/`deepgrill` before publication and
  `reviewit <pr-number>` after PR creation
