# 0013 — Invoking a review is not an escalation

- Status: accepted
- Date: 2026-09-23

## Context

Two misreadings made small changes expensive to review. A request such as
"review this PR" or "run the auto review chain" was taken as trigger 6, which
both overrides the human-glance gate and selects Deep. And a file the
classifier marks review-significant by basename, such as `package.json`, kept a
tooling-only range out of human glance, which invited sessions to argue the
gate away on their own reading of the diff.

## Decision

- Trigger 6 is a direct request for a _deep_ review, or for this change to be
  reviewed despite human glance. Invoking an entry point, asking for "a
  review", or asking for the automatic chain is none of these: the entry point
  applies the gate and resolves the tier as usual.
- A request for the automatic chain names the runner, not a plan. The runner
  takes the tier's order from the review profile as returned, with no added
  engines, repeated steps, or Deep order. Lean's existing cap of two passes per
  engine, with early exit on convergence, already bounds the default cycle.
- The classifier owns human glance. A session does not overrule
  `"skip": false`; a misclassified path is fixed in the classifier rule, via
  an issue that names the path and the rule.

## Consequences

The gate stays mechanical, and the fix for a bad classification lands once, in
the classifier, instead of in every session's judgment. The change is to the
engine-neutral protocol in `REVIEW_WORKFLOW.md` only, so it ports to all three
trees without touching the per-lineage skill prompts that record 0006 keeps
apart.
