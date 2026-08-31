# 0005 — copilot-review Fix Bias: Codex and Gemini only

- Status: accepted
- Date: 2026-08-30

## Divergent files

- `.claude/skills/copilot-review/SKILL.md` (this repo) — no Fix Bias principle
- `codex-platform` `.codex/skills/copilot-review/SKILL.md` — "Fix bias" core
  principle
- `gemini-platform` `.agents/skills/copilot-review/SKILL.md` — identical to the
  Codex text modulo engine names

## The behavioural difference

The Codex and Gemini `copilot-review` skills carry an explicit core principle:
"**Fix bias**: Fix every valid finding, including nits. Defer only extremely
large follow-up refactors, roughly 300+ lines or cross-cutting rewrites, and
track them in GitHub issues."

Claude's version of the same skill has no such bias. Its core principles
include the opposite pressure — "**Scope discipline**: Defer scope-creep issues
to separate GitHub issues" — and its per-comment triage is a neutral three-way
Fix / Defer / Dismiss, where a defer requires a tracking issue URL in the
recorded resolution.

## Why it stands

The instruction each engine carries is the one that pushes against that
engine's default failure mode, so the texts differ precisely because the
intended behaviour is the same: every valid Copilot finding ends up fixed,
deferred-with-a-ticket, or dismissed-with-evidence. The Codex-lineage engines
need the explicit bias to keep valid nits from ending as
acknowledged-but-unaddressed replies. Claude's calibration errs the other way:
it fixes eagerly, and the risk worth writing down is scope creep, so its text
spends its emphasis on deferral discipline instead.

Copying the Fix Bias paragraph into the Claude skill would amplify an
already-present tendency toward churn; deleting it from the Codex and Gemini
skills would regress them to under-fixing. The parity lint should allowlist
the presence of this principle in exactly two of the three trees, citing this
record.
