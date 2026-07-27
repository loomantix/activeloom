# codex-platform

Reusable Codex skills, workflow prompts, and a sync engine for propagating agent tooling into consumer repos. Apache 2.0 + DCO.

> **Status:** v0.1. The Codex surface is young, but the repository is structured for public use: Apache 2.0, DCO, public-safe docs, and review-gated sync tooling.

## What's in here

### Codex skills (`.codex/skills/`)

Operational skills you can install globally or sync into any repo:

| Skill                 | What it does                                                                                                                                                    |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `refactorpass`        | PR-first cleanup pass; posts verified cleanup suggestions inline before applying, pushing, replying, and resolving.                                             |
| `grill`               | PR-first adversarial review. Lean mode runs code-reviewer + silent-failure-hunter lanes; deep mode runs six core lanes plus a conditional tenant-coupling pass. |
| `deepgrill`           | Opens or reuses a draft PR and orchestrates refactorpass plus `grill deep` with an inline finding ledger.                                                       |
| `pr-grill <pr>`       | Cross-engine deep review of an existing PR using the same inline finding, fix-reply, and resolution ledger.                                                     |
| `reviewit <pr>`       | Optional hosted fallback for developers without local Claude Code. Orchestrates post-push Gemini Flash + Copilot review with bounded lean/deep modes.           |
| `copilot-review <pr>` | Address GitHub Copilot review comments systematically.                                                                                                          |
| `feature-dev`         | Guided feature development: discovery, architecture, implementation, validation.                                                                                |
| `issues`              | Thin workflow over `gh issue` with a dependency-aware ready queue. Parses `Blocked by #N` / `Depends on #N` from issue bodies.                                  |
| `backlog-refinement`  | Curate and harden the autonomous queue: verify issues against the integration branch, rewrite agent-ready work, classify exclusions, and learn from loop bails. |
| `agent-loop`          | Experimental Codex relay that opens a draft PR before bounded Codex/Claude convergence and records every finding, reply, and resolution there.                  |
| `actions-usage-audit` | Read-only GitHub Actions billing and workflow-usage analysis with month-over-month attribution.                                                                 |
| `task-packet`         | Execute a markdown Task Packet end-to-end.                                                                                                                      |
| `phone-install`       | Build a release APK from the consumer repo and install it on a tethered Android device over wireless ADB.                                                       |
| `ship-staging <pr>`   | Merge a ready staging PR, mark linked issues on-staging, refresh the local staging reference, and notify Google Chat.                                           |

The synced review workflow supports two developer-selected cross-model paths:
local Codex/Claude convergence when both CLIs are available, or `reviewit` as a
hosted Gemini/Copilot fallback when local Claude Code is unavailable. Consumers
can declare their default path in repository instructions without removing the
other platform capability. Local convergence restarts only for material review
fixes; validated minor-only polish does not keep the cycle running. Every local
finding is recorded before its fix, then replied to with the fix SHA and
validation before resolution.

### Codex references (`.codex/references/`)

Longer role prompts live as references instead of always-loaded instructions:

- `roles/code-explorer.md`
- `roles/code-architect.md`
- `roles/code-reviewer.md`
- `roles/silent-failure-hunter.md`
- `roles/type-design-analyzer.md`
- `roles/comment-analyzer.md`
- `roles/pr-test-analyzer.md`
- `roles/security-reviewer.md`

Skills can load these when they need a specialized review, exploration, or architecture stance.

### Sync engine (`scripts/`)

The sync engine is intentionally agent-agnostic:

- `sync-engine.py` reads upstream `scripts/sync-targets.yml` plus consumer `.platform-config.yml`, applies `<<KEY>>` substitutions, writes or deletes destination files, and supports `create_if_missing`.
- `create-signed-commit.py` creates sync commits through the GitHub Contents API so GitHub can mark them verified when run with a GitHub App token.
- `.github/workflows/sync-from-upstream.yml.template` is the consumer-side workflow template.

## Install

Install the skills once per developer machine before expecting slash-skill commands such as `deepgrill`, `reviewit`, or `agent-loop` to resolve:

```bash
git clone https://github.com/loomantix/codex-platform.git
cd codex-platform
./scripts/install-skills.sh --dry-run # report what would happen
./scripts/install-skills.sh           # symlink missing skills into ~/.codex/skills/
./scripts/install-skills.sh --force   # replace existing entries after backup
```

Updates flow through `git pull` in this checkout. Existing symlinks pick up edits automatically.

If a skill command is not found in a Codex session, run the dry-run check from this checkout. Use the normal installer for missing links; use `--force` only when the dry-run reports stale symlinks or regular files that should be replaced. Start a fresh Codex session after changing installed skills so discovery reloads.

Consumer-owned `create_if_missing` targets are intentionally not upgraded by a
sync. Existing `agent-loop` consumers must manually merge the current config,
instruction, and prompt contracts—including `review_contract_version = 2`—
before using the synced convergence wrapper; see the skill's **Existing
Consumer Migration** section.

## Wire Up A Consumer Repo

1. Add `.platform-config.yml` at the consumer root with template substitutions.
2. Copy `.github/workflows/sync-from-upstream.yml.template` to `.github/workflows/sync-from-upstream.yml`.
3. Fill in `UPSTREAM_REPO`, branch, and secret names.
4. Set the GitHub App secrets on the consumer.
5. Run `gh workflow run "Sync from upstream" --repo <owner>/<consumer>`.
6. Reference `.codex/REVIEW_WORKFLOW.md` from the consumer `AGENTS.md` and declare
   whether that consumer defaults to local convergence or the hosted fallback.

## Design Notes

This repo keeps the durable parts of the old platform model: GitHub issue queue helpers, review automation, Copilot/Gemini plumbing, and tag-gated sync PRs. The agent-facing layer is Codex-specific: `AGENTS.md`, `.codex/skills`, `codex exec`, and concise skill bodies with optional references.

## License

Apache 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
