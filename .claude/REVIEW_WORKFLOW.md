# Review Workflow

This file is synced from `claude-platform` into consumer repos. Consumer edits
will be overwritten on the next sync.

## PR-First Rule

Open a draft pull request before any local cleanup or adversarial review. Local
Claude and Codex passes use GitHub review threads as durable shared context:
post each verified finding inline before editing, push the correction, reply
with the fix and validation, then resolve the thread. Every pass reads resolved
as well as unresolved threads before reviewing the current head.

Load [the local review ledger](references/local-review-ledger.md) before running
`refactorpass`, `grill`, `deepgrill`, `codex-review`, or local review hooks.

## Local Convergence Path

Use this path when both local engines are available:

1. Make the change, validate it, create a clean commit, and open a draft PR.
2. Pin the exact base SHA for the round and give it to both reviewers.
3. Run `codex-review <pr-number>` as a fresh local Codex pass. Read the ledger
   and apply the comment/fix/reply/resolve contract to confirmed findings.
4. On the resulting head, run a fresh Claude `deepgrill <pr-number>` with the
   same ledger contract.
5. Classify fixes as `material` or `minor` **by what the fix changes, not by how
   severe the finding sounded**. A fix is `material` only when it changes product
   code. Tests, fixtures, comments, and docs are `minor`. Restart at Codex when
   either pass makes a material fix. Keep minor-only fixes without restarting.
6. Converge after one complete Codex-then-Claude round changes no product code,
   every pass that committed nothing has an attestation covering the current head,
   every committed pass has a structured fix disposition plus a final-lane
   completion marker, and every local-review thread has a disposition reply and is
   resolved.

   An attestation covers the current head when no product code changed since the
   SHA it names — a later tests-or-docs-only commit does not invalidate it. Diff
   the attested SHA against the head over product paths to confirm. Requiring a
   byte-exact head match instead is what makes this loop unbounded: minor commits
   perpetually re-stale the other engine's attestation, so the condition in step 6
   can never be met while either engine keeps finding test work.

7. **The moment a pass changes no product code, stop and recommend shipping.**
   Name the consumer's ship step — whatever that repo uses to merge the PR.
   Reaching this before the cap is the expected outcome, not an early exit — the
   cap is a backstop for non-convergence, never a quota of rounds to spend.
8. Stop after four rounds by default. Leave the PR draft and report
   non-convergence instead of continuing an unbounded cycle.

Do not add hosted reviewers to this path merely as another ritual. A later
hosted-review fix invalidates local convergence and requires a fresh local
round.

## Hosted Fallback Path

When a local Codex CLI is unavailable:

### Lean

1. Open a draft PR.
2. Run `refactorpass <pr-number>`, then `grill <pr-number>`.
3. Run `reviewit <pr-number>` for the bounded Gemini Flash and Copilot loop.

### Deep

1. Open a draft PR and run `deepgrill <pr-number>`.
2. Run `reviewit <pr-number> deep`; its final local `deepgrill` receives the
   same PR number and ledger.

Use deep mode for auth, crypto, secrets, schema/data-shape work, GitHub Actions,
sync tooling, `.claude/skills/**`, large refactors, recurring incidents, or
customer/tenant-variable behavior.

## Review Principles

- Treat generated findings as hypotheses; verify against source before posting.
- **No reviewer in this chain pre-filters by severity or confidence.** Not a
  `grill` sub-agent, not `codex-review`, not an inline `Agent(...)` prompt you
  write yourself. Each reports everything with a severity and confidence
  attached; the filtering happens one level up, where every lens is visible at
  once and each claim can be checked against the diff. A finding suppressed
  inside the reviewer is unrecoverable; a low-scored finding costs one line to
  dismiss. See [`MODEL_NOTES.md`](MODEL_NOTES.md) §1.
- The agent matrix is a ceiling, not a floor. Run only the lenses whose signals
  appear in the diff, and never add an agent to re-check another agent's work.
  See [`MODEL_NOTES.md`](MODEL_NOTES.md) §2–§3.
- Fix every valid in-scope finding. Dismiss false positives with evidence in the
  thread.
- **A round that only finds test, fixture, and comment work is the signal to
  ship, not to keep going.** It means the product converged and the review has
  turned to auditing its own artifacts. That surface is self-renewing — each
  round's hardening gives the next round new assertions to mutate — so the
  findings never run out and their existence is not evidence more review is
  warranted. Recommend the ship step and move anything genuinely worth doing to
  a follow-up issue.
- Defer only genuinely large architectural work and link the tracking issue.
- A fix without a preceding inline finding, a finding without a reply, or a
  resolved thread without a visible disposition is a failed pass.
- Never copy sensitive source, credentials, private data, or model logs into PR
  metadata.
- Stop at the configured cap and preserve the draft PR on non-convergence.

## Cross-references

- [`MODEL_NOTES.md`](MODEL_NOTES.md) — prompt-authoring deltas for the current
  default model; read before editing any skill or agent.
- [`references/local-review-ledger.md`](references/local-review-ledger.md) — the
  PR-thread ledger contract, including the shared docs/config-only changeset
  classification every skill skips on.
- [`skills/refactorpass/SKILL.md`](skills/refactorpass/SKILL.md) ·
  [`skills/grill/SKILL.md`](skills/grill/SKILL.md) ·
  [`skills/deepgrill/SKILL.md`](skills/deepgrill/SKILL.md) ·
  [`skills/codex-review/SKILL.md`](skills/codex-review/SKILL.md) — the local
  convergence lanes.
- [`skills/reviewit/SKILL.md`](skills/reviewit/SKILL.md) — the hosted fallback,
  including the `tier=flash` cost rule.
- [`skills/review-accessibility/SKILL.md`](skills/review-accessibility/SKILL.md)
  — optional, human-triggered a11y pass; opens its own PR and is not part of
  either path above.
- `/pushit` and `/review-cycle` are retired; their stubs are gone, so old
  invocations resolve to nothing.
