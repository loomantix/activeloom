# 0020 — The ship-staging outbox is vendored into every root; the skill is not

- Status: accepted
- Date: 2026-11-23

## Context

`ship-staging` is a hand-maintained skill in the Codex and Gemini roots only,
with its CI helper `scripts/check-pr-checks.py` beside it. Claude has no
`ship-staging` skill. Claude-driven orchestration reaches a ship through the
other roots' skill.

The durable notification outbox adds one delivery helper that every ship and
every later `flush` must share. Three hand-maintained copies would drift, and
a delivered or pending intent would then depend on which root's copy touched
it last.

## Decision

- The helper has one source, `scripts/ship-notification-outbox.py`.
  `scripts/render-prompts.py` vendors it verbatim to
  `<root>/skills/ship-staging/scripts/notification-outbox.py` in every root,
  using the `VENDORED_DOCUMENTS` mechanism from record 0017. A hand-edit fails
  the render check.
- The Claude root therefore holds `skills/ship-staging/` containing only the
  helper. Any root's session can then flush or inspect pending notifications
  with the same code.
- The skill text and `check-pr-checks.py` stay Codex/Gemini-only. Adding a
  Claude `ship-staging` skill is a separate decision.

## Consequences

`ship-staging` becomes a skill directory shared by three roots. Its parity
residual is the Codex/Gemini-only skill text and CI helper. That divergence is
deliberate, so `parity-allowlist.yml` records it against this record.
