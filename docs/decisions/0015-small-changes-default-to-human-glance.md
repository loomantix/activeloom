# 0015 — Small changes default to a human glance

- Status: accepted
- Date: 2026-10-01

## Context

The human-glance gate stopped only ranges with no review-significant file. A
range of a few lines of code or configuration went to a review chain, and when
it matched a tier trigger it went to the Deep one. At that size the person
merging reads every line anyway, so the chain added cost and almost no
coverage. Tier triggers measure what a missed defect reaches; they have no way
to credit that the code change fits on one screen.

## Decision

- `classify-changeset` returns `smallChange` beside `skip`. It is true when the
  range is review-significant and changes fewer than 20 non-blank lines in the
  `app` class: application code, configuration, and prompt surfaces. Test,
  docs, and generated lines do not count. A test-only range is therefore small
  at any size, so the recommendation states the test lines beside the count.
- The answer fails closed. A lockfile, dependency manifest, or submodule keeps
  a range out, because a version bump pulls in code its line count does not
  measure. So does a review-significant file with no line churn. Comment lines
  count until a lexer can exclude them.
- On `smallChange`, an entry point recommends a human glance and stops with no
  side effects, as it does on `skip`. The recommendation names any of triggers
  1–5 the diff matches, so declining the chain is an informed choice.
- A human overrides by asking for the chain. The tier then resolves from
  triggers 1–5; the override is not a request for Deep.
- Both glance lines state the repository's merge gate when one applies to the
  touched paths. "Read the diff and merge" is wrong advice where a merge
  deploys.
- `agent-loop` keeps gating on `skip` alone: a recommendation needs a human to
  act on it. The session that starts an automatic chain has that human, so it
  applies the full gate, and the passes its runner schedules do not gate again.

## Consequences

Small changes cost one read instead of a chain, and the human decides when a
trigger warrants more. The risk moves to that read: a one-line defect on a
sensitive path now depends on a person noticing the named trigger. The limit is
a constant in the classifier, so changing it is one reviewed edit, and the
`review: human-glance` label and `agent-loop` are unchanged because both read
`skip`.
