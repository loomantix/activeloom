# Review Workflow

This file is synced from `codex-platform` into consumer repos. Consumer-specific
edits will be overwritten on the next sync.

## PR-First Rule

Open a draft pull request before any structured review cleanup such as
`refactorpass`, or adversarial review. Local Codex and Claude passes use GitHub
review threads as durable shared context:
post each verified finding inline before editing, push the correction, reply
with the fix and validation, then resolve the thread. Every pass must read
resolved as well as unresolved threads before reviewing the current head.

Load [the local review ledger](references/local-review-ledger.md) before running
`refactorpass`, `critique`, `deepcritique`, `pr-critique`, or local review hooks.

## Select One Cross-Model Path

Choose the path before review starts. Do not combine them by default.

- **Local convergence** requires both Codex and a local Claude Code CLI. It
  alternates fresh local reviewers on a draft PR and does not use `reviewit` or
  hosted AI reviewers.
- **Hosted fallback** is for developers without local Claude Code. It uses the
  same PR-first local Codex chain, then `reviewit` for Gemini Flash and Copilot
  coverage.

A consumer may declare one path as its repository default. Otherwise, select
based on the developer's available tooling.

## Local Convergence Path

Use this path when both local engines are available:

1. Make the change, run focused validation, and create a clean local commit.
2. Push the feature branch and open or reuse its draft PR. Record the PR number,
   head SHA, and all existing review threads before any reviewer runs.
3. Fetch the target base, record its immutable commit SHA, and give that exact
   SHA to both reviewers for the round. Neither reviewer may re-resolve a
   mutable remote-tracking ref independently.
4. In a fresh Codex session, run `deepcritique <pr-number>`. Read the PR ledger,
   post every confirmed finding inline before editing, fix, validate, commit,
   push, reply, and resolve.
5. On that resulting HEAD, run a fresh adversarial Claude review against the
   same base and PR ledger. Apply the same comment/fix/reply/resolve contract.
6. Classify committed review fixes as `material` or `minor`. A material fix
   affects behavior, correctness, security/privacy, data safety, compatibility,
   deployment/sync integrity, or another substantive contract; restart at Codex
   when either pass makes one. Minor-only fixes are validated and kept but do
   not restart the cycle. Convergence requires one complete Codex-then-Claude
   round with no material fixes.

   **The chain gets cheaper as it repeats.** Two rules make that happen, and both
   are derived from the ledger so a fresh session reaches the same answer:
   - **The refactor pass runs once per engine per PR.** A second cleanup pass over
     an already-simplified diff returns naming and shape churn, which moves the
     head and re-stales the other engine's attestation for nothing that ships.
     Each engine's cleanup lane latches on a `local-review-refactor:v1` marker;
     a docs/config-only skip does not consume it.
   - **Rounds 1–2 are adversarial; round 3 and later are convergence rounds.**
     Once both engines have read the change cold twice, the remaining findings
     are mostly about the review's own artifacts. A convergence round runs only
     the lanes that can find a reason not to deploy, changes the PR only for a
     realistically reachable blocking defect, defers everything else, creates
     an issue only for an urgent high-impact follow-up, and ends the loop as soon
     as it finds no blocker. Lanes still report everything they find —
     the narrowing is a disposition rule applied when consolidating lane output,
     never an instruction to a lane to withhold what it found.

7. Cap the loop at four rounds unless the consumer explicitly configures a
   different positive bound. At cap exhaustion, stop, preserve the branch,
   worktree, and draft PR, and report non-convergence. Do not mark it ready.
8. After convergence, require every local-review thread to contain a disposition
   reply and be resolved, revalidate the exact PR head, and mark the PR ready.

The `agent-loop` skill automates this path with a required non-mutating
validation hook plus `review_max_rounds`, `codex_review_hook`, and
`claude_review_hook`. Under contract v3 every hook writes a structured clean,
changed, or blocked result to `$AGENT_LOOP_REVIEW_RESULT_FILE`. The wrapper
validates that result against observed Git state and the v3 ledger, then posts
the canonical pass/completion attestation itself. It opens a draft PR before
review, exports the pinned PR identity to both hooks, checkpoints private atomic
run state, and verifies that each hook leaves local, remote, and PR heads
aligned. Consumer hooks own semantic finding verification, deterministic inline
posting and disposition, and classification; they must fail or return blocked
if a valid finding or undisposed local-review thread remains.

Do not run `reviewit` after this path merely as an extra ritual. If the developer
switches to hosted review and it creates or pushes a commit, the prior local
convergence is stale: rerun a complete Codex-then-Claude round on the new HEAD
before merge, or explicitly use the hosted fallback as the final review path.

## Hosted Fallback Path

Use this path when local Claude Code is unavailable.

### Lean

1. Make the local change, create a clean commit, and open a draft PR.
2. Run `refactorpass <pr-number>` for source changes.
3. Run `critique <pr-number>`. Lean mode executes the code-reviewer and silent
   failure-hunter lanes, using independent subagents when available.
4. Run `reviewit <pr-number>`. It triggers Gemini Flash and Copilot, verifies and
   deduplicates their findings, fixes confirmed issues, pushes, replies, and
   loops within its configured cap.

### Deep

1. Open a draft PR, then run `deepcritique <pr-number>`. It executes `critique deep`'s
   six core lanes and the conditional tenant-coupling lane, preceded by
   `refactorpass` on this engine's first pass over the PR.
2. Run `reviewit <pr-number> deep`. Deep mode uses the same hosted reviewers
   with its larger cap, early-exit rules, and final fresh Codex `deepcritique`.
   That tail `deepcritique` skips the refactor pass — step 1 already spent this
   engine's cleanup latch on the PR.

Choose deep when the change touches auth, crypto, secret handling, schema/data
shape, GitHub Actions, sync tooling, `.codex/skills/**`, a large refactor, an
area with recurring incidents, or customer/tenant-variable behavior.

## Skip Path

For docs/config-only changes, skip expensive review automation unless the user
explicitly wants it. Source-code changes include common implementation
extensions such as `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`,
`.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, and `.bash`.

## Cross-Engine Relay

When a different engine reviews the PR, run `pr-critique <pr-number>` from its
isolated worktree. It reads the existing ledger, runs the deep matrix, posts
confirmed findings inline, applies fixes, and completes those same threads. The
hand-back is mandatory: the originating engine reads the updated ledger and
re-reviews the new head.

## Review Principles

- Treat every generated finding as a hypothesis. Verify it against code, tests,
  and documented constraints before posting or changing anything.
- Fix a confirmed finding only when the likelihood and impact of real user harm,
  or a credible path to security exploitation, justify the fix's churn and
  regression risk.
- Create a follow-up issue only for a concrete, high-impact defect that should
  be scheduled within roughly two weeks. Record ordinary deferrals without an
  issue; do not turn speculative hardening, cleanup, or low-likelihood edge cases
  into backlog.
- A review fix without a preceding inline finding, a finding without a reply,
  or a resolved thread without a visible disposition is a failed review pass.
- Never copy sensitive source, credentials, private data, or model logs into PR
  metadata. Only the concise verified finding and its disposition belong there.
- Stop at the configured cap and preserve recovery state when reviewers do not
  converge.
