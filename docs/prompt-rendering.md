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
**committed**, because they are the harness-specific distribution artifacts and
what a reader of a harness root expects to find. A consumer receives only the
paths selected by its own sync configuration; rendering does not imply that
every consumer syncs all three roots.

```bash
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

The sync protocol pin (`sync-v1`) is not this version and cannot become it: that
tag is force-moved whenever content changes, so two consumers "on sync-v1" at
different times are running different prompts.

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
file safe to act on. Two shapes appear in it:

| Shape                          | Source                                                           |
| ------------------------------ | ---------------------------------------------------------------- |
| `<root>/skills/<skill>/<path>` | `prompts/skills/<skill>/<path>`                                  |
| `<root>/prompt-stack.json`     | `prompts/profiles/<profile>.yml` and root `PROMPT_STACK_VERSION` |

The second shape is the one to check a reader against: it is a generated path
with **no** corresponding file under `prompts/skills/`, so a tool that resolves
an inventory entry back to a source by string surgery on the skill segment will
not find one. It is also the only entry that is not inside a skill directory.

## Adding a variable

Add it to all three profiles and use `<<KEY>>` in the source. A placeholder with
no value in some profile fails the render rather than shipping through; so does
a placeholder that survives substitution for any reason.

## Two rules that are not negotiable

**Zero conditionals.** The substitution engine has no branching construct and
none will be added. A skill whose _structure_ must differ between harnesses — a
phase only one engine runs, a different number of steps — is per-harness by
definition. It stays out of `prompts/` and is held to account by the parity
lint instead (below). The moment a renderer grows an `{% if %}`, the single
source stops being readable as the thing that ships, which is the only property
that makes rendering safer than copies.

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

## What is _not_ rendered, and the lint that watches it

The review chain — `critique`, `deepcritique`, `refactorpass`, `reviewit`,
`copilot-review`, and their siblings — is deliberately never single-sourced.
[`docs/decisions/0006`](decisions/0006-review-chains-never-converge.md) is the
standing record: those prompts are calibration for a specific model family, and
two engines running the same text are one reviewer with two billing accounts.
The implementations deliberately diverge, so there is no shared source to
recover even if it were wanted. `agent-loop` is unrendered for a different
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
`actions-usage-audit`, `backlog-refinement`, `pr-critique`,
`publish-npm-package`.
