# Sync from upstream

Canonical files (Claude Code skills, the Copilot instructions template, optional GitHub workflows) live in this repo. Consumer repos pull them via a daily-cron GitHub Action, and developers refresh their local skill set with a one-shot install script. This doc explains both flows and the on-disk contract.

## What flows where

The single-source-of-truth list is [`scripts/sync-targets.yml`](../scripts/sync-targets.yml) — it lives in the **upstream** repo (this one, or a fork). Consumers don't author it; they only opt out of specific entries via `skip_targets` in `.platform-config.yml`. Each entry maps a file in the upstream repo to a destination path in the consumer, optionally with placeholder substitution (`<<KEY>>` form) resolved from the consumer's `.platform-config.yml`.

A target with `delete: true` removes the destination from the consumer instead of writing to it (and prunes any empty parent directories). Use this to retire a previously-synced file across all consumers.

A target with `create_if_missing: true` bootstraps the destination on first sync and leaves it alone thereafter. Use this for files that consumers are expected to customize after creation (starter scaffolding, per-consumer configuration). On first creation, required substitutions are still validated and the sync hard-fails if any are missing — same contract as any other copy target. On subsequent syncs the engine short-circuits before substitution, so substitution values declared by the manifest don't have to remain present in the consumer's `.platform-config.yml` once the file exists. Mutually exclusive with `delete`.

## CI flow (consumer-side workflow)

Each consumer repo drops in `.github/workflows/sync-from-upstream.yml` (copied from [`sync-from-upstream.yml.template`](../.github/workflows/sync-from-upstream.yml.template), with the `UPSTREAM_REPO` and secret names filled in). On its daily cron + `workflow_dispatch`, the workflow:

1. Shallow-clones the upstream repo at the pinned `UPSTREAM_REF` tag (defaults to `sync-v1`).
2. Runs [`scripts/sync-engine.py`](../scripts/sync-engine.py) against the consumer working tree.
3. If the working tree changed, opens a PR titled `Sync from <upstream-repo>`.
4. Closes any prior open sync PR — humans either merged it or rejected it; the workflow doesn't accumulate stale sync PRs. The closed PR's review comments persist on GitHub; only the head branch is deleted.

A reviewer merges the PR; once merged, the next `git pull` on a developer's machine surfaces the changes.

### Tag advancement (the gate that ships)

Consumers track a tag (`sync-v1`), not `main`. So an unintended push to upstream main does NOT propagate. Shipping a new sync surface is one deliberate step: force-retag `sync-v1` to point at the commit you want consumers to receive.

```bash
# in the upstream repo, on main, after merging changes you want to ship
git tag -af sync-v1 -m "Retag sync-v1 to <reason>" <commit-sha>
git push --force-with-lease origin sync-v1
```

The `--force-with-lease` is required and intentional — it asserts the tag's previous SHA so a concurrent retag from another maintainer fails loudly rather than silently clobbering. The annotated message documents the cumulative changes since the previous retag.

#### Why the `-v1` suffix is a protocol version, not a content version

The tag is named `sync-v1` because it pins the **sync protocol** — the manifest schema, the substitution syntax, the on-disk contract between this engine and consumer trees. Bumping to `sync-v2` is reserved for a breaking change in that protocol (e.g., a new required manifest field that older engines can't handle, or a substitution syntax change that older engine code parses incorrectly). When that happens, the bump is a coordinated migration: consumers stay on `sync-v1` until they've also updated their pinned engine tarball / workflow to understand `v2`, then bump `UPSTREAM_REF: sync-v2` in their `.github/workflows/sync-from-upstream.yml`.

For ordinary content advances — adding a new file to the sync surface, retiring a stub, fixing typos in a synced doc — **force-retag the existing `sync-v1`**. Don't create `sync-v2` for content; that's what advancing the tag pointer is for. A short-lived `sync-v2` tag that no consumer migrates to becomes orphaned residue.

In practice: `sync-v1` has been the active tag since the protocol was introduced. There is no plan to bump to `sync-v2` until the engine itself ships a breaking change.

### Kill switch

`SKIP_UPSTREAM_SYNC` repo variable disables the sync without editing the workflow:

```bash
gh variable set SKIP_UPSTREAM_SYNC --repo <consumer-repo> --body=true
# … later, to re-enable:
gh variable delete SKIP_UPSTREAM_SYNC --repo <consumer-repo>
```

Use it if upstream is in a known-bad state, or when temporarily stopping syncs during an emergency consumer-side patch (see below).

## Handling emergencies (consumer-side hotfix)

The sync model assumes consumer files are mirrors of upstream's canonical versions. If a consumer needs to hotfix a synced file (CVE in a workflow, urgent reviewer-instruction tweak, etc.), the next sync would normally REVERT that fix. Two-step escape:

1. **Add the file to `skip_targets` in `.platform-config.yml`** so sync stops touching it:
   ```yaml
   skip_targets:
     - .github/workflows/<file>.yml
   ```
2. **Apply the hotfix** to the consumer's copy of the file in a normal PR.
3. **Fix forward in upstream.** Open a PR against the upstream repo with the real fix. Once it lands and a new `sync-vN` tag ships, remove the file from `skip_targets` to resume sync.

The "fix forward in upstream first" rule is the only sustainable shape — the sync mechanism cannot reconcile parallel divergent histories, and `skip_targets` is the only legitimate way to pause it for a single file.

## Dev-side flow (`install-skills.sh`)

Skills resolve from `~/.claude/skills/` before falling back to per-repo `.claude/skills/`. Symlinking the upstream-checkout's skills into the global directory means **`git pull` in the upstream clone updates every skill instantly** — no per-repo PR-merge round-trip for the developer's own tooling.

```bash
cd <your upstream clone>
git pull
./scripts/install-skills.sh         # safe — only installs missing skills
./scripts/install-skills.sh --force # replaces existing entries (backed up)
./scripts/install-skills.sh --dry-run
```

The script symlinks `<upstream>/.claude/skills/<name>` → `~/.claude/skills/<name>`. Set `CLAUDE_SKILLS_DIR` to override the destination.

The CI flow still keeps the in-repo `.claude/skills/` copy in sync — that copy is what teammates without the global install (and CI contexts) use.

## `.platform-config.yml` schema

Each consumer repo has a `.platform-config.yml` at the root. It supplies the substitution values for templated targets:

```yaml
substitutions:
  PROJECT_NAME: <your project>
  PROJECT_OVERVIEW: |
    Short description of the project — what it does, who uses it.
  STACK_TABLE: |
    | Layer    | Tech                          |
    | -------- | ----------------------------- |
    | Backend  | <runtime + framework>         |
  # ... see scripts/sync-targets.yml for the full key list per templated target.

# Optional: opt out of specific files. Use either the source or destination path.
skip_targets: []

# Required before the sync may write a sensitive path. Absent or empty
# means no sensitive path may be written, and the sync fails closed with
# the exact line to add rather than warning in a green job.
allow_sensitive_writes:
  - .github/workflows/dco.yml
```

### `allow_sensitive_writes`

The engine treats a fixed set of destinations as sensitive: `.github/workflows/**`, `.github/actions/**`, `.github/CODEOWNERS`, `package.json`, `pnpm-lock.yaml`, `prisma/schema.prisma`, and `Dockerfile` / `Dockerfile.*`. It refuses to `delete:` any of them unconditionally, and refuses to **write** any of them — overwrite or first-time create — unless the consumer has named that exact path here.

Writing is gated separately from deleting because it is the higher-impact operation on exactly these paths. A deleted workflow stops running; a rewritten workflow runs, with your secrets and whatever `permissions:` the manifest wrote into it. Rewriting `CODEOWNERS` removes the review gate without deleting anything, and rewriting a lockfile is a supply-chain edit your next CI run installs.

Entries must be literal, canonical, repo-relative paths — no globs. Consent inherited from `.github/workflows/**` is what this gate exists to prevent, so a pattern is rejected rather than expanded. Creating a file is gated alongside overwriting it: a workflow that didn't exist before still runs once a manifest authors it. An entry that isn't a sensitive path is a config error, since it's almost always a typo that would leave the real destination unauthorized.

Substitution is plain `<<KEY>>` find-and-replace — no template engine. Multi-line values use YAML block scalars (the `|` form). Keys must be `[A-Z][A-Z0-9_]*`.

## Behavior contract

- **Idempotent.** Re-running the sync against an already-synced repo writes nothing and exits 0.
- **Hard fail on missing required substitution.** If a target declares a placeholder the consumer hasn't configured, the script exits 1 — better to break the sync PR than to silently leave an unfilled `<<KEY>>` in the destination file.
- **Soft warn on undeclared placeholders in the source.** If the source contains `<<FOO>>` but `sync-targets.yml` doesn't declare `FOO` for that target, the placeholder is left intact and a warning is printed. Catches the case where a template change forgot to update the manifest.
- **File mode preserved.** Targets with `mode: "0755"` get chmod'd after write.
- **Sensitive destinations fail closed.** A write to a sensitive path that isn't listed in `allow_sensitive_writes` aborts the sync — including under `--dry-run`, so a dry run never reports a write the real run would refuse. Nothing is written when the gate trips. Permitted sensitive writes are announced in the job log and counted in the closing summary, so a reviewer can see "this run rewrites a workflow" without reading the diff.
- **`create_if_missing` short-circuits before substitution.** When the destination already exists, the engine skips the source read, substitution, and write entirely. This means a consumer can leave `create_if_missing` substitution values undeclared after first creation without breaking later syncs.

## Adding a new consumer

1. **Verify the upstream-read secret exists** if the upstream repo is private. Set `UPSTREAM_READ_TOKEN` (fine-grained PAT or GitHub App token with `Contents: Read` on the upstream repo) on the consumer repo (or as an org-level secret scoped to the consumer). For public upstream repos, no token is needed.
2. **Verify App-token secrets exist** if you want signed sync commits. The reference template reads `SYNC_APP_ID` + `SYNC_APP_PRIVATE_KEY` from secrets — rename in the workflow file if your conventions differ.
3. Create `.platform-config.yml` at the consumer's root with values for every placeholder used by any templated target.
4. Copy `.github/workflows/sync-from-upstream.yml.template` to `.github/workflows/sync-from-upstream.yml` (drop the `.template` suffix), then fill in `UPSTREAM_REPO` and the secret names.
5. Manually trigger the workflow once (`gh workflow run "Sync from upstream"`) to verify the first PR opens cleanly.
6. Review the first sync PR carefully — it's the largest one the consumer will ever see. Subsequent syncs only carry actual upstream changes.

## Cross-repo secret hygiene

> **Important — use `--body "$VALUE"`, not `--body -`.** Passing a secret via stdin (`echo "$TOKEN" | gh secret set --body -`) silently mangles the value: the secret ends up non-empty (so the workflow's `[ -z "$UPSTREAM_READ_TOKEN" ]` validation passes) but the bytes don't authenticate. Failure mode looks identical to a legitimate auth error (`could not read Username for github.com`). The arg form (`--body "$TOKEN"`) is the only reliable transport.

## Prettier and synced files

Synced files are formatted upstream with the canonical [`.prettierrc`](../.prettierrc) at this repo's root. If a consumer runs Prettier with a different config, its `prettier --write` will reformat synced files and the next sync will revert that formatting — producing recurring local working-tree drift.

Two ways to avoid the drift:

1. **Adopt the canonical config** — copy this repo's `.prettierrc` into your consumer repo (or extend yours from it). Prettier then produces identical output on both sides and there's no drift.
2. **Exclude synced paths from your prettier run** — paste the marker block from [`recommended-prettierignore.txt`](../recommended-prettierignore.txt) into your consumer's `.prettierignore`. Keep the `>>> platform-synced paths <<<` markers intact so the block can be replaced mechanically when the synced surface changes. Do this **even if you adopt the canonical config** — synced files are vendored content, and your prettier run has nothing to add there.

Regenerate `recommended-prettierignore.txt` whenever `scripts/sync-targets.yml` changes — the snippet mirrors its `destination:` paths (excluding `delete: true` entries).

### Rendered templates must stay prettier-clean

`.github/copilot-instructions.md` is rendered per-consumer (template + `.platform-config.yml` substitutions), so its cleanliness has two halves:

- **Template half (enforced here).** Ordinary rendering preserves the template's surrounding bytes and splices in each configured value after stripping its trailing newlines. Structural blank collapsing is explicit, not inferred: a Markdown target may list prose-only keys under `collapse_empty_substitutions`. When every placeholder on such a line is opted in and renders exactly empty, the vacated line goes, plus one adjacent blank when keeping it would leave a run (file edges count as blank). Never opt in a key used inside fenced/indented code or raw HTML literal content — `scripts/lint-collapse-sites.py` fails CI on a key whose Markdown template site is not a whole-line placeholder in prose and rejects non-Markdown destinations, since the engine itself cannot classify every file format safely. The `render-check` CI job renders the full surface against `tests/fixtures/render-check/.platform-config.yml` and runs `prettier --check`, then renders `tests/fixtures/render-fidelity/` and diffs it against a checked-in golden so an unintentional rewrite fails too.
- **Consumer half (your `.platform-config.yml`).** The engine does not parse or reformat the contents of substitution values beyond stripping their trailing newlines. Tables, lists, and paragraphs inside them must already be prettier-clean markdown (e.g. a blank line between a paragraph and the list that follows it, aligned table pipes). This is a hard boundary, not a best effort: the engine deliberately has no Markdown parser, because rewriting rendered output without knowing which bytes came from a value corrupts literal content that Prettier preserves. If a sync PR's diff against your repo is pure whitespace, your values are the first place to look.

## Adding a new file to the sync surface

1. Add an entry to `scripts/sync-targets.yml` with `source`, `destination`, and `substitutions: []` (or the placeholder list). For a Markdown destination, if an empty prose-only placeholder sits between blank separators, also add that key to `collapse_empty_substitutions` — it must appear in **both** lists, or the sync fails closed for every consumer. Do not opt in keys used in literal content; the `sync-targets` CI job checks both rules and rejects collapse opt-ins for non-Markdown destinations.
2. If the file uses placeholders, update each consumer's `.platform-config.yml` to provide the new values **before** the sync runs — otherwise the sync workflow fails closed for every consumer until they catch up.
3. Run the sync manually against one consumer first as a smoke test.
