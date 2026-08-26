# The review learning loop

Every review cycle teaches something. This doc says where each lesson goes so
it is still there for the next one — and, in particular, why writing it in the
obvious place loses it.

## The failure this prevents

Role prompts (`.claude/agents/*`, `.codex/references/roles/*`,
`.agents/references/roles/*`) and the `critique` / `deepcritique` skills are
**upstream-owned**. The sync engine rewrites them from the pinned upstream tag
on every run. A reviewer that learns a lesson mid-cycle and edits the role
prompt in the consumer repo has written into a file the next sync PR reverts —
silently, inside a large routine diff that reviewers skim.

Worked example: a consumer repo added five review rules to the role prompts and
critique skills across all three engine surfaces, in one commit. Two days later
the next round of sync PRs — one per engine — reverted every one of them.
Nothing warned; the rules just stopped being applied, inside diffs whose titles
said "Sync from upstream". The rules themselves were good, and one had already
been upstreamed independently into `gemini-platform`, which is why that engine's
sync PR reverted four of the five rather than all five.

## The three layers

| Layer                                             | Owner        | Holds                                                                  |
| ------------------------------------------------- | ------------ | ---------------------------------------------------------------------- |
| Role prompts + `critique` / `deepcritique` skills | this repo    | Review heuristics true of **any** codebase                             |
| `.review/addendum.local.md`                       | the consumer | That repo's **instances**: its flags, paths, invariants, past defects  |
| `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`           | the consumer | How to build, test, and ship — read by every session, not just reviews |

### The routing test

> **Would this rule still be correct in a repo that has never heard of this
> product?**

- **Yes → upstream.** It is a _class_ of defect. Open a PR against the engine
  platform repo. A generic rule written into a consumer's addendum is not
  wrong, it is just invisible to every other repo that needs it.
- **No → the addendum.** It names this repo's flags, paths, services, or past
  incidents. It is an _instance_, and upstreaming it would push one product's
  vocabulary into 25 unrelated repos.

Most lessons split. "Evaluate a new state branch under every mode flag, not
just the one the author had in mind" is the class; "the modes here are
`clipboardFirst` / `deliveryMode` / the two recovery flags" is the instance.
Upstream the first sentence and write the second in the addendum — the upstream
rule tells the reviewer to go read the addendum for the axes.

## The addendum

`.review/addendum.local.md`, at the consumer repo root. Engine-neutral on
purpose: all three engines read the same file, so a lesson learned during a
Codex round is applied by the Claude round that follows it.

Two properties make it safe to append to:

- **It is in no sync manifest.** No upstream target writes that path.
- **It is in no consumer's `allowed_destinations`.** Even a compromised or
  mistaken upstream manifest is refused at the consumer's allowlist. This is
  deliberate and should stay that way: the file's whole value is that it cannot
  be overwritten from outside the repo. Do not "helpfully" add `.review/**` to
  an allowlist, and do not bootstrap the file with a `create_if_missing:` sync
  target — that trades the guarantee for a convenience.

Every role prompt and the `critique` skill tell the reviewer to read it if
present and to review from the prompt alone if it is absent, so a repo that has
never created one loses nothing.

### Adding it to a new repo

1. Create `.review/addendum.local.md`. Start it with a line saying it is
   repo-owned and never synced, then add sections named for the lenses that
   read them (`code-reviewer`, `silent-failure-hunter`, `pr-test-analyzer`, the
   `critique` orchestrator, plus an "all lenses" section).
2. Leave `.review/**` out of every `*-platform-config.yml`
   `allowed_destinations`. Add a comment there saying the omission is
   deliberate, so a later reader does not read it as an oversight.
3. Optionally add a pointer paragraph in the repo-owned instruction files
   (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`). The synced prompts already point at
   the addendum, so this is a bridge for engines and sessions that are not
   running a review skill — and it is what makes the mechanism work in a
   consumer whose sync tag has not advanced yet.

Nothing else is per-repo. The same three steps work in any consumer of any of
the three platform repos.

## The harvest step

At the end of a review cycle — when the ledger converges, or a round produces a
finding that surprised the author — ask one question:

> **What did this round teach that a cold reviewer could not have known from
> the diff and the generic prompts?**

Then route each answer by the test above. Concretely, the lessons worth
harvesting are:

- A defect an engine **missed** that a later engine caught. The lens that
  should have caught it is under-specified; that is an upstream fix if the
  defect class is generic.
- A **green signal that lied** — a suite that reported success while checking
  nothing, a config that scoped a job out, a mirror spec that stayed stale.
  Almost always repo-specific, and almost always invisible to a cold reader.
- A **claim accepted without evidence** that turned out false, especially about
  un-diffed code ("the existing pipe already handles this").
- A **round that fixed the previous round's fix** in the same few lines. That
  is a seam problem, not a defect; record the seam.
- A **false positive** an engine keeps re-raising. Recording the settled answer
  in the addendum stops the whole fleet's next round from relitigating it.

Do not harvest routine findings. The addendum is a ledger of what the prompts
cannot derive, not a log of every issue found.

## Hygiene

- **One claim plus its evidence per entry.** Cite the PR or issue that produced
  it. An entry a reader cannot check is an entry they cannot prune.
- **Prune on absorption.** When an upstream prompt grows a rule that covers an
  addendum entry, delete the entry. Otherwise the addendum grows into a second
  copy of the role prompts and stops being read.
- **No secrets and no sensitive data.** The addendum is a normal committed file
  with the same content rules as any other repo Markdown — in a
  PHI/PII-handling repo, that means redacted identifiers, not raw ones.
- **Upstreaming ships on the tag, not the merge.** A merged platform PR reaches
  consumers only when the `sync-v1` tag advances. Until then the consumer's
  addendum is the only place the rule is live, which is a reason to write the
  instance there even when the class is already upstream in flight.
