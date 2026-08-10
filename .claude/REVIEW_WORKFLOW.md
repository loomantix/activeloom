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
`refactorpass`, `critique`, `deepcritique`, `codex-review`, or local review hooks.

## Local Convergence Path

Use this path when both local engines are available:

1. Make the change, validate it, create a clean commit, and open a draft PR.
2. Pin the exact base SHA for the round and give it to both reviewers.
3. Run `codex-review <pr-number>` as a fresh local Codex pass. Read the ledger
   and apply the comment/fix/reply/resolve contract to confirmed findings.
4. On the resulting head, run a fresh Claude `deepcritique <pr-number>` with the
   same ledger contract.
5. Classify fixes by effect, not path or finding severity. `material` includes
   substantive correctness, security/privacy, data-safety, compatibility,
   deployment/sync, or review-integrity changes, including tests or workflows
   needed to prevent a false green. `minor` is low-risk non-behavioral cleanup
   or polish. Restart at Codex after a material fix; retain minor fixes.

   **The chain gets cheaper as it repeats.** Two rules make that happen, and both
   are enforced from the ledger rather than from session memory:
   - **The refactor pass runs once per engine per PR.** A second `/simplify` over
     an already-simplified diff returns naming and shape churn, which moves the
     head and re-stales the other engine's attestation for nothing that ships.
     Each engine's cleanup lane latches on a `local-review-refactor:v1` marker;
     a docs/config-only skip does not consume it.
   - **Rounds 1–2 are adversarial; round 3 and later are convergence rounds.**
     Once both engines have read the change cold twice, the remaining findings
     are mostly about the review's own artifacts. A convergence round runs only
     the lenses that can find a reason not to deploy, changes the PR only for a
     blocking defect, defers everything else to a linked issue, and ends the loop
     as soon as it finds no blocker. Lenses still report everything they find —
     the narrowing is a disposition rule applied by the orchestrator, never an
     instruction to a review agent to withhold by severity or confidence.

6. Converge after one complete Codex-then-Claude round has no material
   transition, every pass has a validated v3 result for its exact reviewed head,
   and every local-review thread has a disposition reply and is resolved. A
   minor A-to-B transition can complete the round without pretending the first
   engine reviewed B; its exact-head attestation remains historical evidence.
7. The wrapper, not review hooks, posts canonical pass/completion attestations
   after validating structured results and the GitHub ledger.
8. Stop after four rounds by default. Leave the PR draft and report
   non-convergence instead of continuing an unbounded cycle.

Do not add hosted reviewers to this path merely as another ritual. A later
hosted-review fix invalidates local convergence and requires a fresh local
round.

## Hosted Fallback Path

When a local Codex CLI is unavailable:

### Lean

1. Open a draft PR.
2. Run `refactorpass <pr-number>`, then `critique <pr-number>`.
3. Run `reviewit <pr-number>` for the bounded Gemini Flash and Copilot loop.

### Deep

1. Open a draft PR and run `deepcritique <pr-number>`.
2. Run `reviewit <pr-number> deep`; its final local `deepcritique` receives the
   same PR number and ledger. That tail `deepcritique` skips the refactor pass —
   step 1 already spent this engine's cleanup latch on the PR.

Use deep mode for auth, crypto, secrets, schema/data-shape work, GitHub Actions,
sync tooling, `.claude/skills/**`, large refactors, recurring incidents, or
customer/tenant-variable behavior.

## Review Principles

- Treat generated findings as hypotheses; verify against source before posting.
- **No reviewer in this chain pre-filters by severity or confidence.** Not a
  `critique` sub-agent, not `codex-review`, not an inline `Agent(...)` prompt you
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
- **A round that only finds non-material test, fixture, comment, or docs polish
  is the signal to ship, not to keep going.** It means the product converged and
  the review has turned to auditing its own artifacts. A test or workflow fix
  needed to prevent a false green remains material and restarts at Codex under
  step 5. Move genuinely useful non-material polish to a follow-up issue.
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
  [`skills/critique/SKILL.md`](skills/critique/SKILL.md) ·
  [`skills/deepcritique/SKILL.md`](skills/deepcritique/SKILL.md) ·
  [`skills/codex-review/SKILL.md`](skills/codex-review/SKILL.md) — the local
  convergence lanes.
- [`skills/reviewit/SKILL.md`](skills/reviewit/SKILL.md) — the hosted fallback,
  including the `tier=flash` cost rule.
- [`skills/review-accessibility/SKILL.md`](skills/review-accessibility/SKILL.md)
  — optional, human-triggered a11y pass; opens its own PR and is not part of
  either path above.
- `/pushit` and `/review-cycle` are retired; their stubs are gone, so old
  invocations resolve to nothing.
