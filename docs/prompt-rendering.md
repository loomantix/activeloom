# Prompt rendering

Some skills are the same skill on every harness, differing only in vocabulary:
what the todo tool is called, whether a skill is addressed with a leading slash,
which doc a consumer keeps its build config in. Those are single-sourced in
`prompts/` and rendered into each harness root. Review documents use separate
engine templates that compose shared policy partials. Their review methods and
calibration remain per-harness, with differences recorded in decision records.

## The shape

```
prompts/
  profiles/
    claude.yml        root: .claude
    codex.yml         root: .codex
    agents.yml        root: .agents   (the Gemini harness)
  skills/
    <skill>/SKILL.md  one source per rendered skill, with <<KEY>> placeholders
  partials/review/
    <policy>.hbs     shared review policy, included with {{> review/<policy>}}
  review/
    <engine>/       separate claude, codex, and agents templates
      REVIEW_WORKFLOW.md.hbs
      skills/<skill>/SKILL.md.hbs
```

Rendering writes `<root>/skills/<skill>/…` for every profile. Those outputs are
**committed**, because they are the harness-specific distribution artifacts and
what a reader of a harness root expects to find. A consumer receives only the
paths selected by its own sync configuration; rendering does not imply that
every consumer syncs all three roots.

The renderer also writes the [vendored documents](#vendored-documents), whose
source is not in `prompts/` at all.

```bash
npm ci --prefix prompts --ignore-scripts   # pinned authoring-only Handlebars
python3 scripts/render-prompts.py            # write the harness roots
python3 scripts/render-prompts.py --check    # what CI runs; prints a diff per drift
```

CI's `Rendered prompt roots are current` step runs `--check`, so a stale root or
a hand-edit to a generated file fails the build.

A rendered skill's directory in a harness root is **wholly owned** by the
renderer. `--check` walks those directories and reports anything the render did
not emit as `unowned`; a write-mode render deletes it. This is stricter than
comparing the render against the generated-path inventory, and deliberately so:
a file in neither the inventory nor the render — a hand-added `EXTRA.md`, a
`scripts/` addition that a `SKILL.md` then sources — was previously in no set
the gate compared and so was reported by nothing at all. The only exceptions are
the build artifacts the render already excludes at the source (`__pycache__`,
`.pyc`, `.pyo`), because CI's own compile step drops those next to the rendered issue
scripts. Anything else that belongs in a rendered skill belongs in
`prompts/skills/<skill>/`.

## Composed review documents

Edit shared policy in `prompts/partials/review/`; edit engine-specific instructions
in the matching `prompts/review/<engine>/` template. Include policy with a static
Handlebars call such as `{{> review/human-glance-gate}}`. Vocabulary still uses
the existing `<<KEY>>` profile substitutions, applied after composition.

The initial shared policy covers the human-glance entry gate and workflow,
findings before telemetry emission, finding severity, and tier non-triggers.
The routine dependency recommendation lives only in `human-glance.hbs`.
Different review lenses, launch instructions, and calibrated wording stay in the
engine templates. The parity lint continues comparing these engine-specific
outputs; composition does not exempt them from that check.

`COMPOSED_DOCUMENTS` in `scripts/render-prompts.py` declares the exact Markdown
files owned by composition. Unlike a whole rendered skill, this owns **no sibling
files or directories**. Scripts and references beside a composed `SKILL.md`
keep their existing owners. New templates need an explicit destination entry;
unknown or missing templates fail. Sources and destinations reject symlinks and
traversal, and missing or cyclic partials fail before publishing any outputs.
To retire a composed document, move its destination into
`RETIRED_COMPOSED_DOCUMENTS`, remove its template and its `.gitattributes` line,
and render; remove the retired entry after the generated inventory no longer
names it. When a profile's `prompt_stack` names the document, drop that entry and
advance `PROMPT_STACK_VERSION` in the same change: the render refuses a stack
entry it no longer generates.

Handlebars is a build dependency in the private `prompts/package.json`; consumers
receive plain Markdown and require no template engine. Composition accepts only
text and static partial includes: no helpers, conditionals, variables, dynamic
names, context overrides, or fallback blocks. Profile vocabulary remains the
only substitution interface. Templates and partials are covered by skill-content
lint, and rendered Markdown is formatted and compared by the existing check.
`.gitattributes` marks the exact composed outputs as generated so GitHub collapses
their diffs by default; source templates and partials remain visible.

Because every `{{` is parsed, a few things that are ordinary in prompt prose fail
composition:

- A literal `{{` — an Actions `${{ … }}` expression, a `gh --template` example —
  must be written `\{{`.
- Handlebars comments (`{{! … }}`) and whitespace control (`{{~> … }}`) are
  rejected along with everything else that is not a plain include.
- A partial is addressed by its path under `prompts/partials/` without the
  suffix; each segment starts with a lower-case letter and holds only lower-case
  letters, digits, and hyphens.
- Every file under `prompts/partials/` must end in `.hbs`, and every file under
  `prompts/review/` must have a `COMPOSED_DOCUMENTS` entry; a stray file in
  either tree fails the render.

Put an include on its own unindented line and end the partial with a newline. The
include line contributes no newline of its own, and indentation before it reaches
only the partial's first line.

Migrate shared policy without changing rendered bytes or prompt-stack identity.
For later behavior changes, edit the partial, advance `PROMPT_STACK_VERSION`
according to the rules below, and regenerate. Verify composition with
`npm test --prefix prompts` and the renderer with `--check`.

## Adding a rendered skill

Create `prompts/skills/<name>/SKILL.md` and add whatever per-skill values it
needs to the three profiles. The roster is the directory listing, so there is
no hand-maintained skill manifest to keep in step. The renderer owns
`prompts/rendered-files.txt`, a generated path inventory used only to remove and
reject retired outputs.

Removing a whole skill is deliberately two-step: delete its source and add its
name temporarily to `RETIRED_SKILLS` in `scripts/render-prompts.py`, render once
to retire the generated files, then remove the name after the committed
inventory is clean. This prevents the inventory from authorizing deletion of
an unrelated hand-authored skill.

## Vendored documents

`VENDORED_DOCUMENTS` in `scripts/render-prompts.py` maps a repo-relative source
to the path it is copied to under **every** harness root. One entry today:

| Source                                                   | Written to                                 |
| -------------------------------------------------------- | ------------------------------------------ |
| `packages/review-ledger/protocol/local-review-ledger.md` | `<root>/references/local-review-ledger.md` |

The ledger protocol is the engine-neutral contract that
[`packages/review-ledger`](../packages/review-ledger/README.md) implements, so
the package is the only place it can be authored without the document and the
code enforcing it drifting apart. A hand-edit to a copy fails `--check` like
any other generated file, and a write-mode render restores it.

Two deliberate differences from a rendered skill:

- **No substitution.** These documents carry no `<<KEY>>` vocabulary, and a
  placeholder resolving per profile would make three documents out of one.
- **No Prettier.** The renderer formats the Markdown it substitutes into,
  because substitution changes table widths. A verbatim copy has nothing to
  reformat, and formatting it here is the one way a copy could stop matching its
  source with nothing failing. The source is ordinary Markdown covered by the
  repo-wide `Prettier --check`, so an unformatted source fails there.

Retiring one is deliberately two-step, exactly like retiring a skill: drop the
entry and add its root-relative destination to `RETIRED_DOCUMENTS`, render once
to delete the copies, then drop the name once the inventory is clean. A dropped
entry alone leaves three files the inventory still names and the ownership
domain no longer admits, which is a render that cannot be made to pass.

Only the exact paths the table produces are renderer-owned. `references/`
itself is not: it holds hand-authored prompts in two roots, and a destination
directory is never swept the way a rendered skill directory is. A destination
inside `skills/` is rejected too: a rendered skill directory is wholly owned by
the skill render, so a document there could collide with a file that skill
emits, and a hand-authored skill directory is not the renderer's to write into.

## The prompt stack manifest

A profile may declare a `prompt_stack`: the review prompt files whose contents
identify that harness's _prompt generation_ for telemetry. Rendering emits it as
`<root>/prompt-stack.json`, and that harness's
`skills/critique/scripts/prompt-stack-hash.js` hashes exactly the files it
names.

The declaration lives here because the renderer is the thing that knows what a
harness root contains. Before, the list lived in the hasher — in two engine
copies of one script that had to keep agreeing on both membership and byte order
forever, enforced by nothing, with two identities minted for one prompt
generation as the failure mode. A declaration naming a file this repository does
not have now fails the render, where previously a rename read as an absent file
and quietly moved every digest at once.

Each engine's digest covers its own root only, and the two are not comparable:
`.claude` and `.codex` hold deliberately different review prompts (see
[`decisions/0006-review-chains-never-converge.md`](decisions/0006-review-chains-never-converge.md)),
so one digest across both harnesses could only be had by converging prompts that
are supposed to differ. What is shared across harnesses is the version below.

`.agents` declares no stack: that harness has no hasher yet, and shipping a
manifest nothing reads would imply a telemetry identity it does not emit.

## The prompt stack version

`PROMPT_STACK_VERSION` at the repo root holds one `MAJOR.MINOR.PATCH` line. The
renderer stamps it into every emitted manifest, the hasher reports it, and it
reaches a telemetry record as `promptStackVersion`.

It answers what a digest cannot. A digest identifies a generation exactly but
does not **order** two of them, so "did findings-per-token improve after that
prompt change" needs a version to say which came first. The two are reported
side by side and never mixed: a version bump that changed no prompt must not
move the digest, and a prompt edit must move the digest whether or not anyone
remembered to bump the version.

The unit is the **whole prompt stack**, not the individual skill. A review pass
loads several of these files together and the thing being compared is the pass,
so a per-skill version would have to be reassembled into a stack version by
every consumer of the telemetry.

- **MAJOR** — a change to the review protocol itself: the ledger contract, the
  severity ladder, what an engine must post before it edits.
- **MINOR** — a behaviour-changing prompt edit. New or removed instructions,
  a changed gate, a lens added to or dropped from a chain. If a reviewer would
  plausibly act differently, it is at least minor.
- **PATCH** — editorial only: wording, formatting, a fixed typo, a clarified
  sentence that changes no instruction.

The active distribution ref (`sync-v2`) is not this version: that tag can advance, so two consumers installed from it at different times may run different prompts. `sync-v1` is a frozen historical pin with no remaining consumers. The CLI package version, distribution ref, prompt-stack version, and per-harness digest identify different things.

## What other tools may read, and what they may assume

Two renderer outputs are now read by tooling outside the renderer, so their
shape is a contract rather than an implementation detail. Change either
deliberately, and tell the readers.

**`prompts/skills/<skill>/` — the rendered roster.** The directory listing _is_
the roster; there is no hand-maintained skill manifest. A tool that needs to
know which skills are generated (to exclude them from a comparison against
hand-authored ones, say) should read this listing rather than duplicate the
names.

**`prompts/rendered-files.txt` — the generated-path inventory.** One
repo-relative POSIX path per line, byte-sorted, LF-terminated, with a trailing
newline and no comments or blank lines. The renderer is its only writer, and
`--check` compares it against a fresh render, so it cannot silently go stale.

Every entry is inside the renderer's ownership domain, which is what makes the
file safe to act on. The source depends on the declared owner:

| Shape                          | Source                                                                   |
| ------------------------------ | ------------------------------------------------------------------------ |
| `<root>/skills/<skill>/<path>` | `prompts/skills/<skill>/<path>`                                          |
| composed review Markdown       | the engine template in `prompts/review/` plus `prompts/partials/review/` |
| `<root>/prompt-stack.json`     | `prompts/profiles/<profile>.yml` and root `PROMPT_STACK_VERSION`         |
| `<root>/<vendored path>`       | the `VENDORED_DOCUMENTS` source for that path                            |

Composed review entries may sit inside skill directories but have no source
under `prompts/skills/`. The source for an output such as
`.codex/skills/critique/SKILL.md` is
`prompts/review/codex/skills/critique/SKILL.md.hbs`. Inventory readers must account
for this ownership before assuming that every skill path has a shared source.

## Adding a variable

Add it to all three profiles and use `<<KEY>>` in the source. A placeholder with
no value in some profile fails the render rather than shipping through; so does
a placeholder that survives substitution for any reason.

## Two rules that are not negotiable

**Zero conditionals.** Shared skills use vocabulary substitution; composed review
documents add static partial includes. A phase only one engine runs, a different
number of steps, or calibrated reviewer instructions belong in that engine's
template. Handlebars branching and dynamic composition are rejected, so the
structure that ships remains visible in the source.

**Prettier never touches the sources.** `prompts/skills/`, `prompts/review/`, and
`prompts/partials/` are in `.prettierignore` and must stay there. Prettier's Markdown parser is not
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

## What is _not_ rendered, and the lint that watches it

The vendored `review-ledger.js` helper is not a rendered prompt. It is the build
output of [`packages/review-ledger`](../packages/review-ledger/README.md), copied
byte-for-byte into each root's `skills/critique/scripts/`, and CI's
`Review-ledger package` job fails when any copy differs from a fresh build. It
cannot be a render output because its source is a build artifact that does not
exist until `pnpm run build` has run, and the renderer must work from a clean
checkout. The bundle is deliberately outside the prompt stack (see
`prompt_stack` in `prompts/profiles/claude.yml`), so a helper-only change does
not advance `PROMPT_STACK_VERSION`.

The protocol document that ships beside it _is_ rendered — see
[vendored documents](#vendored-documents) — and it is a prompt-stack input, so
editing it does advance `PROMPT_STACK_VERSION`.

The review chain — `critique`, `deepcritique`, `refactorpass`, `reviewit`,
`copilot-review`, and their siblings — retains separate engine implementations.
[`docs/decisions/0006`](decisions/0006-review-chains-never-converge.md) is the
standing record: calibrated review instructions stay separate, while shared
policy is composed from partials in the migrated entry points. `copilot-review`
and other unmigrated skills remain directly authored. `agent-loop` is unrendered for a different
reason — [`0007`](decisions/0007-agent-loop-per-harness-launch.md) — three
launch models and three supervision models around one protocol.

"Deliberately different" and "nobody noticed" look identical in a diff, so an
unrendered shared skill is not left to its own devices:

```bash
python3 scripts/lint-prompt-parity.py            # what CI runs
python3 scripts/lint-prompt-parity.py --report   # the residual table
python3 scripts/lint-prompt-parity.py --diff <skill>   # the normalized diff
```

Every skill that lives in more than one prompt root and is not rendered gets
diffed with the harness vocabulary normalized away — read from these same
profiles, so the lint and the renderer cannot disagree about what counts as the
same word in two dialects. Whatever survives that needs a disposition in
[`docs/decisions/parity-allowlist.yml`](decisions/parity-allowlist.yml):
`recorded` against a decision record, or `held` against a tracking issue and a
residual ceiling that may shrink but never grow. Anything else fails.

A skill at **zero** residuals is reported as a promotion candidate rather than
a failure, and an allowlist entry naming one fails as stale — that is how a
held debt retires. Promotion is a separate change, and not automatically a free
one: it turns one hand-maintained path per root into one source plus three
generated outputs, so anything else keyed to those paths — a lint suppression,
a pin, a manifest — has to resolve to the source before the skill moves.

Held today, with the drift written down rather than waved through:
`actions-usage-audit`, `pr-critique`, `publish-npm-package`.

## Shared agent-loop budget regions

The controllers retain their engine-specific review and attestation paths. Their
budget state machine and accounting shell functions are single-sourced in
`scripts/agent-loop-budget.py.inc` and `scripts/agent-loop-budget.sh.inc`.
`python3 scripts/render-prompts.py` replaces the delimited budget regions in all
three controllers/helpers; `--check` rejects drift. Inline generation keeps
Codex's existing base-pinned, self-contained helper boundary intact. Edit these
sources, regenerate, and commit the generated regions together.
