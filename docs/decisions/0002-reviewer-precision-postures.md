# 0002 — Reviewer precision postures: report-everything-scored (Claude) vs high-precision (Codex, Gemini)

- Status: accepted
- Date: 2026-08-30

## Divergent files

- `.claude/skills/critique/SKILL.md` and `.claude/agents/code-reviewer.md`
  (this repo) — finder prompts request everything, scored
- `.claude/skills/codex-review/SKILL.md` (this repo) — the cross-engine
  contract deliberately uses the Codex posture
- `codex-platform` `.codex/skills/critique/SKILL.md` — "Still keep the
  reporting bar high"
- `gemini-platform` `.agents/skills/critique/SKILL.md` — same wording
- Rationale: `.claude/MODEL_NOTES.md` §1

## The behavioural difference

The Claude chain forbids self-filtering inside a finder: every lens prompt must
"request every plausible finding with severity and `file:line`" and must "not
ask finders to suppress findings by confidence"; the `code-reviewer` agent
attaches both a severity and a confidence score to each finding so the caller
can cut the list wherever it wants. Narrowing is a disposition rule enforced
one level up, in the orchestrator's verify pass.

The Codex and Gemini chains instruct the reviewer itself to hold a high
precision bar: "only report specific, actionable findings with file/line
evidence. If a suspected issue cannot be supported, dismiss it privately or
list it as dismissed with the evidence that disproved it." Consistently,
`/codex-review` — Claude's own skill for invoking Codex — asks it for "only
high-confidence material findings", terse.

## Why it stands

This is per-model calibration, measured, not drift. `MODEL_NOTES.md` §1 records
both halves. Architecturally, a finding suppressed inside the finder's own
prompt is unrecoverable — the caller never learns it existed — while a
low-scored finding costs one line to cut; so find, score, and cut belong in
three separate places. Model-specifically, the current Claude models follow a
suppression instruction literally while reporting with high precision _and_
high recall, so a self-suppression instruction there is close to a pure loss:
the model obeys, reports less, and gives up little false-positive noise in
exchange.

None of that transfers to another model family. The Codex chain's terse
high-confidence contract is its own calibration and works as-is; MODEL*NOTES §1
says explicitly not to retune another vendor's prompt from a Claude release
note. Both chains apply the same actionability bar at disposition time — the
divergence is only about \_where* the filter runs, and each engine's placement
follows its measured behaviour. Copying either posture across engine lines
would either drop real defects (suppression pushed into Claude finders) or
flood a chain built around a terse contract (report-everything pushed into
Codex/Gemini).
