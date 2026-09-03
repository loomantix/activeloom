# Prompt rendering

Some skills are the same skill on every harness, differing only in vocabulary:
what the todo tool is called, whether a skill is addressed with a leading slash,
which doc a consumer keeps its build config in. Those are single-sourced in
`prompts/` and rendered into each harness root. Everything else stays
per-harness and is held to account by a decision record instead.

## The shape

```
prompts/
  profiles/
    claude.yml        root: .claude
    codex.yml         root: .codex
    agents.yml        root: .agents   (the Gemini harness)
  skills/
    <skill>/SKILL.md  one source per rendered skill, with <<KEY>> placeholders
```

Rendering writes `<root>/skills/<skill>/…` for every profile. Those outputs are
**committed**, because they are what consumers sync and what a reader of a
harness root expects to find — a template in `.claude/skills/` would ship
verbatim to every consumer, since the sync manifest copies those paths with no
substitution of its own.

```bash
python3 scripts/render-prompts.py            # write the harness roots
python3 scripts/render-prompts.py --check    # what CI runs; prints a diff per drift
```

CI's `Rendered prompt roots are current` step runs `--check`, so a stale root or
a hand-edit to a generated file fails the build.

## Adding a rendered skill

Create `prompts/skills/<name>/SKILL.md` and add whatever per-skill values it
needs to the three profiles. The roster is the directory listing — there is no
manifest to keep in step, and so nothing to forget.

## Adding a variable

Add it to all three profiles and use `<<KEY>>` in the source. A placeholder with
no value in some profile fails the render rather than shipping through; so does
a placeholder that survives substitution for any reason.

## Two rules that are not negotiable

**Zero conditionals.** The substitution engine has no branching construct and
none will be added. A skill whose _structure_ must differ between harnesses — a
phase only one engine runs, a different number of steps — is per-harness by
definition. It stays out of `prompts/` and is reconciled by the parity lint
against a `docs/decisions/` record. The moment a renderer grows an `{% if %}`,
the single source stops being readable as the thing that ships, which is the
only property that makes rendering safer than copies.

**Prettier never touches the sources.** `prompts/skills/` is in
`.prettierignore` and must stay there. Prettier's Markdown parser is not
placeholder-aware and corrupts them: it paired the underscores inside
`<<REVIEW_CHAIN_POINTER>>` with a neighbouring `_before_` emphasis span and
rewrote the key to `<<REVIEW*CHAIN_POINTER>>`, which the engine's `<<KEY>>`
pattern no longer matches — so it substituted nothing and rendered a literal
`<<REVIEW*CHAIN_POINTER>>` into all three roots while `--check` passed, because
source and output agreed. The renderer now sweeps its own output for surviving
`<<…>>` delimiters and fails closed, which catches the whole class rather than
that one instance.

Rendered Markdown _is_ formatted, by the renderer itself, with the same pinned
Prettier as the repo-wide check. This is not cosmetic: substituting a value of a
different width into a Markdown table changes the column alignment Prettier
enforces, so an unformatted render would fail the repo's own `Prettier --check`
on its own output.

## Why the source tree is inside the weaponization gate

`.claude/lint-skill-content.py` scans `prompts/skills/` alongside the harness
roots. It has to: one added line in a source is rendered into three roots and
reaches every consumer of all three on the next sync, so leaving the source out
while gating the output would make the gate sidesteppable by editing the more
powerful file.

## What is _not_ rendered

The review chain — `critique`, `deepcritique`, `refactorpass`, `reviewit`,
`copilot-review`, and their siblings — is deliberately never single-sourced.
[`docs/decisions/0006`](decisions/0006-review-chains-never-converge.md) is the
standing record: those prompts are calibration for a specific model family, and
two engines running the same text are one reviewer with two billing accounts.
Measured divergence across the chain is 70–92%, so there is no shared source to
recover even if it were wanted.

Held back for now, pending zero parity-lint residuals: `copilot-review`,
`actions-usage-audit`, `backlog-refinement`.
