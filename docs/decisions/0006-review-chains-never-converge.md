# 0006 — The Claude and Codex review chains never converge

- Status: accepted
- Date: 2026-08-30

## Scope

This record covers the review-chain skill texts of the Claude lineage (this
repo, `.claude/skills/{refactorpass,critique,deepcritique,reviewit,copilot-review,codex-review}/SKILL.md`
and `.claude/agents/*.md`) and the Codex lineage (`codex-platform`
`.codex/skills/**`, and `gemini-platform` `.agents/skills/**`, which is ported
from the Codex tree — their `copilot-review` skills are byte-identical modulo
engine names). It is the standing record that the other decision records in
this directory point at, and the one a future cleanup is most likely to
violate.

## The decision

The two lineages' review-chain prompts are **never** to be unified — not in
either direction, not partially by "tidying" near-identical passages, not as a
side effect of a rename, a repo restructure, or a parity-lint fix. What
converges is the engine-neutral **protocol**: the vendored local-review-ledger
contract, the PR-as-ledger model, the four-rung severity ladder (`blocking`,
`major`, `minor`, `nit`), the once-per-engine refactor latch, and the
role-based relay in `REVIEW_WORKFLOW.md`, which deliberately never names an
engine. A protocol change ports to all three trees — each time rewritten in the
target lineage's own calibration, never by copying prose across the boundary.

## Why prompt convergence is a defect, not a cleanup

Every file under a skills tree is a prompt, and a prompt is calibration for the
model family that runs it. `.claude/MODEL_NOTES.md` records that a phrasing
that helped on one model _generation_ can actively hurt on the next; across
model _families_ the transfer is strictly worse, and §1 draws the boundary
explicitly: do not retune another vendor's prompt from a Claude release note —
measure first. Records 0001–0005 are the measured instances: the same
underlying goal (a fresh-eyed review, a high-signal finding list, one cleanup
pass, a bounded hosted-review loop, all valid findings resolved) is reached by
_different_ instructions per family, because the instructions compensate for
different default behaviours.

The deeper reason is what the two-class architecture is for. The review
protocol's value comes from an independent second opinion: a Codex pass reads
the PR cold, calibrated differently from the Claude pass that preceded it.
Converging the prompts erodes exactly that independence — two engines running
the same text are one reviewer with two billing accounts. The divergence is not
a cost of the architecture; it is the product.

## The failure mode this record forestalls

The chains' skill files are similar enough to invite unification: same names,
same phase shapes, long shared protocol passages. Without a standing record, the
distinction erodes the first time someone diffs `critique` across trees, sees
90% overlap, and "fixes" the rest — and the remaining 10% is precisely the
deliberate calibration catalogued in records 0001–0005. The parity lint exists
to catch _accidental_ drift in the shared protocol surface; this record and its
siblings are its allowlist for the remainder. When the lint flags a divergence
that is in fact deliberate, the fix is a new decision record here — never a
cross-lineage edit that makes the texts match.
