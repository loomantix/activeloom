# 0019 — The dependency analysis opens a fix PR for test gaps

- Status: accepted
- Date: 2026-10-22
- Amends: [0018](0018-dependency-updates-get-an-analysis-not-a-review.md). Its routing and evidence rules stand.

## Context

Under [0018](0018-dependency-updates-get-an-analysis-not-a-review.md), a
`gaps` verdict recommended an issue and stopped. The commonest gap is a
consumer path that no test reaches through the real dependency. For example,
every spec of an SDK client mocks the client's `send`, so serialization,
signing and error parsing never run in CI. That gap recurs on every later bump
of the same dependency. The work that closes it is mechanical: update the
dependency, and add an offline test at the seam the repository already calls.
Leaving it to a human meant a bot PR waited on an issue that nobody had filed.

## Decision

- **On `gaps`, the analysis opens the issue.** It reuses an open issue or PR
  that already tracks the same gaps or dependencies.
- **When tests alone close every gap, it also opens a draft fix PR.** That PR
  updates the same dependencies and adds the missing tests. Where the analysis
  reproduced an upstream defect, it takes the oldest release that fixes it,
  with a regression test that fails at the base. A missing signal, or a test
  that needs product code, configuration or test tooling, is not eligible: the
  analysis stops at the issue. `--no-fix` skips the PR.
- **The fix PR takes the normal review route.** Its tests are code this
  repository contains, so it is a mixed range. A review chain reviews it, and
  the analysis runs on its dependency files at the converged head, as 0018
  already requires. The analysis never reviews, readies or merges a PR it
  opened. The review workflow and the classifier do not change.
- **The analysis hands the PR back.** It comments on the analysed PR and ends
  with `DEP-FIX-PR: <fix> <head> issue=<i> supersedes=<n>`. An orchestrator
  reads that line as it reads a build's `BUILD-PR` line, and starts a chain for
  the fix PR. A herder relays it in its final report. The analysed PR stays
  unshipped at `gaps`.

## Consequences

A gap of this kind now produces a PR ready for review rather than a
recommendation, and the tests it adds cover the next bump of the same
dependency. Every line the analysis writes still passes a review chain before
it merges. A design that let a PR of tests plus dependency pins skip the chain
was considered and rejected: agent-written tests would then merge with no
reviewer having read them.
