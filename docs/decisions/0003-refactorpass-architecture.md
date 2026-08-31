# 0003 — refactorpass architecture: delegation to /simplify (Claude) vs inline Cleanup Matrix (Codex, Gemini)

- Status: accepted
- Date: 2026-08-30

## Divergent files

- `.claude/skills/refactorpass/SKILL.md` (this repo) — single `/simplify`
  invocation
- `codex-platform` `.codex/skills/refactorpass/SKILL.md` — inline "Cleanup
  Matrix"
- `gemini-platform` `.agents/skills/refactorpass/SKILL.md` — inline "Cleanup
  Matrix"

## The behavioural difference

Claude's `refactorpass` delegates the actual cleanup to the harness-provided
`/simplify` skill: one `Skill(skill="simplify", ...)` call against the PR diff,
then verification, a single `refactor: /simplify pass — <summary>` commit, and
the ledger marker. The skill body is mostly ledger protocol; the cleanup
judgment lives in `/simplify`, which applies the edits it finds — that is its
contract.

Codex and Gemini have no `/simplify` in their harnesses, so their
`refactorpass` carries the cleanup judgment inline as a three-lane Cleanup
Matrix — a simplicity/DRY lane, a correctness-preserving lane, and a
convention/API lane — run as independent subagent reviewers where the runtime
permits, or as three serial local passes otherwise, with the degraded mode
disclosed in the output as `cleanup depth`.

## Why it stands

The divergence is a harness-capability boundary, not two teams solving the same
problem differently by accident. Claude Code ships `/simplify` as a
first-class, separately maintained skill; duplicating its lens list inside
`refactorpass` would create a second copy that drifts from the real one. The
Codex and Gemini runtimes have no such skill to call, so the matrix _is_ the
port of `/simplify`'s judgment into those trees.

What is converged — and what the parity lint should actually check — is the
behavioural contract around the pass: it is PR-first, runs at most once per PR
per engine on a `local-review-refactor:v1` latch, skips docs/config-only
changesets, and lands as verified commits recorded in the shared ledger. All
three files agree on that. The mechanism inside the pass is expected to differ
and should be allowlisted citing this record.
