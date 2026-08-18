# Getting started — wire up a consumer repo

This walkthrough assumes you have a consumer repo (any GitHub repo on which you want the skills, agents, and review workflow available), and you want it to consume from `loomantix/codex-platform` (or your own fork).

If you only want the skills installed in your local Codex (not synced into a repo), you can skip everything below and run `scripts/install-skills.sh` from a clone — see [`README.md`](../README.md#install-developer-side).

## Prerequisites

- The consumer repo exists on GitHub.
- A GitHub App is installed on the consumer with `contents: write` and `pull_requests: write`. The App's id and private key will be stored as secrets on the consumer.
  - You can use the same App across multiple consumers; org-installation makes this easy. Alternatively, create a per-consumer App.
  - If you don't want to set up an App, you can fall back to `git commit + git push` with the default `GITHUB_TOKEN` — but commits won't be GitHub-signed, which matters for SOC 2 / ISO 27001 audit posture.
- If your upstream repo is private (e.g. a fork of this repo kept private inside an org), you also need a fine-grained PAT or App token with `Contents: Read` on the upstream — stored on the consumer as `UPSTREAM_READ_TOKEN`.

## 1. Add `.platform-config.yml` to the consumer

This file lives at the consumer's repo root and provides substitution values for templated targets (currently just the Copilot reviewer instructions).

```yaml
# .platform-config.yml
substitutions:
  PROJECT_NAME: My Project
  PROJECT_OVERVIEW: |
    Short description of the project — what it does, who uses it.
  CANONICAL_DOCS: '`docs/architecture.md`, `docs/conventions.md`'
  STACK_TABLE: |
    | Layer    | Tech                                |
    | -------- | ----------------------------------- |
    | Backend  | Node 20 + Fastify                   |
    | Frontend | React 18 + Vite                     |
    | DB       | Postgres 16 + Prisma                |
    | Tests    | Vitest                              |
  CODE_RULES: |
    - Strict TypeScript everywhere. No `any`.
    - Conventional commits enforced by commitlint.
    - Path aliases: `@/*` in both apps.
  DOMAIN_RULES: ''
  REVIEW_FOCUS: |
    1. Correctness — logic errors, edge cases, off-by-one.
    2. Security — secret handling, auth bypass, injection at edges.
    3. Convention adherence.
    4. Testing gaps.
    5. Maintainability.
  WHAT_NOT_TO_SUGGEST_EXTRA: ''

# Optional: opt out of specific upstream files.
# Use either the source or destination path.
skip_targets: []

# Required. The ceiling on what an upstream manifest may write, delete, or
# bootstrap in this repo. Every destination must match at least one pattern.
# The list below matches what the canonical manifest ships today — trim it
# if you don't want a surface. Dropping the workflows line, for example,
# means upstream can never write to your CI, whatever the manifest says.
allowed_destinations:
  - .codex/**
  - .github/copilot-instructions.md
  - .github/workflows/dco.yml
  - agent-loop-instructions.md
```

> **Don't omit `allowed_destinations`.** Leaving it out is not "the default" — it's fail-open: the engine prints a warning into an otherwise-green job and then trusts the upstream manifest to write anywhere in your tree, including `.github/workflows/`. The absent-key case only exists so the gate could ship without breaking consumers mid-flight, and it will flip to fail-closed once the fleet has migrated. See [Bounding what the sync can write](sync.md#bounding-what-the-sync-can-write-allowed_destinations).

Substitution is plain `<<KEY>>` find-and-replace — no template engine. Multi-line values use YAML block scalars (the `|` form). All keys must be `[A-Z][A-Z0-9_]*`.

## 2. Add the sync workflow to the consumer

Copy [`.github/workflows/sync-from-upstream.yml.template`](../.github/workflows/sync-from-upstream.yml.template) to your consumer at `.github/workflows/sync-from-upstream.yml` (drop the `.template` suffix).

Edit:

- `UPSTREAM_REPO: <owner>/<repo>` → e.g. `loomantix/codex-platform`
- `PR_BASE_BRANCH: main` → adjust if your release flow uses `staging` or another branch.
- The `Validate UPSTREAM_READ_TOKEN is configured` step — remove entirely if your upstream is public.
- The `Clone upstream` step — if your upstream is public, the auth-header logic still works (it's a no-op when `UPSTREAM_READ_TOKEN` is empty), but you can simplify if you want.

## 3. (Skip) The sync manifest is upstream-owned

The sync engine reads [`scripts/sync-targets.yml`](../scripts/sync-targets.yml) from the **upstream** checkout (the cloned-into-`/tmp` copy of the upstream repo). The manifest is upstream-owned, not consumer-owned — you don't author one in your consumer.

If you're using `loomantix/codex-platform` directly, the manifest already exists there and ships the full skill set. To opt out of specific files for one consumer, list them in `skip_targets` inside that consumer's `.platform-config.yml` (see step 1).

If you forked `loomantix/codex-platform` and want to customize what gets synced for **all** of your consumers (drop a skill that isn't relevant fleet-wide, add one of your own), edit `scripts/sync-targets.yml` in your fork.

## 4. Set the App-token secrets on the consumer

The workflow expects these secrets. Names are configurable in the workflow file; defaults shown:

- `SYNC_APP_ID` — GitHub App id (numeric).
- `SYNC_APP_PRIVATE_KEY` — PEM-encoded private key for the App.
- `UPSTREAM_READ_TOKEN` — fine-grained PAT or App token with `Contents: Read` on the upstream (only required for private upstream).

Set them as repo secrets:

```bash
gh secret set SYNC_APP_ID --repo <owner>/<consumer> --body "<app-id>"
gh secret set SYNC_APP_PRIVATE_KEY --repo <owner>/<consumer> --body "$(cat path/to/key.pem)"
# Only if upstream is private:
gh secret set UPSTREAM_READ_TOKEN --repo <owner>/<consumer> --body "<token>"
```

> **Important — use `--body "$VALUE"`, not `--body -`.** Passing a secret via stdin (`echo "$TOKEN" | gh secret set --body -`) silently mangles the value. The arg form is the only reliable transport.

### Tag signing keys

The sync workflow verifies the upstream tag's signature before it runs any upstream code. Set the allowed signers as a repo **variable** — these are public keys, not secrets:

```bash
gh variable set SYNC_TAG_ALLOWED_SIGNERS --repo <owner>/<consumer> --body "$(cat allowed_signers)"
```

Where `allowed_signers` holds one principal per line, in git's allowed-signers format:

```
maintainer-a@example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...
```

Ask upstream for its published signing keys. Until the variable is set the workflow warns and syncs anyway, so this doesn't block first-time onboarding — but an unset variable means anyone who can push a tag to the upstream can ship code into this repo. See [Signing the tag](sync.md#signing-the-tag).

For multi-consumer setups, prefer org-level secrets scoped to the consumer repos:

```bash
gh secret set SYNC_APP_ID --org <your-org> --visibility selected --body "<app-id>" \
  --repos consumer-a,consumer-b,consumer-c
```

## 5. Run the workflow once

```bash
gh workflow run "Sync from upstream" --repo <owner>/<consumer>
```

The first run opens a sync PR with a large diff (it's bringing in the whole skill set + workflow doc + Copilot instructions for the first time). Review carefully, merge.

Subsequent daily runs only carry actual upstream changes — usually empty.

## 6. (Optional) Reference `REVIEW_WORKFLOW.md` from the consumer's `AGENTS.md`

The sync brings `.codex/REVIEW_WORKFLOW.md` into the consumer. For Codex sessions to actually follow that workflow, add a one-line reference in the consumer's `AGENTS.md`:

```markdown
## AI review workflow

See [`.codex/REVIEW_WORKFLOW.md`](.codex/REVIEW_WORKFLOW.md) — canonical for the lean/deep chains.
```

Without this reference, Codex only auto-loads `AGENTS.md`; the synced workflow file would be dormant.

## Kill switch

`SKIP_UPSTREAM_SYNC` repo variable disables the sync without editing the workflow:

```bash
gh variable set SKIP_UPSTREAM_SYNC --repo <owner>/<consumer> --body=true
# … later, to re-enable:
gh variable delete SKIP_UPSTREAM_SYNC --repo <owner>/<consumer>
```

Use it if upstream is in a known-bad state.

## Tag-based gating

Consumers track the `sync-v1` tag, not `main`. So a stray push to upstream main doesn't propagate to consumers. Shipping a new sync surface is two deliberate steps in the upstream repo:

```bash
# in the upstream repo, on main
git tag -sf sync-v1 -m "Retag sync-v1 to <reason>" <commit-sha>
git push --force-with-lease origin sync-v1
```

Sign the tag (`-s`, not `-a`). Advancing a tag is a force-push, not a pull request, so it never passes through `CODEOWNERS` review — the signature plus a tag protection ruleset are what make the tag trustworthy. See [The tag is the real trust boundary](sync.md#the-tag-is-the-real-trust-boundary).

The `-v1` suffix is the protocol version, not a content version. Retag `sync-v1` for ordinary content changes. Create `sync-v2` only for a breaking sync-protocol change that requires coordinated consumer workflow updates.

## Troubleshooting

- **First sync PR has weird `<<KEY>>` left intact** — your `.platform-config.yml` is missing a required substitution. The workflow log will list which keys.
- **Sync workflow fails with "could not read Username for github.com"** — the upstream is private and the `UPSTREAM_READ_TOKEN` secret is missing or mis-set. Re-set with `--body "$TOKEN"` (arg form, not stdin).
- **Sync PR is empty** — already in sync. The workflow's `Detect changes` step prints `✅ Already in sync with upstream` and skips PR creation.
- **Sync PR keeps re-opening with the same content after merge** — your consumer is edits-loop-ing against an upstream-managed file. Either fix-forward in upstream, or add the file to `skip_targets` until upstream catches up.
