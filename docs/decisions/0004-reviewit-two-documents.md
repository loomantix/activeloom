# 0004 — reviewit: tier-authority orchestrator (Claude) vs resumable thin orchestrator (Codex, Gemini)

- Status: accepted
- Date: 2026-08-30

## Divergent files

- `.claude/skills/reviewit/SKILL.md` (this repo) — 744 lines
- `codex-platform` `.codex/skills/reviewit/SKILL.md` — 141 lines
- `gemini-platform` `.agents/skills/reviewit/SKILL.md` — 141 lines

## The behavioural difference

All three fire the same two hosted reviewers (Gemini Flash + Copilot) at the
same iteration watermark with the same staggered handling, the same lean cap of
2 and deep cap of 4, and the same no-auto-trigger rule. Around that shared
core, the documents are structured for different session realities:

- **Claude's** is the tier-authority and budget-interaction document. It
  resolves the effective review tier from the recorded PR marker in both
  directions (a `deep` argument from an internal handoff is an assertion, not
  an override; a direct human request is recorded as a trigger), and in deep
  mode it runs a mid-loop cost-shift checkpoint between iterations 2→3 and 3→4
  that pauses and asks the user — continue, bail to the final `/deepcritique`,
  or merge as-is — before spending another paid iteration.
- **Codex's and Gemini's** are re-entrancy documents. They add a `--resume`
  mode backed by a local state-file schema (PR head SHA, `copilotRequested`,
  handled comment ids, the exact resume command) so an interrupted run can be
  reconstructed from `gh pr view` and continued without re-firing reviewers,
  and they take the resolved tier as given ("`deep` … asserts the resolved
  review tier rather than choosing it").

## Why it stands

These are two different documents, not one document that drifted apart, and the
744-vs-141 line gap is the design, not decay. Claude sessions running this
skill are long-lived and interactive — the harness supports asking the user a
question mid-loop, so the paid-budget checkpoint and tier arbitration live
there. Codex and Gemini runs are more often relaunched between phases, so the
load-bearing feature is cheap re-entry, and tier authority is deliberately kept
out of the thin orchestrator and left with the ledger.

A parity lint comparing these files textually, or by length, would report noise
forever. The comparable surface is the shared invariant list above (reviewers,
watermark, caps, stagger, no auto-trigger, ledger-recorded resolutions); the
rest is allowlisted citing this record. Porting the cost-shift checkpoint into
the Codex/Gemini documents or the state schema into Claude's would each add
machinery its host session model cannot use.
