# The review learning loop (Gemini surface)

The routing rule, the addendum contract, and the harvest step are engine-neutral
and documented once, canonically, at
[loomantix/claude-platform `docs/review-learning-loop.md`](https://github.com/loomantix/claude-platform/blob/main/docs/review-learning-loop.md).
Read that first. This page carries only what is specific to the Gemini surface.

## Upstream-owned here

These are rewritten on every sync. A lesson written into one of them **in a
consumer repo** is reverted by the next sync PR.

- `.agents/references/roles/*.md` — the finder lenses.
- `.agents/skills/critique/SKILL.md` and `.agents/skills/deepcritique/SKILL.md`.
- `.agents/REVIEW_WORKFLOW.md`.

A generic lesson belongs in one of those files **in this repo**, shipped to
consumers when the `sync-v1` tag advances.

## Consumer-owned

`.review/addendum.local.md` at the consumer repo root — engine-neutral, read by
every engine, in no sync manifest and in no consumer's
`allowed_destinations`. Each role prompt above and the `critique` skill tell the
reviewer to read it when present and to proceed from the prompt alone when it is
absent.

Do not add `.review/**` to a consumer allowlist and do not bootstrap the file
with a `create_if_missing:` sync target. Being unreachable from upstream is the
property the file exists for.
