# Getting started — wire up a consumer repo

This walkthrough assumes you have a consumer repo (any GitHub repo on which you want the skills, agents, and review workflow available), and you want it to consume from `loomantix/activeloom` (or your own fork).

One workflow and one config file per consumer, whatever mix of harnesses you run. Which harnesses you receive is a list in the config.

If you only want the skills installed in your local Claude Code (not synced into a repo), you can skip everything below and run `scripts/install-skills.sh` from a clone — see [`README.md`](../README.md#install-developer-side).

## Prerequisites

- The consumer repo exists on GitHub.
- A GitHub App is installed on the consumer with `contents: write` and `pull_requests: write`. The App's id and private key will be stored as secrets on the consumer.
  - You can use the same App across multiple consumers; org-installation makes this easy. Alternatively, create a per-consumer App.
  - If you don't want to set up an App, you can fall back to `git commit + git push` with the default `GITHUB_TOKEN` — but commits won't be GitHub-signed, which matters for SOC 2 / ISO 27001 audit posture.
- If your upstream repo is private (e.g. a fork of this repo kept private inside an org), you also need a fine-grained PAT or App token with `Contents: Read` on the upstream — stored on the consumer as `UPSTREAM_READ_TOKEN`.

## 1. Add `.activeloom-config.yml` to the consumer

This file lives at the consumer's repo root. It declares which harnesses the repo runs and provides substitution values for templated targets (currently just the Copilot reviewer instructions).

```yaml
# .activeloom-config.yml

# Which harnesses this repo runs. Names come from the upstream manifest's
# `harnesses:` block; you receive these target sets plus the shared one.
harnesses: [claude, codex]

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

# Optional: review telemetry gates, declared once for the repo. Each is
# `on` or `off`; omit either to leave it to the ambient environment.
telemetry:
  extract: on
```

`skip_targets`, `allowed_destinations`, `allow_sensitive_writes`, and `substitutions` may also be set per harness under a `harnesses:` mapping — see [`docs/sync.md`](sync.md#how-the-two-levels-compose) for how the two levels compose, which is not the same rule for every key.

**Already have `.platform-config.yml` / `.codex-platform-config.yml` / `.gemini-platform-config.yml`?** They keep working: the engine composes the ones present into this shape, one harness per file, and a file you never had means a harness you never ran. See [Migrating from the pre-sync-v2 config files](sync.md#migrating-from-the-pre-sync-v2-config-files).

Substitution is plain `<<KEY>>` find-and-replace — no template engine. Multi-line values use YAML block scalars (the `|` form). All keys must be `[A-Z][A-Z0-9_]*`.

## 2. Add the sync workflow to the consumer

Copy [`.github/workflows/sync-from-upstream.yml.template`](../.github/workflows/sync-from-upstream.yml.template) to your consumer at `.github/workflows/sync-from-upstream.yml` (drop the `.template` suffix).

Edit:

- `UPSTREAM_REPO: <owner>/<repo>` → e.g. `loomantix/activeloom`
- `PR_BASE_BRANCH: ''` → **required**, the branch sync PRs land on. It is not always your default branch: a repo that promotes staging → main lands sync PRs on `staging` while the workflow _definition_ is still read from the default branch, so a change to this file only takes effect after the next promotion.
- The `Validate UPSTREAM_READ_TOKEN is configured` step — remove entirely if your upstream is public.
- The `Clone upstream` step — if your upstream is public, the auth-header logic still works (it's a no-op when `UPSTREAM_READ_TOKEN` is empty), but you can simplify if you want.

## 3. (Skip) The sync manifest is upstream-owned

The sync engine reads [`scripts/sync-targets.yml`](../scripts/sync-targets.yml) from the **upstream** checkout (the cloned-into-`/tmp` copy of the upstream repo). The manifest is upstream-owned, not consumer-owned — you don't author one in your consumer.

If you're using `loomantix/activeloom` directly, the manifest already exists there and ships the full skill set. To opt out of specific files for one consumer, list them in `skip_targets` inside that consumer's `.activeloom-config.yml` (see step 1).

If you forked `loomantix/activeloom` and want to customize what gets synced for **all** of your consumers (drop a skill that isn't relevant fleet-wide, add one of your own), edit `scripts/sync-targets.yml` in your fork.

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

## 6. (Optional) Reference the synced `.claude/` docs from the consumer's `CLAUDE.md`

The sync brings `.claude/REVIEW_WORKFLOW.md`, `.claude/MODEL_NOTES.md`, and `.claude/references/local-review-ledger.md` into the consumer. The ledger is loaded by the review skills themselves, so it needs no `CLAUDE.md` entry — but the other two do. For Claude sessions to actually follow them, add references in the consumer's `CLAUDE.md`:

```markdown
## AI review workflow

See [`.claude/REVIEW_WORKFLOW.md`](.claude/REVIEW_WORKFLOW.md) — canonical for the lean/deep chains.
See [`.claude/MODEL_NOTES.md`](.claude/MODEL_NOTES.md) before editing anything under `.claude/skills/` or `.claude/agents/`.
```

Without these references, Claude Code only auto-loads `CLAUDE.md`; the synced docs would be dormant. Add them **after** the first sync PR merges, so the links resolve.

## Kill switch

`SKIP_UPSTREAM_SYNC` repo variable disables the sync without editing the workflow:

```bash
gh variable set SKIP_UPSTREAM_SYNC --repo <owner>/<consumer> --body=true
# … later, to re-enable:
gh variable delete SKIP_UPSTREAM_SYNC --repo <owner>/<consumer>
```

Use it if upstream is in a known-bad state.

## Tag-based gating

Migrated consumers track `sync-v2`, not `main`, so a stray push to upstream main does not propagate. The initial tag creation belongs to the coordinated consumer cutover, after the protocol implementation is merged and validated:

```bash
# in the upstream repo, on main
git tag sync-v2 -a -m "..."
git push origin sync-v2
```

Update each migrated consumer's workflow to `UPSTREAM_REF: sync-v2`. Keep `sync-v1` frozen at its last pre-restructure commit for consumers that have not migrated; never retag it to deliver the new protocol. Subsequent v2 content updates advance `sync-v2` as described in [Tag advancement](sync.md#tag-advancement-the-gate-that-ships).

## Troubleshooting

- **First sync PR has weird `<<KEY>>` left intact** — your `.activeloom-config.yml` is missing a required substitution. The workflow log will list which keys.
- **Sync workflow fails with "could not read Username for github.com"** — the upstream is private and the `UPSTREAM_READ_TOKEN` secret is missing or mis-set. Re-set with `--body "$TOKEN"` (arg form, not stdin).
- **Sync PR is empty** — already in sync. The workflow's `Detect changes` step prints `✅ Already in sync with upstream` and skips PR creation.
- **Sync PR keeps re-opening with the same content after merge** — your consumer is edits-loop-ing against an upstream-managed file. Either fix-forward in upstream, or add the file to `skip_targets` until upstream catches up.
