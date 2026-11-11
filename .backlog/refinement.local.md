# Backlog refinement — local rubric

> Repository-owned and never synced. It extends the synced core rubric (`core-rubric.md` in the `backlog-refinement` skill) with this repository's instances: its branch, its sensitive paths, its labels, its own transformations. Where this file and the core disagree, this file wins. A rule that would hold in any repository belongs in the core — list it under _Upstream candidates_ until it lands there.
>
> **Local rubric version: 1** — bump on every material change and record the bump in `learnings.local.md`.

## Settings

- **Integration branch:** `main`
- **Rewrite mode:** `edit` <!-- edit | suggest -->

`main` is the default branch, so loop PRs target it and a PR's `Closes #N` closes the issue on merge. There is no merged-but-unpromoted state to track: an open issue whose fix is on `main` is stale.

Priority labels, highest first. An empty marker turns priority-setting off. Keep exactly one marker line.

<!-- priority-labels: priority: critical, priority: high, priority: medium, priority: low -->

Labels on issues a scheduled workflow opens and closes; refinement skips them entirely. Empty means none. Keep exactly one marker line.

<!-- auto-managed-labels: -->

Title prefixes that record a priority someone already set, highest first and one per priority label above (e.g. `[P0], [P1], [P2], [P3]`). Refinement treats a matching prefix as an existing priority. Empty means titles carry none. Keep exactly one marker line.

<!-- priority-title-prefixes: -->

What refinement does with a verified-stale issue: `close` it itself once its evidence is validated against the integration branch, after the evidence comment, or `recommend` a close for a human. Keep `close` unless a wrong close is expensive to notice or undo. Keep exactly one marker line.

<!-- stale-action: close -->

Interview handback confirmation: `ask` shows one combined preview and asks before posting/applying; `auto` skips only that handback question. Both require the user to confirm the interview summary and each offered close. Keep exactly one marker line.

<!-- grill-handback: ask -->

## Priority definitions

_This repository uses the core rubric's tier table unchanged._

## Additional disqualifiers

These are this repository's sensitive paths: a non-trivial change to any of them needs a human reviewer even with green CI. Each uses a core `agent-bail:` category; there is no local category. A one-line typo or comment fix there may still be agent work; anything touching the invariant is not.

- **`agent-bail: credential-gate`** — signing and attestation: `scripts/create-signed-commit.py`. It protects the invariant that commits and review attestations are signed by the identity they claim; a change can weaken provenance without failing any test.
- **`agent-bail: credential-gate`** — CI and release workflows: `.github/workflows/**`. They hold the repository's token permissions, secret use, action pins, and the publish pipeline; a change alters what automation can do with credentials, and is only proven by a run with those credentials.
- **`agent-bail: credential-gate`** — the agent-loop gh guard hooks: `.claude/skills/agent-loop/scripts/hook-gh-guard` and its `.codex/` and `.agents/` copies. They bound which GitHub operations an unattended agent may run; a loop iteration must never edit the guard that constrains it.
- **`agent-bail: credential-gate`** — the installer CLI: `cli/**`. It is published as a package and writes into consumers' repositories; a change ships to every installer through a credential-gated release, and a mistake overwrites files outside this repository.
- **`agent-bail: synced-surface`** — the synced surface: `.claude/**`, `.codex/**`, `.agents/**`, their sources under `prompts/**`, `packages/review-ledger/**` (vendored into the skills as `review-ledger.js`), and the shared templates `scripts/review-telemetry.json.template` and `.github/copilot-instructions.md.template`. Consumers sync these files, so every change reaches other repositories, and the skills and agent definitions there are prompts whose wording steers unattended runs. Many files are generated from sources under `prompts/`; ownership has to be established before an edit.

When several apply, the core rubric's _Choosing the category_ order holds: `credential-gate` beats `synced-surface` — so a change to `.claude/skills/agent-loop/scripts/hook-gh-guard` is `credential-gate`.

## Additional transformations

_Repository-specific make-ready transformations, in the core rubric's §2 table shape. Empty until an RCA adds one._

## Upstream candidates

_Rules recorded here that would hold in any repository. Propose each to the core rubric upstream, then delete it here once the synced core carries it._
