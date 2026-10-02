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
  Configuration or a prompt surface under a test-named directory is not test
  code and counts toward the limit.
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

## Amendment shipped with this record

**To [0010](0010-assurance-control-matrix.md)**, "The human-glance gate". That
section says the gate's inputs are the file classifications and nothing else,
and that line count is never an automatic exemption. This record adds line count
as a second input, in one bounded form:

- It is a recommendation to a human, who overrides it by asking for the chain.
  Where no human is present to answer, the gate is unchanged: `agent-loop` reads
  `skip` alone, and a pass a runner schedules does not gate.
- It does not ignore reach, which was 0010's objection to sizing. The
  recommendation names every tier trigger the diff matches.
- File kind still decides `skip`, the one outcome that stops without asking.

0010's rule stands everywhere else: size selects no tier, lowers no control, and
exempts nothing from a chain a human asked for.

## Consequences

Small changes cost one read instead of a chain, and the human decides when a
trigger warrants more. The risk moves to that read: a one-line defect on a
sensitive path now depends on a person noticing the named trigger. The limit is
a constant in the classifier, so changing it is one reviewed edit, and the
`review: human-glance` label and `agent-loop` are unchanged because both read
`skip`.
