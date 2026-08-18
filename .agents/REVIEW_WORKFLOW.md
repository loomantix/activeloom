# Review Workflow

This file is synced from `gemini-platform` into consumer repos. Consumer-specific
edits will be overwritten on the next sync.

## PR-First Rule

Open a draft pull request before any structured review cleanup such as
`refactorpass`, or adversarial review. Review passes use GitHub review threads
as durable shared context: post each verified finding inline before editing, push
the correction, reply with the fix and validation, then resolve the thread. Every
pass must read resolved as well as unresolved threads before reviewing the current head.

Load [the local review ledger](references/local-review-ledger.md) before running
`refactorpass`, `critique`, `deepcritique`, `pr-critique`, or local review hooks.

## Multi-Model Local Convergence Path

Use this path when multiple local agent engines (Antigravity/Gemini, Claude, Codex) are available:

1. Make the change, run focused validation, and create a clean local commit.
2. Push the feature branch and open or reuse its draft PR. Record the PR number,
   head SHA, and all existing review threads before any reviewer runs.
3. Fetch the target base, record its immutable commit SHA, and give that exact
   SHA to both reviewers for the round. Neither reviewer may re-resolve a
   mutable remote-tracking ref independently.
4. In a fresh session, run `deepcritique <pr-number>`. Read the PR ledger,
   post every confirmed finding inline before editing, fix, validate, commit,
   push, reply, and resolve.
5. On that resulting HEAD, run a fresh adversarial review against the
   same base and PR ledger. Apply the same comment/fix/reply/resolve contract.
6. Classify committed review fixes as `material` or `minor`. A material fix
   affects behavior, correctness, security/privacy, data safety, compatibility,
   deployment/sync integrity, or another substantive contract; restart at the first
   engine when either pass makes one. Minor-only fixes are validated and kept but do
   not restart the cycle. Convergence requires one complete round with no material fixes.

   **The chain gets cheaper as it repeats:**
   - **The refactor pass runs once per engine per PR.** A second cleanup pass over
     an already-simplified diff returns naming and shape churn, which moves the
     head and re-stales the other engine's attestation for nothing that ships.
   - **Rounds 1–2 are adversarial; round 3 and later are convergence rounds.**
     Once both engines have read the change cold twice, the remaining findings
     are mostly about the review's own artifacts. A convergence round runs only
     the lanes that can find a reason not to deploy, changes the PR only for a
     realistically reachable blocking defect, defers everything else, creates
     an issue only for an urgent high-impact follow-up, and ends the loop as soon
     as it finds no blocker.

7. Cap the loop at four rounds unless the consumer explicitly configures a
   different positive bound. At cap exhaustion, stop, preserve the branch,
   worktree, and draft PR, and report non-convergence. Do not mark it ready.
8. After convergence, require every local-review thread to contain a disposition
   reply and be resolved, revalidate the exact PR head, and mark the PR ready.

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
