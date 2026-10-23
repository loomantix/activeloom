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
  point, unless the review override below applies.
- **The classifier decides.** `classify-changeset` reports `dependencyUpdate`
  when every review-significant file is a dependency pin change: a lockfile, a
  manifest or workspace-catalog version line, a GitHub Actions `uses:` version
  or SHA pin, a container image tag or digest, or a submodule gitlink. It reads
  each file's changed lines and keeps any edit it does not recognise on the
  normal route. `dependencyPaths` lists the pin changes either way.
- **The classifier is a best-effort heuristic.** It pattern-matches lines; it
  does not parse the manifest, lockfile, Dockerfile, or workflow, so it cannot
  prove a change is dependency-only. Review rounds on its first version found
  string syntax it read as a pin more than once, and each fix narrowed one
  pattern. A parser-based classifier is tracked in
  [#456](https://github.com/loomantix/activeloom/issues/456).
- **Review override.** A human who directly asks for a dependency update to be
  reviewed anyway, a current or previously applied `review: full` label on the
  PR, or a matching always-Deep repository tier extension puts it back on the
  review route. Read all label-event pages; label removal alone does not revoke
  the request. A matching tier extension is a standing request for Deep. The entry point classifies with `--force-review` added, and
  `classify-changeset` then reports `dependencyUpdate: false` and
  `dependencyReviewForced: true` and keeps `dependencyPaths`. The range then
  follows the mixed-range rule below, except that the chain reviews the
  dependency files as ordinary files and they can set the tier. The analysis
  still runs at the converged head.
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
  chain for a mixed range. Accept only the authenticated actor's latest unedited
  report, with GraphQL `lastEditedAt` null; edited or unavailable evidence blocks
  readiness and shipping. First-party pins also require evidence covering review
  or human acceptance of the exact upstream changes before installation or a
  clear verdict; organization ownership and branch reachability are insufficient.
  It writes no ledger run, roster, tier marker,
  attestation, or review telemetry record.

## Consequences

Dependency PRs stop consuming review rounds and start producing evidence a
reviewer would otherwise have to ask for: the tests that ran and the signal to
watch. The classifier's line recognition is a heuristic in both directions: a
bump it misses runs the normal route (the safe direction), and content it
wrongly reads as a pin skips the chain unless someone applies the review
override. A misrecognised file is fixed in the classifier, and the override is
the way past it in the meantime. Only a direct or standing human request or label application applies it,
never a session's own reading of the diff. The 0014 routine-dependency glance
rule is removed, since the analysis covers every dependency update rather than
one class of patch bump.
