# 0003 — refactorpass architecture: proportionate cleanup per harness

- Status: accepted
- Date: 2026-08-30
- Updated: 2026-09-08

## Divergent files

- `.claude/skills/refactorpass/SKILL.md` (this repo) — one read-only cleanup worker
- `codex-platform` `.codex/skills/refactorpass/SKILL.md` — inline "Cleanup
  Matrix"
- `gemini-platform` `.agents/skills/refactorpass/SKILL.md` — inline "Cleanup
  Matrix"

## The behavioural difference

Claude's `refactorpass` delegates proposals to at most one read-only cleanup
worker, with local analysis as the fallback. The coordinator verifies proposals,
posts findings before edits, and commits the validated corrections. Wrapped
cleanup shares the enclosing pass's single publication and fixed finding set.

Codex carries cleanup judgment inline and defaults to one focused direct pass.
It delegates only substantial independent tracks and applies cleanup when its
clarity or bug-risk benefit justifies the churn. Direct execution is a supported
choice, reported as such, rather than a degraded mode. A no-op is successful.

Gemini retains its three-lane Cleanup Matrix and its existing execution policy.
The absence of `/simplify` does not itself require either agent fan-out or a
fixed number of serial reads.

## Why it changed

The original Claude implementation called `/simplify`, which edits as part of
its contract. Its cleanup exemption conflicted with the v3 ledger's requirement
that a changed result carry a fixed finding. A refactor latch records that
cleanup ran; it does not supply that evidence. Read-only proposals keep cleanup
compatible with both posting order and the enclosing pass's publication limit.
The per-engine difference is now analysis breadth, not mutation ownership.

What is converged — and what the parity lint should actually check — is the
behavioural contract around the pass: it is PR-first, runs at most once per PR
per engine on a `local-review-refactor:v1` latch, skips docs/config-only
changesets, and lands as verified commits with prior findings in the shared ledger. All
three files agree on that. The mechanism inside the pass is expected to differ
and should be allowlisted citing this record.
