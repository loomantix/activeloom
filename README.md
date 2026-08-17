# gemini-platform

Reusable Google Antigravity and Gemini CLI skills, workflow prompts, and a sync engine for propagating agent tooling into consumer repositories. Apache 2.0 + DCO.

> **Status:** v0.1. Open-source platform for Antigravity / Gemini CLI developers: Apache 2.0, DCO, clean-room public documentation, and review-gated sync tooling.

## What's in here

### Antigravity Skills (`.agents/skills/`)

Operational skills you can install globally into `~/.gemini/config/skills/` or sync into any repository's `.agents/skills/`:

| Skill | What it does |
| :--- | :--- |
| `critique` | PR-first adversarial review. Lean mode runs `code-reviewer` + `silent-failure-hunter` lanes using independent subagents; deep mode runs six core lanes + conditional tenant-coupling. |
| `deepcritique` | Orchestrates deep adversarial review with parallel subagents (`invoke_subagent`), applying 4 failure lenses (State-Matrix Coupling, Probe Precision, Data Safety/Telemetry, Pending-Public Sanitization) and an inline PR ledger. |
| `refactorpass` | PR-first cleanup pass; posts verified cleanup suggestions inline before applying, pushing, replying, and resolving. |
| `pr-critique <pr>` | Cross-engine deep review of an existing PR using the same inline finding, fix-reply, and resolution ledger. |
| `reviewit <pr>` | Hosted review integration with bounded lean/deep modes. |
| `copilot-review <pr>` | Address GitHub Copilot review comments systematically. |
| `grill` | Pre-code interview. Maps the problem as a design tree and asks the whole dependency-ordered frontier each round. Integrates with `/grill-me`. Derived from [mattpocock/skills](https://github.com/mattpocock/skills) — see [NOTICE](NOTICE). |
| `diagnosing-bugs` | Debugging discipline for bugs that survived the first read — no hypothesis until a tight, deterministic loop goes red on the user's exact symptom. Derived from [mattpocock/skills](https://github.com/mattpocock/skills) — see [NOTICE](NOTICE). |
| `feature-dev` | Guided 5-phase feature development: discovery, architecture, implementation, validation, and review. |
| `issues` | Workflow over `gh issue` with a dependency-aware ready queue. Parses `Blocked by #N` / `Depends on #N` from issue bodies. |
| `backlog-refinement` | Curate and harden the autonomous queue: verify issues, rewrite agent-ready work, and classify exclusions. |
| `agent-loop` | Autonomous dev-test-critique convergence loop that records every finding, reply, and resolution in the PR ledger. |
| `actions-usage-audit` | Read-only GitHub Actions billing and workflow-usage analysis. |
| `publish-npm-package` | Prepare, bootstrap, publish, and verify npm releases with Trusted Publishing and provenance. |
| `task-packet` | Execute a markdown Task Packet end-to-end with scoped subagent delivery. |
| `phone-install` | Build a release APK from the consumer repo and install on a tethered Android device over ADB. |
| `ship-staging <pr>` | Merge a ready staging PR, mark linked issues on-staging, and notify Google Chat. |

### Antigravity References (`.agents/references/`)

Specialized role prompts loaded on-demand via progressive disclosure:
- `roles/code-reviewer.md`
- `roles/silent-failure-hunter.md`
- `roles/security-reviewer.md`
- `roles/type-design-analyzer.md`
- `roles/comment-analyzer.md`
- `roles/pr-test-analyzer.md`
- `roles/code-explorer.md`
- `roles/code-architect.md`

### Sync Engine (`scripts/`)

- `sync-engine.py` reads upstream `scripts/sync-targets.yml` plus consumer `.gemini-platform-config.yml` (or `.platform-config.yml`), applies `<<KEY>>` substitutions, writes/deletes destination files, and supports `create_if_missing`.
- `create-signed-commit.py` creates verified sync commits through the GitHub Contents API with a GitHub App token.
- `templates/sync-from-gemini-platform.yml` is the consumer-side 2-job sandboxed workflow template.

---

## Global Install

Install the skills globally onto your machine into `~/.gemini/config/skills/`:

```bash
git clone https://github.com/loomantix/gemini-platform.git
cd gemini-platform
./scripts/install-skills.sh --dry-run # report what would happen
./scripts/install-skills.sh           # symlink skills into ~/.gemini/config/skills/
./scripts/install-skills.sh --force   # replace existing entries after backup
```

---

## Wire Up A Consumer Repo

1. Add `.gemini-platform-config.yml` at the consumer root:
   ```yaml
   substitutions:
     PROJECT_NAME: MyProject

   skip_targets:
     - .github/workflows/dco.yml

   allowed_destinations:
     - .agents/**
     - agent-loop-instructions.md
     - .github/workflows/dco.yml
   ```
2. Copy `templates/sync-from-gemini-platform.yml` to `.github/workflows/sync-from-gemini-platform.yml`.
3. Configure the daily scheduled sync workflow to keep all skills and rules continuously up to date.

---

## License & DCO

- Licensed under [Apache 2.0](LICENSE).
- All commits require [Developer Certificate of Origin (DCO)](DCO) sign-off (`git commit -s`).
