# Review Workflow

This file is synced from `codex-platform` into consumer repos. Consumer-specific
edits will be overwritten on the next sync.

## Select One Cross-Model Path

Choose the path before review starts. Do not combine them by default.

- **Local convergence** requires both Codex and a licensed local Claude Code
  CLI. It alternates fresh local reviewers before publication and does not use
  `reviewit` or hosted AI reviewers.
- **Hosted fallback** is for developers without local Claude Code. It keeps the
  Codex pre-push chain and uses `reviewit` after PR creation for Gemini Flash and
  Copilot coverage.

A consumer may declare one path as its repository default. Otherwise, select
based on the developer's available tooling.

## Local Convergence Path

Use this path when both local engines are available:

1. Make the change, run focused validation, and create a clean local commit.
2. Fetch the target base and give both reviewers the same explicit base ref.
3. In a fresh Codex session, run `deepgrill` against the branch diff. Verify
   every finding against source, fix every confirmed finding, validate, commit
   fixes, and leave a clean tree. Do not push.
4. On that resulting HEAD, run a fresh adversarial Claude review against the
   same base. Apply the same verify/fix/validate/commit/no-push contract.
5. Classify committed review fixes as `material` or `minor`. A material fix
   affects behavior, correctness, security/privacy, data safety, compatibility,
   deployment/sync integrity, or another substantive contract; restart at Codex
   when either pass makes one. Minor-only fixes (clarity, low-risk cleanup, or
   non-behavioral test/docs polish) are validated and kept but do not restart the
   cycle. Convergence requires one complete Codex-then-Claude round with no
   material fixes.
6. Cap the loop at four rounds unless the consumer explicitly configures a
   different positive bound. At cap exhaustion, stop, preserve the branch and
   worktree, and report non-convergence. Do not publish an unreviewed branch.
7. After convergence, fetch and integrate the base again, inspect the final
   diff, revalidate, then push and open the PR.

The `agent-loop` skill automates this path with a required non-mutating
validation hook plus `review_max_rounds`, `codex_review_hook`, and
`claude_review_hook`. Review hooks classify committed fixes through
`$AGENT_LOOP_REVIEW_OUTCOME_FILE`; a missing classification defaults to
`material`, while a pass with no commit is clean.
It disables ordinary `git push`, Git aliases, and `gh` invocations while hooks
run, while preserving authenticated Git reads, and publishes only the final
captured commit. The wrapper cannot prove that an
arbitrary command launched the named engine, started fresh, used the required
base SHA, or resolved every finding; consumer hook commands own those
guarantees and must fail if a valid finding remains.

Do not run `reviewit` after this path merely as an extra ritual. Use it only when
the developer intentionally chooses the hosted fallback or explicitly requests
additional hosted review.

## Hosted Fallback Path

Use this path when local Claude Code is unavailable.

### Lean

1. Make the local change.
2. Run `refactorpass` for source changes.
3. Run lean `grill` before pushing. It must execute the code-reviewer and silent
   failure-hunter lanes, using independent subagents when available.
4. Push and open the PR.
5. Run `reviewit <pr-number>`. It triggers Gemini Flash and Copilot, verifies and
   deduplicates their findings, fixes confirmed issues, pushes, replies, and
   loops within its configured cap.

### Deep

1. Run `deepgrill` before pushing. It executes `refactorpass` plus `grill deep`'s
   six core lanes and the conditional tenant-coupling lane.
2. Push and open the PR.
3. Run `reviewit <pr-number> deep`. Deep mode uses the same hosted reviewers
   with its larger cap, early-exit rules, and final fresh Codex `deepgrill`.

Choose deep when the change touches auth, crypto, secret handling, schema/data
shape, GitHub Actions, sync tooling, `.codex/skills/**`, a large refactor, an
area with recurring incidents, or customer/tenant-variable behavior.

## Skip Path

For docs/config-only changes, skip expensive review automation unless the user
explicitly wants it. Source-code changes include common implementation
extensions such as `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`,
`.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, and `.bash`.

## Cross-Engine Relay (Optional)

When you want a second engine's eyes on a PR another engine authored, run
`pr-grill <pr-number>` on your own branch. It runs the deep matrix against the
PR diff, applies confirmed fixes, and pushes signed, labeled commits back to the
PR head so the originating engine can re-review the new HEAD. The hand-back is
mandatory: `pr-grill` is one leg of a round trip, not a terminal review.

## Review Principles

- Treat every generated finding as a hypothesis. Verify it against code, tests,
  and documented constraints before changing anything.
- Fix every valid in-scope finding, including nits. Dismiss false positives with
  a concrete rationale.
- Defer only genuinely large follow-up work, roughly 300+ lines or a
  cross-cutting rewrite, and track it explicitly.
- Never let a review hook push, open a PR, invoke another review path, or copy
  sensitive source, credentials, customer data, or model logs into PR metadata.
- Stop at the configured cap and preserve recovery state when reviewers do not
  converge.
