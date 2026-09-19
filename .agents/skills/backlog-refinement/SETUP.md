# Setting up backlog refinement in a repository

`backlog-refinement setup` creates the repository's two local files and the labels the rubric uses, or migrates a repository that still keeps its rubric inside a harness's skill directory. You are drafting for the user's confirmation: every value here steers which issues an unattended loop will build, so a plausible guess is worse than an honest blank.

The files live at the repository root, outside every harness directory, so Claude, Codex, and Gemini/Agy all read the same copy:

- `.backlog/refinement.local.md` — from [`templates/refinement.local.md`](./templates/refinement.local.md)
- `.backlog/learnings.local.md` — from [`templates/learnings.local.md`](./templates/learnings.local.md)

## 1. Survey what exists

Report all of this to the user before asking anything:

- Whether `.backlog/refinement.local.md` and `.backlog/learnings.local.md` exist, and how many `TODO(backlog):` markers each still carries.
- Every legacy `RUBRIC.md` and `LEARNINGS.md` under `.claude/skills/backlog-refinement/`, `.codex/skills/backlog-refinement/`, and `.agents/skills/backlog-refinement/`. When there is more than one of a kind, diff them — copies seeded once per harness drift apart.
- `gh label list --limit 500 --json name,description` — which of the labels in the core rubric's label model and §3 already exist, and every existing label that looks like a priority scheme (a `priority` prefix, `P0`–`P3`, `urgent`, `critical`).
- `agent-loop-instructions.md` at the repository root, if present, and whether it names a legacy rubric path.
- The default branch, and whether the repo ships through a separate integration branch (a `staging` or `develop` branch that PRs target).

If both local files exist with no `TODO(backlog):` markers, no legacy file remains, and every label exists, say the repository is already set up and stop.

## 2. Migrate a legacy rubric

Skip to step 3 when there is no legacy file.

1. If the legacy copies disagree, show the user the diff and ask which is authoritative, or whether to merge them. Do not pick silently — the newer-looking copy is not always the one the repo meant.
2. Sort the authoritative rubric's content into three piles and show the user the result before writing:
   - **Already in `core-rubric.md`** — drop it. Say what you are dropping.
   - **Repository-specific** (names this repo's paths, labels, branches, or incidents) — carry it into `.backlog/refinement.local.md` under the matching section.
   - **Generic but not yet in the core** — carry it into the local file _and_ list it under _Upstream candidates_, so it can be proposed to the core rubric.
3. Keep the legacy rubric's version number as the local rubric's starting version, and carry its `auto-managed-labels` marker over verbatim.
4. Move the authoritative `LEARNINGS.md` to `.backlog/learnings.local.md` unchanged. When the harness copies differ, append the entries unique to the others, newest first. When `.backlog/learnings.local.md` already exists — an earlier `setup` ran and the legacy copies were kept — merge in only the entries it lacks; never overwrite it.
5. If `agent-loop-instructions.md` names a legacy rubric path, point it at `.agents/skills/backlog-refinement/core-rubric.md` and `.backlog/refinement.local.md` instead.
6. With the user's agreement, delete every legacy `RUBRIC.md` and `LEARNINGS.md`. The sync no longer writes them, so nothing will recreate them.

Then continue with step 3 for anything the legacy rubric did not answer.

## 3. Settle the values

Ask the open questions in one round, numbered, each with your recommended answer and the evidence behind it. Ask only what step 1 and step 2 left open:

1. **Integration branch** — the branch the loop's PRs target, and so the branch verify-against-HEAD fetches.
2. **Sensitive paths** — the parts of this repository where a non-trivial change needs a human reviewer even with green CI (regulated data, money movement, authentication, production access, audit trails), and the `agent-bail:` label to use for them. Draft candidates from the code and from any existing review addendum; the user decides.
3. **Priority labels** — the four label names, highest first. When the repo already has a priority scheme, recommend reusing it rather than creating a parallel one; when it has two, recommend one and point out that the other should be retired. Ask whether the core tier definitions fit or the repo wants its own wording.
4. **Auto-managed labels** — labels on issues that a scheduled workflow opens and closes. Look for them in `.github/workflows/` before asking.
5. **Rewrite mode** — `edit` (refinement rewrites the issue body) or `suggest` (it posts the rewrite as a comment for a human to apply).

## 4. Write the files

Create `.backlog/refinement.local.md` from the template — or, when it already exists, edit it in place — fill each section from the answers, and remove each `TODO(backlog):` marker you filled. Leave a marker in place for anything the user chose to defer, so the next `setup` run finds it. Create `.backlog/learnings.local.md` from its template unless step 2 already moved one into place. Show the user the finished local file.

## 5. Create the labels

List the labels that do not exist yet — `dev: agent`, `agent: refined`, `status: blocked`, each core and local `agent-bail:` category, `needs: grill`, `needs: product-grill`, and the four priority labels — and create them once the user agrees:

```bash
gh label create "<name>" --color <hex> --description "<one line from the core rubric>"
```

Never rename or delete an existing label as part of setup; migrating issues off a retired scheme is a separate, confirmed step.

## 6. Hand off

Tell the user:

- Commit `.backlog/` in the repository.
- Keep `.backlog/**` out of every `allowed_destinations` list in `.activeloom-config.yml`. The sync never writes there, and leaving it out means a mistaken upstream manifest is refused rather than overwriting the repo's rubric.
- Next steps: `backlog-refinement queue`, then `refine --backfill` if the repository has issues assessed before this layer existed.
