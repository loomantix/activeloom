# Sync from upstream

Canonical files (Claude Code skills, the Copilot instructions template, optional GitHub workflows) live in this repo. Consumer repos pull them via a daily-cron GitHub Action, and developers refresh their local skill set with a one-shot install script. This doc explains both flows and the on-disk contract.

## What flows where

The single-source-of-truth list is [`scripts/sync-targets.yml`](../scripts/sync-targets.yml) — it lives in the **upstream** repo (this one, or a fork). Consumers don't author it; they only opt out of specific entries via `skip_targets` in `.activeloom-config.yml`. Each entry maps a file in the upstream repo to a destination path in the consumer, optionally with placeholder substitution (`<<KEY>>` form) resolved from the consumer's `.activeloom-config.yml`.

The manifest emits **one target set per harness plus one harness-independent `shared:` set**. A consumer receives the shared set plus the sets of every harness it names in its config's `harnesses:` list — so a repo that only runs Codex never sees a `.claude/**` write, and one that runs all three receives exactly what three separate upstreams used to deliver. Each harness declares a `root` (the prompt directory it owns), a `legacy_config` (the pre-sync-v2 config filename the compatibility shim maps back to it), and its `targets`.

Harnesses are processed in manifest declaration order. That matters in exactly one place: a `create_if_missing` destination shipped by more than one harness is bootstrapped by the first declared harness and preserved by every later one.

A target with `delete: true` removes the destination from the consumer instead of writing to it (and prunes any empty parent directories). Use this to retire a previously-synced file across all consumers.

A target with `create_if_missing: true` bootstraps the destination on first sync and leaves it alone thereafter. Use this for files that consumers are expected to customize after creation (starter scaffolding, per-consumer configuration). On first creation, required substitutions are still validated and the sync hard-fails if any are missing — same contract as any other copy target. On subsequent syncs the engine short-circuits before substitution, so substitution values declared by the manifest don't have to remain present in the consumer's `.activeloom-config.yml` once the file exists. Mutually exclusive with `delete`.

## CI flow (consumer-side workflow)

Each consumer repo drops in **one** `.github/workflows/sync-from-upstream.yml` (copied from [`sync-from-upstream.yml.template`](../.github/workflows/sync-from-upstream.yml.template), with `UPSTREAM_REPO`, `PR_BASE_BRANCH`, and the secret names filled in), however many harnesses it runs. Before sync-v2 the file was copied once per upstream and each copy passed its own `--config`; now the harness list lives in the consumer config instead. On its daily cron + `workflow_dispatch`, the workflow:

1. Shallow-clones the upstream repo at the pinned `UPSTREAM_REF` tag (defaults to `sync-v2`).
2. Runs [`scripts/sync-engine.py`](../scripts/sync-engine.py) against the consumer working tree.
3. If the working tree changed, opens a PR titled `Sync from <upstream-repo>`.
4. Closes any prior open sync PR — humans either merged it or rejected it; the workflow doesn't accumulate stale sync PRs. The closed PR's review comments persist on GitHub; only the head branch is deleted.

A reviewer merges the PR; once merged, the next `git pull` on a developer's machine surfaces the changes.

### Tag advancement (the gate that ships)

Consumers track a tag (`sync-v2`), not `main`. So an unintended push to upstream main does NOT propagate. Shipping a new sync surface is one deliberate step: force-retag `sync-v1` to point at the commit you want consumers to receive.

```bash
# in the upstream repo, on main, after merging changes you want to ship
git tag -af sync-v2 -m "Retag sync-v2 to <reason>" <commit-sha>
git push --force-with-lease origin sync-v2
```

The `--force-with-lease` is required and intentional — it asserts the tag's previous SHA so a concurrent retag from another maintainer fails loudly rather than silently clobbering. The annotated message documents the cumulative changes since the previous retag.

#### Why the `-v1` suffix is a protocol version, not a content version

The tag is named `sync-v1` because it pins the **sync protocol** — the manifest schema, the substitution syntax, the on-disk contract between this engine and consumer trees. Bumping to `sync-v2` is reserved for a breaking change in that protocol (e.g., a new required manifest field that older engines can't handle, or a substitution syntax change that older engine code parses incorrectly). When that happens, the bump is a coordinated migration: consumers stay on `sync-v1` until they've also updated their pinned engine tarball / workflow to understand `v2`, then bump `UPSTREAM_REF: sync-v2` in their `.github/workflows/sync-from-upstream.yml`.

For ordinary content advances — adding a new file to the sync surface, retiring a stub, fixing typos in a synced doc — **force-retag the existing `sync-v1`**. Don't create `sync-v2` for content; that's what advancing the tag pointer is for. A short-lived `sync-v2` tag that no consumer migrates to becomes orphaned residue.

In practice: `sync-v1` was the active tag from the protocol's introduction until the manifest grew its `harnesses:` layer and the three per-harness consumer configs became one. That change is engine-breaking in both directions — a sync-v1 engine reads the new manifest as zero targets, and this engine rejects a flat `targets:` list by name — which is what the protocol pin is for. `sync-v1` stays frozen at the last pre-restructure commit for consumers that have not cut over; `sync-v2` is the tag a cut-over consumer pins.

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

1. **Add the file to `skip_targets` in `.activeloom-config.yml`** so sync stops touching it:
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

## `.activeloom-config.yml` schema

Each consumer repo has one `.activeloom-config.yml` at the root. It declares which harnesses the repo runs, supplies substitution values for templated targets, and carries the gates that bound what the sync may write:

```yaml
# Which harnesses this repository runs. List form when you need no
# per-harness overrides…
harnesses: [claude, codex]

# …or mapping form when you do. An empty entry means "this harness, under
# the top-level gates".
harnesses:
  claude:
    allowed_destinations:
      - .claude/**
      - agent-loop-instructions.md
  codex: {}

# Review telemetry, declared once for the repository. Each gate is `on` or
# `off`; omit either to leave it to the ambient environment. These render
# into the synced `.claude/settings.json` env block — see below.
telemetry:
  emit: off
  extract: on

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
# the exact line to add rather than warning in a green job. The canonical
# manifest writes no such path today, so most consumers need no entry at
# all; the refusal names any that appear, in a block you can paste as-is.
allow_sensitive_writes: []
```

### How the two levels compose

`skip_targets`, `allowed_destinations`, `allow_sensitive_writes`, and `substitutions` may appear at the top level, inside a harness entry, or both. They do not compose the same way, and the difference is deliberate:

| Key                      | Top level + harness  | Why                                                                                                                                                                                               |
| ------------------------ | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `substitutions`          | merged, harness wins | A harness override is the narrower statement.                                                                                                                                                     |
| `skip_targets`           | union                | Both are opt-outs you wrote down; taking both is the conservative reading.                                                                                                                        |
| `allow_sensitive_writes` | union                | Same.                                                                                                                                                                                             |
| `allowed_destinations`   | **harness replaces** | This is the gate that bounds the write surface. Unioning it would hand every harness every other harness's surface — precisely the separation the three per-harness config files used to provide. |

The manifest's `shared:` set is governed by the top-level keys alone. A harness that declares no `allowed_destinations` falls back to the top-level list; a scope governed by neither keeps the migration-era fail-open, and says so as a workflow warning.

### Review telemetry

`LOOM_REVIEW_TELEMETRY` (emitting a record to a pull request) and `LOOM_REVIEW_TELEMETRY_EXTRACT` (measuring the pass at all) are read by the usage helper from the environment. `telemetry:` is how a repository declares them once, in the same file as everything else the sync knows about it: the engine renders the gates you declared into the `env` block of the synced `.claude/settings.json`.

Only gates you actually declare are rendered. Settings-declared environment beats the ambient shell, so an env block that named every gate would have to invent a value for the ones you left alone — and a defaulted `off` would silently override a developer who had exported `on`. Declaring neither renders `"env": {}`, which sets nothing.

The value is computed by the engine and exposed as the reserved substitution key `REVIEW_TELEMETRY_ENV`. Declaring that key under `substitutions:` is a config error rather than an override: two sources for one derived value is a bug either way.

### Migrating from the pre-sync-v2 config files

Before sync-v2 a consumer carried one config file per upstream: `.platform-config.yml`, `.codex-platform-config.yml`, `.gemini-platform-config.yml`. Those are not three names for one file — each **is** the config for its own harness, which is the fragmentation this schema removes. So the engine's compatibility shim **composes** them rather than choosing between them, and the config rename does not have to happen in the same change as the workflow cutover.

When there is no `.activeloom-config.yml`, the engine reads every legacy file that is present and builds the config for you:

- Each file becomes the entry for the harness that claims its filename in the manifest (`legacy_config`).
- **A missing legacy file means that harness is absent.** A repository that never carried `.gemini-platform-config.yml` never ran that harness, and must not acquire it by upgrading its engine.
- Per-harness keys are carried over verbatim.
- Top-level `substitutions` are merged; two files disagreeing on one key is a real collision and fails closed.
- Top-level `skip_targets` is the **intersection**. A shared target skipped in two files and synced by the third was being routed to a single owner, not switched off; a union would silently retire it.
- Top-level `allowed_destinations` and `allow_sensitive_writes` are unions, so whichever grant covered the shared targets before still covers them.

The composed config is reported in the job log. `--config <legacy file>` still works and reads that one file as its harness alone, which is the transitional invocation for a consumer still running one workflow per upstream.

To retire the shim, write one `.activeloom-config.yml` and delete the legacy files. An `.activeloom-config.yml` present on disk wins outright — surviving legacy files are then ignored, not merged in.

### `allow_sensitive_writes`

The engine treats a fixed set of destinations as sensitive: `.github/workflows/**`, `.github/actions/**`, `**/CODEOWNERS`, `**/package.json`, `**/pnpm-lock.yaml`, `**/prisma/schema.prisma`, and `**/Dockerfile` / `**/Dockerfile.*`. It refuses to `delete:` any of them unconditionally, and refuses to **write** any of them — overwrite or first-time create — unless the consumer has named that exact path here.

One carve-out: a path inside an engine's own prompt surface (`.claude/**`, `.codex/**`, `.agents/**`) is not sensitive to either guard. Both guards exist to stop a manifest reaching _outside_ its surface into the files that configure your project — what CI runs, who reviews it, what the build installs. The prompt tree is the manifest's own payload, in a directory you already opened to it through `allowed_destinations`, and the engine writes arbitrary executable content there — skills, hooks, the vendored review-ledger bundle — with no gate at all. Gating `.claude/skills/critique/scripts/package.json`, two lines of `{"type": "module"}` scoping the directory that holds that bundle, while writing the bundle itself ungated on the same run is not a smaller grant, only a more confusing one. None of the guarded shapes carry authority there either: GitHub reads workflows only from `.github/workflows/`, resolves `CODEOWNERS` only from the root, `.github/`, and `docs/`, and a package manager installs a nested manifest only when a workspace declares it. The carve-out is also what makes a synced sensitive path retirable — no consent key covers deletes, so without it a file the manifest ships could never be withdrawn by tombstone.

The `**/` entries match at any depth, root included, because a workspace-shaped repo keeps these files at `apps/web/package.json` or `services/api/Dockerfile` rather than at the root. `CODEOWNERS` is matched at any depth because GitHub resolves it from the repository root, `.github/`, and `docs/` — gating only one of the three would leave the review gate rewritable. The two `.github/` entries stay depth-pinned, since those directories are the only place GitHub reads workflows and composite actions from.

Writing is gated separately from deleting because it is the higher-impact operation on exactly these paths. A deleted workflow stops running; a rewritten workflow runs, with your secrets and whatever `permissions:` the manifest wrote into it. Rewriting `CODEOWNERS` removes the review gate without deleting anything, and rewriting a lockfile is a supply-chain edit your next CI run installs.

Entries must be literal, canonical, repo-relative paths — no globs. Consent inherited from `.github/workflows/**` is what this gate exists to prevent, so a pattern is rejected rather than expanded. Creating a file is gated alongside overwriting it: a workflow that didn't exist before still runs once a manifest authors it. An entry that isn't a sensitive path is a config error, since it's almost always a typo that would leave the real destination unauthorized.

Consent is required for any target the sync would write, whether or not this particular run changes the bytes — a sync that ran green for months and then failed the day upstream edited the file would surface the missing entry at the worst possible time. Two cases need no entry, because no write can happen: a target you opted out of with `skip_targets`, and a `create_if_missing` target whose destination already exists as a file (the engine has permanently committed to leaving that file alone). A destination outside `allowed_destinations` reports that error instead, since adding sensitive consent for it would not make it writable.

Your consumer config is refused as a destination, and no entry authorizes it. It records both `allow_sensitive_writes` and `allowed_destinations`, so a manifest able to rewrite it could grant itself consent on one run and spend that consent on the next — with the job log reporting an opt-in you never made. The refusal compares each destination against the resolved config path, so an explicit `--config` elsewhere is covered, while a config file vendored in your tree as an example or a fixture stays an ordinary destination. After a legacy compose (below) every file the config was read from is protected, not just one.

**All of these checks match the destination path as written, not the file it resolves to.** The write itself follows symlinks, so a symlink in your working tree decouples the path the gate judges from the file that ends up rewritten — an ordinary destination symlinked to a workflow is written without consent, and a symlinked consumer config is not caught by the refusal above. The engine assumes an upstream-controlled manifest and a consumer tree free of malicious symlinks (see `resolve_under` in `scripts/sync-engine.py`); anyone able to commit a symlink to your repository can commit the target file directly regardless. Closing the gap is tracked internally.

When a refusal lists paths you have not granted, it prints the complete `allow_sensitive_writes:` block you should end up with — the newly denied destinations **and** the grants already in your config — to _replace_ that key rather than to append after it. Appending a second `allow_sensitive_writes:` key would silently discard the first, since YAML keeps only the last occurrence, and the following run would then refuse a path your config visibly names.

Substitution is plain `<<KEY>>` find-and-replace — no template engine. Multi-line values use YAML block scalars (the `|` form). Keys must be `[A-Z][A-Z0-9_]*`.

## Behavior contract

- **Idempotent.** Re-running the sync against an already-synced repo writes nothing and exits 0.
- **Hard fail on missing required substitution.** If a target declares a placeholder the consumer hasn't configured, the script exits 1 — better to break the sync PR than to silently leave an unfilled `<<KEY>>` in the destination file.
- **Soft warn on undeclared placeholders in the source.** If the source contains `<<FOO>>` but `sync-targets.yml` doesn't declare `FOO` for that target, the placeholder is left intact and a warning is printed. Catches the case where a template change forgot to update the manifest.
- **File mode preserved.** Targets with `mode: "0755"` get chmod'd after write.
- **Sensitive destinations fail closed.** Consent is checked for every target before the sync writes anything, so a missing `allow_sensitive_writes` entry aborts the run with the tree untouched — and the error lists every destination you need to add, not just the first. The one exception is the symlink caveat above: the pre-pass reads the tree as it stands at the start of the run, so an earlier `delete:` target that removes a symlinked ancestor can land before the refusal. The check also applies under `--dry-run`, so a dry run never reports a write the real run would refuse. A permitted sensitive write is announced in the job log and counted in the closing summary at the point it actually happens, so a reviewer can see "this run rewrites a workflow" without reading the diff — and a steady-state sync that rewrites nothing says nothing.
- **`create_if_missing` short-circuits before substitution.** When the destination already exists, the engine skips the source read, substitution, and write entirely. This means a consumer can leave `create_if_missing` substitution values undeclared after first creation without breaking later syncs.

## Adding a new consumer

1. **Verify the upstream-read secret exists** if the upstream repo is private. Set `UPSTREAM_READ_TOKEN` (fine-grained PAT or GitHub App token with `Contents: Read` on the upstream repo) on the consumer repo (or as an org-level secret scoped to the consumer). For public upstream repos, no token is needed.
2. **Verify App-token secrets exist** if you want signed sync commits. The reference template reads `SYNC_APP_ID` + `SYNC_APP_PRIVATE_KEY` from secrets — rename in the workflow file if your conventions differ.
3. Create `.activeloom-config.yml` at the consumer's root: a `harnesses:` list, and values for every placeholder used by any templated target.
4. Copy `.github/workflows/sync-from-upstream.yml.template` to `.github/workflows/sync-from-upstream.yml` (drop the `.template` suffix), then fill in `UPSTREAM_REPO`, `PR_BASE_BRANCH`, and the secret names. `PR_BASE_BRANCH` has no default: it is the branch sync PRs land on, which is not always the default branch — a repo that promotes staging → main lands them on `staging` while this workflow definition is still read from the default branch.
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

`.github/copilot-instructions.md` is rendered per-consumer (template + `.activeloom-config.yml` substitutions), so its cleanliness has two halves:

- **Template half (enforced here).** Ordinary rendering preserves the template's surrounding bytes and splices in each configured value after stripping its trailing newlines. Structural blank collapsing is explicit, not inferred: a Markdown target may list prose-only keys under `collapse_empty_substitutions`. When every placeholder on such a line is opted in and renders exactly empty, the vacated line goes, plus one adjacent blank when keeping it would leave a run (file edges count as blank). Never opt in a key used inside fenced/indented code or raw HTML literal content — `scripts/lint-collapse-sites.py` fails CI on a key whose Markdown template site is not a whole-line placeholder in prose and rejects non-Markdown destinations, since the engine itself cannot classify every file format safely. The `render-check` CI job renders the full surface — every harness — against `tests/fixtures/render-check/.activeloom-config.yml` and runs `prettier --check`, then renders `tests/fixtures/render-fidelity/` and diffs it against a checked-in golden so an unintentional rewrite fails too.
- **Consumer half (your `.activeloom-config.yml`).** The engine does not parse or reformat the contents of substitution values beyond stripping their trailing newlines. Tables, lists, and paragraphs inside them must already be prettier-clean markdown (e.g. a blank line between a paragraph and the list that follows it, aligned table pipes). This is a hard boundary, not a best effort: the engine deliberately has no Markdown parser, because rewriting rendered output without knowing which bytes came from a value corrupts literal content that Prettier preserves. If a sync PR's diff against your repo is pure whitespace, your values are the first place to look.

## Adding a new file to the sync surface

1. Add an entry to the right set in `scripts/sync-targets.yml` — under `harnesses.<name>.targets` for a file only that harness's consumers should receive, or under `shared.targets` for a harness-independent one — with `source`, `destination`, and `substitutions: []` (or the placeholder list). For a Markdown destination, if an empty prose-only placeholder sits between blank separators, also add that key to `collapse_empty_substitutions` — it must appear in **both** lists, or the sync fails closed for every consumer. Do not opt in keys used in literal content; the `sync-targets` CI job checks both rules and rejects collapse opt-ins for non-Markdown destinations.
2. Two target sets must not write the same destination unless the entry is `create_if_missing`; CI fails the manifest otherwise. If the file uses placeholders, update each consumer's `.activeloom-config.yml` to provide the new values **before** the sync runs — otherwise the sync workflow fails closed for every consumer until they catch up.
3. Run the sync manually against one consumer first as a smoke test.
