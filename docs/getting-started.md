# Getting started

ActiveLoom distributes engineering skills and supporting tools to existing agent clients. For scope and task selection, start with the [README](../README.md) or [agent entry guide](agent-guide.md).

## Run the CLI from source

As of September 10, 2026, the public npm registry does not serve the `activeloom` package. Use the checked-in CLI. In a Bash or compatible shell:

```bash
git clone https://github.com/loomantix/activeloom.git
cd activeloom
export ACTIVELOOM_CHECKOUT="$PWD"
activeloom() { node "$ACTIVELOOM_CHECKOUT/cli/bin/activeloom.js" "$@"; }
activeloom tiers
```

Keep this shell open for the commands below. The `activeloom` function invokes the local installer; it still downloads content from `sync-v2` by default. After npm publication, `npx activeloom` will be the equivalent package invocation. Source examples do not require publication or a global npm install.

Requirements: Node 18.17+; Python 3.9+ with PyYAML for `init`; network access to fetch upstream content. Installation does not supply an agent client, model access, GitHub authentication, or the additional tools a skill needs to run.

## Choose an adoption tier

Choose by where files should live and how updates should arrive. These tiers are separate from Lean/Deep review scope.

| Tier                         | Command                                       | Scope and requirements                                                            |
| ---------------------------- | --------------------------------------------- | --------------------------------------------------------------------------------- |
| [0 — Try it](#tier-0-try-it) | `activeloom add <skill> --harness <id>`       | Personal skills and support files; no GitHub credential needed for public content |
| [1 — Commit it](#tier-1)     | `activeloom init --harness <id>`              | Repository files and config; review and commit updates yourself                   |
| [2 — Automate it](#tier-2)   | `activeloom init --sync --harness <id>`       | Scheduled update PRs; GitHub Actions permissions and built-in token               |
| [3 — Sign it](#tier-3)       | `activeloom init --sync --app --harness <id>` | App-backed sync; GitHub App setup and optional private-upstream read access       |

Tier 2 is the recommended tier for scheduled repository updates (`init --sync`); `init --sync --app` selects Tier 3. For sync setup, a GitHub App is only ever needed at Tier 3. Personal evaluation and manual adoption do not require one.

Use Tier 0 for personal evaluation, Tier 1 for manual repository adoption, and Tier 2 when scheduled update proposals are useful. Tier 3 adds App identity and signed sync commits. Higher tiers are optional.

`activeloom tiers` prints this table in your terminal.

For Codex or OpenAI API work, also see [OpenAI documentation setup](openai-docs.md)
for the official skill and a documentation MCP connection shared across local
repositories.

---

<a id="tier-0-try-it"></a>

## Tier 0 — Try it

No consumer repository or GitHub credential is needed to copy public skills. Running them still requires the relevant agent client and any tools or authentication named in the skill.

```bash
activeloom add critique
```

That copies `critique` and supporting harness files into the selected personal configuration roots. Use `--harness claude`, `--harness codex`, or `--harness gemini` explicitly; otherwise the installer uses machine detection. Start a new agent session and check which skill it loads before running it. A review chain can require additional skills and peer reviewer clients.

```bash
activeloom add                 # list every skill available
activeloom add critique issues # install several at once
activeloom add critique --harness codex
activeloom add critique --dry-run   # show what would happen, write nothing
```

Re-run with `--force` to replace something already installed.

This tier installs into your home directory rather than a consumer repository. Review its preview because supporting references and agent definitions are installed alongside the selected skills. Existing destinations are preserved unless you request replacement.

**When to move up:** when you want your team to have the same skills, rather than just you.

<a id="tier-1"></a>

## Tier 1 — Commit it

The skills and supporting configuration are checked into the repository so teammates share the same files. Each teammate still supplies the agent client and tools required to execute them.

```bash
cd /path/to/your-repo
activeloom init --harness codex
```

This writes the selected harness trees, shared sync targets, and consumer configuration:

- **The harness roots** — `.claude/`, `.codex/`, `.agents/`, or whichever subset applies. `init` picks them by looking: a harness already checked in wins, otherwise the agent CLIs on your machine, otherwise Claude Code. Override with `--harness claude --harness codex`.
- **`.activeloom-config.yml`** — the repository's own configuration, described below.

`init` shows what it detected and asks before writing, since a machine with three agent CLIs installed is not the same thing as a team that wants three harness trees committed. Pass `--yes` to accept the detection, or `--harness claude` to state it outright — either skips the question, as does any non-interactive run.

Review the generated diff and commit the intended files. Tier 1 does not add a scheduled sync workflow, but shared targets can include other workflow files such as DCO. To pick up upstream changes later, run `init` again; upstream-managed files may be replaced, while consumer-owned config is preserved.

`activeloom detect` prints what `init` would decide, and writes nothing.

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

`activeloom init --dry-run` previews changes without writing consumer files. On a new consumer, the full tree render is skipped until `.activeloom-config.yml` exists; the output identifies that limitation.

**When to move up:** when re-running `init` by hand starts getting forgotten.

<a id="tier-2"></a>

## Tier 2 — Automate it

Repository installation plus a scheduled workflow that proposes changes available at the configured upstream ref. Tier 2 skips the shared DCO workflow target because its built-in token cannot push workflow-file updates.

```bash
activeloom init --sync
```

This additionally writes `.github/workflows/sync-from-upstream.yml`, with `UPSTREAM_REPO` and `PR_BASE_BRANCH` already filled in. Commit it.

**There are no secrets to set.** The workflow authenticates with the `GITHUB_TOKEN` that GitHub Actions mints for every run — nothing to create, store, or rotate. That is what makes this the recommended setup rather than the App-based one.

One repository setting is required, and it is easy to miss:

> **Settings → Actions → General → "Allow GitHub Actions to create and approve pull requests"**

Without it, `GITHUB_TOKEN` may write code but may not open a pull request, and the sync fails with an error that does not name the setting. The workflow detects that specific failure and tells you.

GitHub creates pull-request checks for PRs opened with `GITHUB_TOKEN`, but holds them for manual approval. On each sync PR, select **Approve workflows to run** before reviewing its checks. Push-triggered workflows are not started by a `GITHUB_TOKEN` push. See [GitHub's `GITHUB_TOKEN` documentation](https://docs.github.com/en/actions/concepts/security/github_token#when-github_token-triggers-workflow-runs).

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

Everything in Tier 2, but the sync commits are created through the GitHub Contents API using a GitHub App identity, which makes them **GitHub-signed** (`committer: GitHub`, `verified: true`). Choose this tier when your repository policy requires signed sync commits or a GitHub App identity. It does not establish compliance with a security standard. It is also the only tier that can read a private upstream.

```bash
activeloom init --sync --app
```

This writes the App variant of the workflow. It needs a GitHub App installed on the repository with `Contents: write`, `Pull requests: write`, and `Workflows: write`. The Workflows permission is required because the shared target set ships `.github/workflows/dco.yml`. It also needs two secrets:

```bash
gh secret set SYNC_APP_ID --repo <owner>/<repo>
gh secret set SYNC_APP_PRIVATE_KEY --repo <owner>/<repo> < key.pem
```

One App can serve many repositories; an org-level installation makes that straightforward:

```bash
gh secret set SYNC_APP_ID --org <org> --visibility selected \
  --repos repo-a,repo-b,repo-c
```

**If your upstream is private** — a fork of this repository kept inside your org — you also need a fine-grained PAT or App token with `Contents: Read` on it, stored as `UPSTREAM_READ_TOKEN`.

Upgrading from Tier 2 is a workflow swap plus those secrets and App permissions. After reviewing the replacement, run `activeloom init --sync --app --force` to replace the existing generated workflow; the CLI preserves your consumer config. Remove `.github/workflows/dco.yml` from the config's top-level `skip_targets` after the App is installed so Tier 3 can sync that workflow through its installation token.

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

# Required for syncing DCO in Tier 1 or Tier 3. Tier 2 skips this target
# because its built-in token cannot push workflow changes.
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

`activeloom` ships the installer, not the prompts. It fetches content from a **tarball at the selected ref** of this repository at run time — by default the `sync-v2` tag, which is the same ref the sync workflow tracks.

Both routes default to the same distribution gate. The tag is movable: consumers installed at different times can differ, as can different harness selections, substitutions, and explicit refs. A push to upstream `main` does not propagate through the default route until the tag advances and the consumer installs or merges the update.

```bash
activeloom add critique --ref main   # install from a different ref
```

Use `--ref` deliberately. A branch such as `main` opts into development content; a commit SHA identifies a fixed revision. When `init --sync` generates a workflow, it records the selected remote ref. With `--upstream-dir`, local files are used for installation and the workflow keeps its template ref; local content is not automatically published for future syncs.

## Referencing the synced docs

Each selected harness receives its own review workflow and supporting references. Link the installed documents from your repository agent instructions. For a Claude consumer, for example:

```markdown
## AI review workflow

See [`.claude/REVIEW_WORKFLOW.md`](.claude/REVIEW_WORKFLOW.md) — canonical for the lean/deep chains.
See [`.claude/MODEL_NOTES.md`](.claude/MODEL_NOTES.md) before editing anything under `.claude/skills/` or `.claude/agents/`.
```

Add these **after** the first sync lands, so the links resolve.

## Installing from a clone

`scripts/install-skills.sh` is a Claude-specific contributor helper: it **symlinks** skills out of a clone of this repository, so local edits are live and `git pull` updates every linked skill at once. That is the workflow for contributing to activeloom itself.

`activeloom add` copies, and is the door for using the skills. Use the script if you are changing skills; use the CLI if you are running them.

```bash
git clone https://github.com/loomantix/activeloom.git
cd activeloom
./scripts/install-skills.sh --dry-run
./scripts/install-skills.sh
```

## Troubleshooting

- **`no tag sync-v2 in loomantix/activeloom`** — check repository access and whether the selected upstream has that tag. Choose another verified ref only if you intend to install its content; `main` selects development content.
- **`python3 cannot import PyYAML`** — the sync engine needs it: `python3 -m pip install pyyaml`. Only `init` needs Python; `add` does not.
- **`refusing to sync a tree into itself`** — you ran `init` from inside an activeloom checkout. Run it from the repository that should receive the files.
- **Sync workflow fails with missing placeholder errors** — a required substitution is missing from `.activeloom-config.yml`. The log names which; the `onboard` skill fills them.
- **Sync workflow fails with "could not read Username for github.com"** — the upstream is private, so you need Tier 3 and an `UPSTREAM_READ_TOKEN`.
- **Sync PR is empty** — already in sync. The workflow prints `✅ Already in sync with upstream` and skips PR creation.
- **Sync PR keeps reopening with the same content after merge** — your repository is edit-looping against an upstream-managed file. Fix forward upstream, or add the file to `skip_targets` until it catches up.
