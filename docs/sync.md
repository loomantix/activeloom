# Sync from upstream

Canonical files (Codex skills, the Copilot instructions template, optional GitHub workflows) live in this repo. Consumer repos pull them via a daily-cron GitHub Action, and developers refresh their local skill set with a one-shot install script. This doc explains both flows and the on-disk contract.

## What flows where

The single-source-of-truth list is [`scripts/sync-targets.yml`](../scripts/sync-targets.yml) — it lives in the **upstream** repo (this one, or a fork). Consumers don't author it; they only opt out of specific entries via `skip_targets` in `.platform-config.yml`. Each entry maps a file in the upstream repo to a destination path in the consumer, optionally with placeholder substitution (`<<KEY>>` form) resolved from the consumer's `.platform-config.yml`.

A target with `delete: true` removes the destination from the consumer instead of writing to it (and prunes any empty parent directories). Use this to retire a previously-synced file across all consumers.

A target with `create_if_missing: true` bootstraps the destination on first sync and leaves it alone thereafter. Use this for files that consumers are expected to customize after creation (starter scaffolding, per-consumer configuration). On first creation, required substitutions are still validated and the sync hard-fails if any are missing — same contract as any other copy target. On subsequent syncs the engine short-circuits before substitution, so substitution values declared by the manifest don't have to remain present in the consumer's `.platform-config.yml` once the file exists. Mutually exclusive with `delete`.

## Bounding what the sync can write (`allowed_destinations`)

The manifest is authored **upstream**. Consumers don't review it before it runs — the cron pulls it and the engine acts on it. `allowed_destinations` is a consumer-authored policy enforced by the sync engine. It bounds accidental or unexpected manifest expansion while that engine is trusted; it is not a sandbox around arbitrary upstream code.

Set it in the consumer's `.platform-config.yml`. Every write, delete, and `create_if_missing` bootstrap must match at least one pattern, or the sync fails:

```yaml
allowed_destinations:
  - .codex/** # skills, references, review workflow
  - .github/copilot-instructions.md # templated Copilot reviewer instructions
  - .github/workflows/dco.yml # only if you want DCO enforcement synced
  - agent-loop-instructions.md # agent-loop scaffolding, consumer-owned
```

That list matches what the canonical manifest actually ships today: everything else it writes lives under `.codex/`. Trim it further if you don't want a given surface — dropping the `.github/workflows/dco.yml` line means the trusted sync engine will reject a manifest that tries to write to your workflows directory.

Patterns are gitignore-flavored globs: `**/` spans path segments, `*` and `?` stop at `/`, everything else is literal. They are anchored at both ends, so `.codex/**` does not match `.codexfoo`.

The key is tri-state, and the difference matters:

| Value                          | Behavior                                                                  |
| ------------------------------ | ------------------------------------------------------------------------- |
| **Key absent**                 | Fail-open. Warns, then trusts the manifest to write anywhere in the tree. |
| `allowed_destinations:` (null) | Config error. Almost always a mid-edit accident, so the engine refuses.   |
| Non-empty list                 | Enforced. Every destination must match a pattern.                         |
| `[]`                           | Deny everything — the "freeze this consumer" knob.                        |

> **Set this key.** The absent-key case exists only so the gate could be introduced without breaking consumers mid-flight, and the warning it prints lands in a green job where nobody reads it. A consumer without an allowlist grants the upstream manifest write access to its entire tree, including `.github/workflows/`. The next breaking sync protocol version will make the absent key fail closed; `sync-v1` keeps its migration-compatible warning behavior.

`allowed_destinations` and `skip_targets` solve different problems and don't substitute for each other. `skip_targets` is an opt-out you maintain per file as the manifest grows — it only stops what you already knew to name. `allowed_destinations` is a ceiling that also applies to targets the upstream adds later, which is the case that actually matters.

### What it does not cover

Two gaps to know about, so the allowlist isn't mistaken for a stronger boundary than it is:

- **Upstream code execution.** The workflow runs `scripts/sync-engine.py` from the verified upstream checkout. A malicious engine can ignore this policy or write outside it, so `allowed_destinations` protects against manifest drift only while the engine itself is trusted. The tag-signature gate authenticates the release signer; it does not sandbox the released code.
- **Overwrites of sensitive paths.** The engine refuses to `delete:` `.github/workflows/**`, `.github/CODEOWNERS`, lockfiles, and Dockerfiles, but it does not block _overwriting_ them. A rewritten workflow doesn't fail loudly — it runs, with your secrets. Keep sensitive paths out of `allowed_destinations` unless you genuinely want them synced.
- **The commit step is broader than the engine.** `create-signed-commit.py` commits everything `git status` reports in the working tree, not just what the engine wrote, so the allowlist does not constrain what ultimately lands in the sync PR. Review the PR diff; that is still the backstop.

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
git tag -sf sync-v1 -m "Retag sync-v1 to <reason>" <commit-sha>
git push --force-with-lease origin sync-v1
```

The `--force-with-lease` is required and intentional — it asserts the tag's previous SHA so a concurrent retag from another maintainer fails loudly rather than silently clobbering. The annotated message documents the cumulative changes since the previous retag.

Use `-s` (signed), not `-a`. The signature is what lets a consumer verify that the tag came from a maintainer rather than from whoever last had push access — see [Signing the tag](#signing-the-tag) below.

#### The tag is the real trust boundary

`CODEOWNERS` and branch protection gate merges into `main`. Consumers do not consume `main` — they consume the tag. **Advancing a tag is a force-push, not a pull request**, so it does not pass through code-owner review at all. Anyone who can push to this repo can point `sync-v1` at any commit, including one that was never reviewed, and every consumer picks it up on the next daily cron.

Two controls close that gap. Ship both — they fail independently.

**1. Protect the tag (server-side, protects every consumer at once).**

```bash
gh api --method POST repos/<owner>/codex-platform/rulesets --input - <<'JSON'
{
  "name": "sync tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/tags/sync-v*"],
      "exclude": []
    }
  },
  "rules": [
    {"type": "creation"},
    {
      "type": "update",
      "parameters": {"update_allows_fetch_and_merge": false}
    },
    {"type": "deletion"}
  ],
  "bypass_actors": [
    {
      "actor_id": <release-team-id>,
      "actor_type": "Team",
      "bypass_mode": "always"
    }
  ]
}
JSON
```

These creation, update, and deletion restrictions mean only the release team can create or move matching tags. GitHub's `required_signatures` ruleset rule applies to commit signatures, not the annotated tag object's signature, so it is intentionally not used here. The consumer-side `git verify-tag` gate below enforces the tag signature and pins the accepted release keys.

The `update` rule is the one that carries the most weight, and it is the reason this ruleset is **required rather than optional**. A signature proves who signed a tag object; it says nothing about whether that object is the release upstream currently intends to ship. Only the `update` rule stops the ref being moved backwards onto an older object — see [Replaying an old signed tag](#replaying-an-old-signed-tag). The consumer-side gate bounds the damage; it does not replace this.

Verify it took effect:

```bash
RULESET_ID=$(gh api repos/<owner>/codex-platform/rulesets \
  --jq '.[] | select(.name == "sync tags") | .id')
gh api "repos/<owner>/codex-platform/rulesets/${RULESET_ID}" \
  --jq '{name, target, enforcement, rules, bypass_actors}'
```

**2. Have consumers verify the signature (client-side, protects a consumer if the ruleset is missing or gets changed).** Covered next.

#### Signing the tag

Sign with SSH — no keyring or agent wrangling in CI, and the public half is what consumers pin.

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git tag -sf sync-v1 -m "Retag sync-v1 to <reason>" <commit-sha>
git push --force-with-lease origin sync-v1
```

Publish the **public** keys of everyone allowed to ship a release, in git's allowed-signers format — one principal per line:

```
maintainer-a@example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...
maintainer-b@example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...
```

Each consumer sets that block as the `SYNC_TAG_ALLOWED_SIGNERS` repo variable, and their sync workflow refuses to run the engine on a tag that doesn't verify against it. It is a variable rather than a secret because these are public keys — treating them as secret only makes rotation harder.

Rotating a signer means updating that variable in every consumer, so keep the list to people who actually cut releases.

#### Replaying an old signed tag

Retagging with `git tag -sf` mints a **brand new tag object** with a fresh signature. The previous object is not destroyed and its signature stays valid forever. That leaves a gap a signature check alone cannot see:

```bash
# Attacker has push access to upstream. No signing key required.
git push --force origin <old-tag-object-sha>:refs/tags/sync-v1
```

Every consumer's next cron run fetches that object, `git verify-tag` reports a good signature from a real maintainer, and the fleet silently rolls back onto whatever code shipped before the last fix. Nothing was forged — an old, genuine release was simply replayed.

The tag ruleset's `update` rule blocks the force-push at the source. As a consumer-side backstop, the sync workflow also refuses any tag whose **tagger date** is older than the newest one it has already accepted. That date lives inside the signed payload, so it cannot be adjusted without invalidating the signature:

| Case                                                       | Result      |
| ---------------------------------------------------------- | ----------- |
| Newer signed tag (ordinary release)                        | accepted    |
| Ref force-moved back onto an older signed tag object       | **blocked** |
| Deliberate revert published as a new signed tag, dated now | accepted    |

That last row is the distinction that matters: a real revert mints a fresh tag object dated now, even when it points back at an older commit, so intentional rollbacks still work. Only replay of a stale object is refused.

The high-water mark lives in `.github/sync-upstream-state` in the consumer repo:

```
# Managed by the sync workflow — do not edit by hand.
# Newest upstream tag accepted by the signature gate; replay guard.
last_verified_tag_timestamp=1767225600
last_verified_tag_object=4209f13...
```

It is written only when the tag actually moves, so an unchanged tag never manufactures an empty PR, and it rides along in the sync PR where it is reviewable. Deleting it resets the floor — treat an unexplained deletion in a sync diff as a finding. The guard is active only when `SYNC_TAG_ALLOWED_SIGNERS` is set; without signature verification an attacker can simply mint a fresh tag, so a date check on its own would buy nothing.

> **Existing-consumer migration:** the workflow template is copied manually; it is not a sync-manifest destination. First update the consumer workflow from the current template so it contains the `Verify upstream tag signature` step. Then set `SYNC_TAG_ALLOWED_SIGNERS` and run the workflow against a known unsigned test tag to confirm it stops before Python setup or any upstream script executes. A consumer with the updated step but no variable gets a warning and syncs anyway, so publishing signed tags does not break consumers that have not received the keys yet.

> **Unset is a temporary state, not a supported one.** Like the absent-`allowed_destinations` case, it exists only so the gate could be introduced without breaking consumers mid-flight, and `sync-v2` will make it fail closed. Until then the warning is deliberately hard to miss: an unverified run annotates the job, writes to the run summary, and — because a warning in a green job is not a control — puts a banner at the top of the body of every PR it opens. If you are reviewing a sync PR carrying that banner, the diff has no provenance behind it beyond push access to the upstream. The replay guard is also inactive in that state.

If your maintainers already sign with GPG instead, drop `gpg.format ssh`, keep `git tag -s`, and swap the consumer-side verification step to import an armored public key rather than writing an allowed-signers file — the shape of the gate is the same.

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

Skills resolve from `~/.codex/skills/` before falling back to per-repo `.codex/skills/`. Symlinking the upstream-checkout's skills into the global directory means **`git pull` in the upstream clone updates every skill instantly** — no per-repo PR-merge round-trip for the developer's own tooling.

```bash
cd <your upstream clone>
git pull
./scripts/install-skills.sh         # safe — only installs missing skills
./scripts/install-skills.sh --force # replaces existing entries (backed up)
./scripts/install-skills.sh --dry-run
```

The script symlinks `<upstream>/.codex/skills/<name>` → `~/.codex/skills/<name>`. Set `CODEX_SKILLS_DIR` to override the destination.

The CI flow still keeps the in-repo `.codex/skills/` copy in sync — that copy is what teammates without the global install (and CI contexts) use.

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

# Required for sync-v1 consumers. Bound the trusted engine to the canonical
# destinations this consumer accepts.
allowed_destinations:
  - .codex/**
  - .github/copilot-instructions.md
  - .github/workflows/dco.yml
  - agent-loop-instructions.md
```

Substitution is plain `<<KEY>>` find-and-replace — no template engine. Multi-line values use YAML block scalars (the `|` form). Keys must be `[A-Z][A-Z0-9_]*`.

## Behavior contract

- **Idempotent.** Re-running the sync against an already-synced repo writes nothing and exits 0.
- **Hard fail on missing required substitution.** If a target declares a placeholder the consumer hasn't configured, the script exits 1 — better to break the sync PR than to silently leave an unfilled `<<KEY>>` in the destination file.
- **Soft warn on undeclared placeholders in the source.** If the source contains `<<FOO>>` but `sync-targets.yml` doesn't declare `FOO` for that target, the placeholder is left intact and a warning is printed. Catches the case where a template change forgot to update the manifest.
- **File mode preserved.** Targets with `mode: "0755"` get chmod'd after write.
- **`create_if_missing` short-circuits before substitution.** When the destination already exists, the engine skips the source read, substitution, and write entirely. This means a consumer can leave `create_if_missing` substitution values undeclared after first creation without breaking later syncs.

## Adding a new consumer

1. **Verify the upstream-read secret exists** if the upstream repo is private. Set `UPSTREAM_READ_TOKEN` (fine-grained PAT or GitHub App token with `Contents: Read` on the upstream repo) on the consumer repo (or as an org-level secret scoped to the consumer). For public upstream repos, no token is needed.
2. **Verify App-token secrets exist** if you want signed sync commits. The reference template reads `SYNC_APP_ID` + `SYNC_APP_PRIVATE_KEY` from secrets — rename in the workflow file if your conventions differ.
3. Create `.platform-config.yml` at the consumer's root with values for every placeholder and the required `allowed_destinations` list shown above.
4. Copy `.github/workflows/sync-from-upstream.yml.template` to `.github/workflows/sync-from-upstream.yml` (drop the `.template` suffix), then fill in `UPSTREAM_REPO` and the secret names.
5. Set `SYNC_TAG_ALLOWED_SIGNERS` to the upstream release keys before treating the workflow as a verified sync path.
6. Manually trigger the workflow once (`gh workflow run "Sync from upstream"`) to verify the first PR opens cleanly, and exercise an unsigned test tag to confirm the signature gate fails before upstream code runs.
7. Review the first sync PR carefully — it's the largest one the consumer will ever see. Subsequent syncs only carry actual upstream changes.

## Cross-repo secret hygiene

> **Important — use `--body "$VALUE"`, not `--body -`.** Passing a secret via stdin (`echo "$TOKEN" | gh secret set --body -`) silently mangles the value: the secret ends up non-empty (so the workflow's `[ -z "$UPSTREAM_READ_TOKEN" ]` validation passes) but the bytes don't authenticate. Failure mode looks identical to a legitimate auth error (`could not read Username for github.com`). The arg form (`--body "$TOKEN"`) is the only reliable transport.

## Prettier and synced files

Synced files are formatted upstream with the canonical [`.prettierrc`](../.prettierrc) at this repo's root. If a consumer runs Prettier with a different config, its `prettier --write` will reformat synced files and the next sync will revert that formatting — producing recurring local working-tree drift.

Two ways to avoid the drift:

1. **Adopt the canonical config** — copy this repo's `.prettierrc` into your consumer repo (or extend yours from it). Prettier then produces identical output on both sides and there's no drift.
2. **Exclude synced paths from your prettier run** — paste the marker block from [`recommended-prettierignore.txt`](../recommended-prettierignore.txt) into your consumer's `.prettierignore`. Keep the `>>> platform-synced paths <<<` markers intact so the block can be replaced mechanically when the synced surface changes.

Regenerate `recommended-prettierignore.txt` whenever `scripts/sync-targets.yml` changes — the snippet mirrors its `destination:` paths.

### Rendered templates must stay prettier-clean

`.github/copilot-instructions.md` is rendered per-consumer (template + `.platform-config.yml` substitutions), so its cleanliness has two halves:

- **Template half (enforced here).** Ordinary rendering preserves the template's surrounding bytes and splices in each configured value after stripping its trailing newlines. Structural blank collapsing is explicit, not inferred: a Markdown target may list prose-only keys under `collapse_empty_substitutions`. When every placeholder on such a line is opted in and renders exactly empty, the vacated line goes, plus one adjacent blank when keeping it would leave a run (file edges count as blank). Never opt in a key used inside fenced/indented code or raw HTML literal content — `scripts/lint-collapse-sites.py` fails CI on a key whose Markdown template site is not a whole-line placeholder in prose and rejects non-Markdown destinations, since the engine itself cannot classify every file format safely. The `render-check` CI job renders `tests/fixtures/render-fidelity/` and diffs it against a checked-in golden so an unintentional rewrite fails.
- **Consumer half (your `.platform-config.yml`).** The engine does not parse or reformat the contents of substitution values beyond stripping their trailing newlines. Tables, lists, and paragraphs inside them must already be prettier-clean markdown (e.g. a blank line between a paragraph and the list that follows it, aligned table pipes). This is a hard boundary, not a best effort: the engine deliberately has no Markdown parser, because rewriting rendered output without knowing which bytes came from a value corrupts literal content that Prettier preserves. If a sync PR's diff against your repo is pure whitespace, your values are the first place to look.

## Adding a new file to the sync surface

1. Add an entry to `scripts/sync-targets.yml` with `source`, `destination`, and `substitutions: []` (or the placeholder list). The entry shape is validated fail-closed, so an authoring slip stops the sync rather than being ignored: an unrecognized key in a `targets:` entry is an error (a typo silently disables the field it was meant to enable), `substitutions` and `collapse_empty_substitutions` must each be a list of strings, and every declared key must match `[A-Z][A-Z0-9_]*`. For a Markdown destination, if an empty prose-only placeholder sits between blank separators, also add that key to `collapse_empty_substitutions`. Every collapse key must also appear in `substitutions`; the engine fails closed when that one-way subset rule is violated. Omitting an otherwise appropriate collapse key leaves the empty line intact rather than failing, so cover intentional empty-value cases in the upstream render checks. Do not opt in keys used in literal content; the `sync-targets` CI job checks declared opt-ins and rejects them for non-Markdown destinations. That lint is deliberately stricter than the engine: it also requires each opted-in key to occur at least once and each site to have a blank separator on both sides.
2. If the file uses placeholders, update each consumer's `.platform-config.yml` to provide the new values **before** the sync runs — otherwise the sync workflow fails closed for every consumer until they catch up.
3. Run the sync manually against one consumer first as a smoke test.
