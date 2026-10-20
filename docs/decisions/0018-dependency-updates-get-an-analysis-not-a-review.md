# 0018 — Dependency updates get a dependency analysis, not a review chain

- Status: accepted
- Date: 2026-10-20
- Supersedes: the dependency half of [0014](0014-dependency-review-tier-and-reviewit-boundaries.md). Its hosted-review half stands.

## Context

[0014](0014-dependency-review-tier-and-reviewit-boundaries.md) kept dependency
bumps out of Deep but left them on the Lean chain, with a human-glance
recommendation for validated routine development-dependency patches. That still
spent a review chain on a diff with nothing in it to critique: a manifest,
lockfile, Actions pin, or image digest change carries no code from this
repository. Its risk is upstream code, the consumers here that run it, whether
their tests pass, and whether a break would show after merge. Lean lenses do not
ask those questions, and the routine-patch recommendation answered them only
for one narrow class of bump, with the evidence left to the session.

## Decision

- **No review chain on a dependency update.** Not Lean, not Deep, not a
  human-glance recommendation or auto-glance, on any engine and from any entry
  point, including an explicit request to review it anyway.
- **The classifier decides.** `classify-changeset` reports `dependencyUpdate`
  when every review-significant file is a dependency pin change: a lockfile, a
  manifest or workspace-catalog version line, a GitHub Actions `uses:` version
  or SHA pin, a container image tag or digest, or a submodule gitlink. It reads
  each file's changed lines and fails closed — any other edit keeps the range on
  the normal route. `dependencyPaths` lists the pin changes either way.
- **The dependency analysis replaces the chain.** One rendered skill,
  `dep-analysis`, on every harness: what could break (from → to, release notes,
  breaking changes, security fixes, consumers and their tiers), the existing
  tests that exercise those paths (run with the repository's own gate commands),
  the concrete signal that would show a break after merge, and a verdict. When
  tests or signals fall short it recommends a separate issue documenting the
  gaps, to close before merge.
- **Mixed ranges split by part.** A bump plus code: the code follows the normal
  gate and tier, and the dependency files get the analysis after the chain
  converges, at the converged head. The dependency files never set the tier.
- **One recorded outcome.** The analysis posts a PR comment whose first line is
  `<!-- dependency-analysis:v1 head=<sha> verdict=<clear|gaps|broken|blocked> -->`.
  An orchestrator ship gate requires the latest one to say `verdict=clear` at
  the exact current head — alone for a dependency update, beside a converged
  chain for a mixed range. It writes no ledger run, roster, tier marker,
  attestation, or review telemetry record.

## Consequences

Dependency PRs stop consuming review rounds and start producing evidence a
reviewer would otherwise have to ask for: the tests that ran and the signal to
watch. The classifier's line recognition is a heuristic; a bump it misses runs
the normal route (the safe direction), and a misrecognised file is fixed in the
classifier, not argued past in a session. The 0014 routine-dependency glance
rule is removed, since the analysis covers every dependency update rather than
one class of patch bump.
