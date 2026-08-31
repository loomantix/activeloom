# 0001 — Pre-review session gate: hard gate in Claude and Gemini, advisory in Codex

- Status: accepted
- Date: 2026-08-30

## Divergent files

- `.claude/skills/deepcritique/SKILL.md` (this repo) — "Context-window check"
- `gemini-platform` `.agents/skills/deepcritique/SKILL.md` — "Context Window Check"
- `codex-platform` `.codex/skills/deepcritique/SKILL.md` — "Context And Session Check"

## The behavioural difference

All three engines check, before starting `deepcritique`, whether the current
session authored the change or carries dense implementation context. What they
do with a "yes" differs:

- **Claude** stops: "Proceed in the current session only after an explicit
  override." The stated rationale is review quality, not just cost — "A larger
  context window does not relax this gate: authoring rationale anchors the
  reviewer" — grounded in `.claude/MODEL_NOTES.md` §8.
- **Gemini** also stops ("Do not proceed in the current session unless the user
  explicitly overrides"), but its stated rationale is cache headroom and chain
  cost.
- **Codex** advises and continues: "This is cost and quality advice, not a
  workflow gate. Do not stop, defer the authorized task, or require a new
  session solely because context is heavy."

## Why it stands

The Claude gate is a relevance guardrail, not a capacity guardrail. A session
that just wrote the code re-reads its own diff already holding the rationale
that produced it — the opposite of the fresh-eyes stance the adversarial pass
exists to provide — and that failure mode gets _stronger_ as context windows
grow, because more authoring history survives to pollute the pass
(`MODEL_NOTES.md` §8). Softening the Claude gate to advice re-opens a measured
regression.

The Codex posture is equally deliberate in the other direction: Codex runs
inside relay protocols where an unconditional stop would abandon an authorized
task mid-chain, and its skill text spells out the narrow conditions under which
stopping is correct (an owed handoff to another engine, an explicit user
boundary, an unsafe runtime). Hardening it to a gate would convert a cost
signal into spurious protocol breaks.

Gemini keeps the hard stop on cost grounds; its fan-out (up to seven lanes
inheriting session cache state) makes a heavy authoring context materially more
expensive there than a one-shot pass would be.

Converging any of the three onto another's posture would fix nothing and break
the engine it was copied onto. The parity lint must treat this as an
allowlisted divergence, citing this record.
