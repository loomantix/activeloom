# Backlog refinement — local rubric

> Repository-owned and never synced. It extends the synced core rubric (`core-rubric.md` in the `backlog-refinement` skill) with this repository's instances: its branch, its sensitive paths, its labels, its own transformations. Where this file and the core disagree, this file wins. A rule that would hold in any repository belongs in the core — list it under _Upstream candidates_ until it lands there.
>
> **Local rubric version: 1** — bump on every material change and record the bump in `learnings.local.md`.

## Settings

- **Integration branch:** `TODO(backlog): the branch loop PRs target, e.g. main`
- **Rewrite mode:** `edit` <!-- edit | suggest -->

Priority labels, highest first. An empty marker turns priority-setting off. Keep exactly one marker line.

<!-- priority-labels: priority: critical, priority: high, priority: medium, priority: low -->

Labels on issues a scheduled workflow opens and closes; refinement skips them entirely. Empty means none. Keep exactly one marker line.

<!-- auto-managed-labels: -->

## Priority definitions

_Optional. Leave empty to use the core rubric's tier table, or replace it with this repository's wording._

## Additional disqualifiers

- **`agent-bail: TODO(backlog): name`** — TODO(backlog): this repository's sensitive paths, where a non-trivial change needs human review even with green CI. Name the paths and the invariant they protect. A one-line typo fix there may still be agent work; anything touching the invariant is not.

## Additional transformations

_Repository-specific make-ready transformations, in the core rubric's §2 table shape. Empty until an RCA adds one._

## Upstream candidates

_Rules recorded here that would hold in any repository. Propose each to the core rubric upstream, then delete it here once the synced core carries it._
