# 0006 — The Claude and Codex review chains never converge

- Status: accepted
- Date: 2026-08-30
- Amended: 2026-10-04 — share engine-neutral policy through static partials.

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

The two lineages retain separate review methods, lens instructions, calibration,
and orchestration. Similar wording alone does not justify unifying those parts.
What converges is the engine-neutral **policy and protocol**: human-glance
eligibility, tier rules, evidence requirements, the vendored local-review-ledger
contract, the PR-as-ledger model, the four-rung severity ladder (`blocking`,
`major`, `minor`, `nit`), the once-per-engine refactor latch, and the
role-based relay in `REVIEW_WORKFLOW.md`, which deliberately never names an
engine. Shared policy can be authored once in `prompts/partials/review/` and
included by the separate engine templates in `prompts/review/`. A policy edit
then reaches every entry point through generation rather than manual copying.

Composition uses static Handlebars partials. Engine-specific branches belong in
their own templates; helpers, conditionals, and dynamic partial selection are
rejected. Extract only shared policy, preserving the rendered text during a
migration. Engine-specific wording remains in its template, even where another
engine expresses a similar rule. See [prompt rendering](../prompt-rendering.md).

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
Preserving different review methods and calibration supports that independence.
Shared severity definitions or human-glance eligibility do not require identical
review methods. The original blanket prohibition on sharing passages made a
single policy correction require edits across many entry points. The boundary
is now the instruction's purpose, rather than whether it lives in a skill file.

## The failure mode this record forestalls

The chains' skill files are similar enough to invite unification: same names,
same phase shapes, long shared protocol passages. Without a standing record, the
distinction erodes the first time someone diffs `critique` across trees, sees
90% overlap, and "fixes" the rest — and the remaining 10% is precisely the
deliberate calibration catalogued in records 0001–0005. The parity lint exists
to catch _accidental_ drift in the shared protocol surface; this record and its
siblings are its allowlist for the remainder. When the lint flags a divergence
that is in fact deliberate, the fix is a new decision record here — never a
cross-lineage edit that makes calibrated review instructions match. A source
migration of already-shared policy must leave the rendered instructions unchanged;
subsequent policy changes are reviewed separately from that migration.
