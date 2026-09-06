# Getting started

There are four ways to use activeloom. They form a ladder: each rung is the one below it plus exactly one thing, and the thing being added is always **credential cost**.

Start at the rung whose price you are willing to pay. Nothing higher is a prerequisite for anything lower, and moving up later never means undoing what you already did.

| Tier                         | Command                            | Needs        | You get                                                  |
| ---------------------------- | ---------------------------------- | ------------ | -------------------------------------------------------- |
| [0 — Try it](#tier-0-try-it) | `npx activeloom add <skill>`       | nothing      | skills in your own agent, on this machine                |
| [1 — Commit it](#tier-1)     | `npx activeloom init`              | nothing      | the same skills checked into a repo, for your whole team |
| [2 — Automate it](#tier-2)   | `npx activeloom init --sync`       | nothing      | ...kept up to date by a daily pull request               |
| [3 — Sign it](#tier-3)       | `npx activeloom init --sync --app` | a GitHub App | ...with GitHub-signed commits, and private upstreams     |

**Tier 2 is the recommended tier and the right answer for most repositories** — it is `npx activeloom init --sync`, not bare `init`, which stops at Tier 1. A GitHub App is only ever needed at Tier 3.

`npx activeloom tiers` prints this table in your terminal.

---

## Tier 0 — Try it

No repository. No account. No key. Nothing committed.

```bash
npx activeloom add critique
```

That installs the `critique` skill into your own agent configuration directory (`~/.claude/skills/`, and the equivalent for any other agent CLI it finds). Start a new agent session and it is there.

```bash
npx activeloom add                 # list every skill available
npx activeloom add critique issues # install several at once
npx activeloom add critique --harness codex
npx activeloom add critique --dry-run   # show what would happen, write nothing
```

Re-run with `--force` to replace something already installed.

This tier writes outside your current directory — into your home directory — and nowhere else. It touches no repository, so there is nothing to review, revert, or explain to a teammate.

**When to move up:** when you want your team to have the same skills, rather than just you.

<a id="tier-1"></a>

## Tier 1 — Commit it

The skills, checked into the repository, so every teammate gets them without installing anything.

```bash
cd your-repo
npx activeloom init
```

This writes two things:

- **The harness roots** — `.claude/`, `.codex/`, `.agents/`, or whichever subset applies. `init` picks them by looking: a harness already checked in wins, otherwise the agent CLIs on your machine, otherwise Claude Code. Override with `--harness claude --harness codex`.
- **`.activeloom-config.yml`** — the repository's own configuration, described below.

`init` shows what it detected and asks before writing, since a machine with three agent CLIs installed is not the same thing as a team that wants three harness trees committed. Pass `--yes` to accept the detection, or `--harness claude` to state it outright — either skips the question, as does any non-interactive run.

Commit both. That is the whole tier: no workflow, no secrets, no automation. To pick up upstream changes later, run `init` again.

`npx activeloom detect` prints what `init` would decide, and writes nothing.

### Filling in the config

`init` writes what it can verify — the lockfile, the ecosystems, the declared test and lint scripts — and leaves a `TODO(activeloom):` marker everywhere judgement is needed:

```yaml
substitutions:
  PROJECT_NAME: 'widget'
  PROJECT_OVERVIEW: |
    TODO(activeloom): one short paragraph — what this project does and who uses it.
```

It refuses to guess these on purpose. They are substituted into `.github/copilot-instructions.md`, which reviews every pull request, and a plausible-sounding invention is worse than a blank: nobody re-reads a field that looks filled in.

To fill them, run the **`onboard`** skill in your agent. It reads the repository, drafts each value with the evidence it came from, and presents them for you to confirm — it does not write until you say so. Then re-run `init` so the values reach the rendered files.

`npx activeloom init --dry-run` reports what would change without writing.

**When to move up:** when re-running `init` by hand starts getting forgotten.

<a id="tier-2"></a>

## Tier 2 — Automate it (the default)

Everything in Tier 1, plus a scheduled workflow that opens a pull request whenever upstream changes.

```bash
npx activeloom init --sync
```

This additionally writes `.github/workflows/sync-from-upstream.yml`, with `UPSTREAM_REPO` and `PR_BASE_BRANCH` already filled in. Commit it.

**There are no secrets to set.** The workflow authenticates with the `GITHUB_TOKEN` that GitHub Actions mints for every run — nothing to create, store, or rotate. That is what makes this the recommended setup rather than the App-based one.

One repository setting is required, and it is easy to miss:

> **Settings → Actions → General → "Allow GitHub Actions to create and approve pull requests"**

Without it, `GITHUB_TOKEN` may write code but may not open a pull request, and the sync fails with an error that does not name the setting. The workflow detects that specific failure and tells you.

### What `PR_BASE_BRANCH` means

The branch sync PRs land on — your integration branch, which is **not** always your default branch. A repository that promotes `staging` → `main` lands sync PRs on `staging`, while the workflow _definition_ is still read from the default branch. `init` fills it from your `origin` HEAD; override with `--base-branch staging` if that is wrong.

### What you are trading away

- **Sync commits are not signed.** They are ordinary commits by `github-actions[bot]`.
- **The upstream must be public.** Reading a private upstream needs a credential, and not having one is what defines this tier.

Both are exactly what Tier 3 buys back.

### Turning it off

```bash
gh variable set SKIP_UPSTREAM_SYNC --repo <owner>/<repo> --body=true
gh variable delete SKIP_UPSTREAM_SYNC --repo <owner>/<repo>   # re-enable
```

**When to move up:** when an audit control requires signed commits, or your upstream is a private fork.

<a id="tier-3"></a>

## Tier 3 — Sign it

Everything in Tier 2, but the sync commits are created through the GitHub Contents API using a GitHub App identity, which makes them **GitHub-signed** (`committer: GitHub`, `verified: true`). This is the tier for repositories under SOC 2, ISO 27001, or a similar regime that requires attested-actor sign-off on every change. It is also the only tier that can read a private upstream.

```bash
npx activeloom init --sync --app
```

This writes the App variant of the workflow. It needs a GitHub App installed on the repository with `contents: write` and `pull_requests: write`, and two secrets:

```bash
gh secret set SYNC_APP_ID --repo <owner>/<repo> --body "<app-id>"
gh secret set SYNC_APP_PRIVATE_KEY --repo <owner>/<repo> --body "$(cat key.pem)"
```

> **Use the `--body "$VALUE"` form.** Passing a secret on stdin (`echo "$TOKEN" | gh secret set --body -`) silently mangles the value.

One App can serve many repositories; an org-level installation makes that straightforward:

```bash
gh secret set SYNC_APP_ID --org <org> --visibility selected \
  --body "<app-id>" --repos repo-a,repo-b,repo-c
```

**If your upstream is private** — a fork of this repository kept inside your org — you also need a fine-grained PAT or App token with `Contents: Read` on it, stored as `UPSTREAM_READ_TOKEN`.

Upgrading from Tier 2 is a workflow swap plus those secrets. Nothing written at Tier 2 has to be undone.

---

## The config file

`.activeloom-config.yml` lives at the consumer's repository root. One file per repository, whatever mix of harnesses you run.

```yaml
# Which harnesses this repo runs. You receive these target sets plus the shared one.
harnesses: [claude, codex]

substitutions:
  PROJECT_NAME: My Project
  PROJECT_OVERVIEW: |
    Short description — what it does, who uses it.
  CANONICAL_DOCS: '`docs/architecture.md`, `docs/conventions.md`'
  STACK_TABLE: |
    | Layer    | Tech              |
    | -------- | ----------------- |
    | Backend  | Node 20 + Fastify |
    | DB       | Postgres 16       |
  CODE_RULES: |
    - Strict TypeScript everywhere. No `any`.
    - Conventional commits enforced by commitlint.
  DOMAIN_RULES: ''
  REVIEW_FOCUS: |
    1. Correctness — logic errors, edge cases, off-by-one.
    2. Security — secret handling, auth bypass, injection at edges.
    3. Convention adherence.
  WHAT_NOT_TO_SUGGEST_EXTRA: ''

# Required before the sync may write a sensitive path. Every consumer receives
# `.github/workflows/dco.yml`, so this entry is what lets your first sync run.
# A refusal names any others it needs, in a block you can paste as-is.
allow_sensitive_writes:
  - .github/workflows/dco.yml

# Optional: opt out of specific upstream files, by source or destination path.
skip_targets: []

# Optional: review telemetry gates, declared once for the repo.
telemetry:
  extract: on
```

Substitution is plain `<<KEY>>` find-and-replace — no template engine. Multi-line values use YAML block scalars (`|`). All keys must match `[A-Z][A-Z0-9_]*`.

`skip_targets`, `allowed_destinations`, `allow_sensitive_writes`, and `substitutions` may also be set per harness under a `harnesses:` mapping — see [`docs/sync.md`](sync.md#how-the-two-levels-compose) for how the two levels compose, which is not the same rule for every key.

**Already have `.platform-config.yml` / `.codex-platform-config.yml` / `.gemini-platform-config.yml`?** They keep working: the engine composes the ones present into this shape, one harness per file. See [Migrating from the pre-sync-v2 config files](sync.md#migrating-from-the-pre-sync-v2-config-files).

## Where the content comes from

`npx activeloom` ships the installer, not the prompts. It fetches content from a **tag-pinned tarball** of this repository at run time — by default the `sync-v2` tag, which is the same ref the sync workflow tracks.

That is deliberate: both doors read one gate, so the CLI and CI can never deliver different prompts. A push to upstream `main` propagates to neither until the tag moves.

```bash
npx activeloom add critique --ref main   # install from a different ref
```

> **If `sync-v2` has not been cut yet**, every command fails with a message naming the tag. Pass `--ref main` until the consumer cutover creates it.

## Referencing the synced docs

The sync brings `.claude/REVIEW_WORKFLOW.md`, `.claude/MODEL_NOTES.md`, and `.claude/references/local-review-ledger.md` into your repository. The ledger is loaded by the review skills themselves, but the other two are only read if your `CLAUDE.md` points at them:

```markdown
## AI review workflow

See [`.claude/REVIEW_WORKFLOW.md`](.claude/REVIEW_WORKFLOW.md) — canonical for the lean/deep chains.
See [`.claude/MODEL_NOTES.md`](.claude/MODEL_NOTES.md) before editing anything under `.claude/skills/` or `.claude/agents/`.
```

Add these **after** the first sync lands, so the links resolve.

## Installing from a clone

`scripts/install-skills.sh` is a different door with a different job: it **symlinks** skills out of a clone of this repository, so local edits are live and `git pull` updates every linked skill at once. That is the workflow for contributing to activeloom itself.

`npx activeloom add` copies, and is the door for using the skills. Use the script if you are changing skills; use the CLI if you are running them.

```bash
git clone https://github.com/loomantix/activeloom.git
cd activeloom
./scripts/install-skills.sh --dry-run
./scripts/install-skills.sh
```

## Troubleshooting

- **`no tag sync-v2 in loomantix/activeloom`** — the tag has not been cut yet, or you are pointed at a fork without it. Pass `--ref main`.
- **`python3 cannot import PyYAML`** — the sync engine needs it: `python3 -m pip install pyyaml`. Only `init` needs Python; `add` does not.
- **`refusing to sync a tree into itself`** — you ran `init` from inside an activeloom checkout. Run it from the repository that should receive the files.
- **First sync PR leaves `<<KEY>>` intact** — a required substitution is missing from `.activeloom-config.yml`. The log names which; the `onboard` skill fills them.
- **Sync workflow fails with "could not read Username for github.com"** — the upstream is private, so you need Tier 3 and an `UPSTREAM_READ_TOKEN`.
- **Sync PR is empty** — already in sync. The workflow prints `✅ Already in sync with upstream` and skips PR creation.
- **Sync PR keeps reopening with the same content after merge** — your repository is edit-looping against an upstream-managed file. Fix forward upstream, or add the file to `skip_targets` until it catches up.
