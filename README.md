# gemini-platform

Reusable Google Antigravity and Gemini CLI skills, workflow prompts, and a sync engine for propagating agent tooling into consumer repositories. Apache 2.0 + DCO.

> **Status:** v0.1. Open-source platform for Antigravity / Gemini CLI developers: Apache 2.0, DCO, clean-room public documentation, and review-gated sync tooling.

## What's in here

### Antigravity Skills (`.agents/skills/`)

Operational skills you can install globally into `~/.gemini/config/skills/` or sync into any repository's `.agents/skills/`:

| Skill                 | What it does                                                                                                                                                                                                                                            |
| :-------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `critique`            | PR-first adversarial review. Lean mode runs `code-reviewer` + `silent-failure-hunter` lanes using independent subagents; deep mode runs six core lanes + conditional tenant-coupling.                                                                   |
| `deepcritique`        | Orchestrates six independent adversarial review lanes plus conditional tenant-coupling, using the inline PR ledger for every confirmed finding and disposition. Runs only on a resolved Deep review tier and hands a Lean changeset back to `critique`. |
| `refactorpass`        | PR-first cleanup pass; posts verified cleanup suggestions inline before applying, pushing, replying, and resolving.                                                                                                                                     |
| `pr-critique <pr>`    | Cross-engine deep review of an existing PR using the same inline finding, fix-reply, and resolution ledger.                                                                                                                                             |
| `reviewit <pr>`       | Hosted review integration; its iteration cap follows the resolved review tier.                                                                                                                                                                          |
| `copilot-review <pr>` | Address GitHub Copilot review comments systematically.                                                                                                                                                                                                  |
| `grill`               | Pre-code interview. Maps the problem as a design tree and asks the whole dependency-ordered frontier each round. Derived from [mattpocock/skills](https://github.com/mattpocock/skills) — see [NOTICE](NOTICE).                                         |
| `diagnosing-bugs`     | Debugging discipline for bugs that survived the first read — no hypothesis until a tight, deterministic loop goes red on the user's exact symptom. Derived from [mattpocock/skills](https://github.com/mattpocock/skills) — see [NOTICE](NOTICE).       |
| `feature-dev`         | Guided 5-phase feature development: discovery, architecture, implementation, validation, and review.                                                                                                                                                    |
| `issues`              | Workflow over `gh issue` with a dependency-aware ready queue. Parses `Blocked by #N` / `Depends on #N` from issue bodies.                                                                                                                               |
| `backlog-refinement`  | Curate and harden the autonomous queue: verify issues, rewrite agent-ready work, and classify exclusions.                                                                                                                                               |
| `agent-loop`          | Autonomous dev-test-critique convergence loop that records every finding, reply, and resolution in the PR ledger.                                                                                                                                       |
| `actions-usage-audit` | Read-only GitHub Actions billing and workflow-usage analysis.                                                                                                                                                                                           |
| `publish-npm-package` | Prepare, bootstrap, publish, and verify npm releases with Trusted Publishing and provenance.                                                                                                                                                            |
| `task-packet`         | Execute a markdown Task Packet end-to-end with scoped subagent delivery.                                                                                                                                                                                |
| `phone-install`       | Build a release APK from the consumer repo and install on a tethered Android device over ADB.                                                                                                                                                           |
| `ship-staging <pr>`   | Merge a ready staging PR, mark linked issues on-staging, and notify Google Chat.                                                                                                                                                                        |

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

- `sync-engine.py` reads upstream `scripts/sync-targets.yml` plus the consumer config passed via `--config` (defaulting to `.platform-config.yml`; the consumer workflow template selects `.gemini-platform-config.yml` when present), applies `<<KEY>>` substitutions, writes/deletes destination files, and supports `create_if_missing`.
- `create-signed-commit.py` creates verified sync commits through the GitHub Git Database API with a GitHub App token, and refuses to publish a branch whose commit GitHub did not attest.
- `templates/sync-from-gemini-platform.yml` is the consumer-side 2-job sandboxed workflow template.

---

## Global Install

Install the skills globally for Google Antigravity (`~/.gemini/config/skills/`), Gemini CLI (`~/.gemini/skills/`), and the agent skills directory (`~/.agents/skills/`):

```bash
git clone https://github.com/loomantix/gemini-platform.git
cd gemini-platform
./scripts/install-skills.sh --dry-run # report what would happen
./scripts/install-skills.sh           # symlink skills into global directories
./scripts/install-skills.sh --force   # replace existing entries after backup
./scripts/install-skills.sh --runtime antigravity # install only for Antigravity
./scripts/install-skills.sh --runtime gemini-cli  # install only for Gemini CLI
```

> `backlog-refinement` can read packaged templates when run globally. `agent-loop` always targets the repository where it is invoked and requires that repository's synced `agent-loop.config`, `prompt.txt`, and root instructions.

---

## Wire Up A Consumer Repo

1. Add `.gemini-platform-config.yml` at the consumer root:

   ```yaml
   substitutions:
     PROJECT_NAME: MyProject
     PROJECT_OVERVIEW: ''
     CANONICAL_DOCS: ''
     STACK_TABLE: ''
     CODE_RULES: ''
     DOMAIN_RULES: ''
     REVIEW_FOCUS: ''
     WHAT_NOT_TO_SUGGEST_EXTRA: ''

   skip_targets:
     - .github/workflows/dco.yml
     - .github/copilot-instructions.md
     - agent-loop-instructions.md

   allowed_destinations:
     - .agents/**
     - agent-loop-instructions.md
     - .github/copilot-instructions.md
     - .github/workflows/dco.yml
   ```

   `skip_targets` is where a consumer settles ownership of a destination that
   more than one upstream writes. The defaults above opt out of the shared DCO
   workflow and leave `.github/copilot-instructions.md` and
   `agent-loop-instructions.md` to whichever upstream already owns them in that
   repo — drop an entry if this repo should be the writer instead.

2. Copy `templates/sync-from-gemini-platform.yml` to `.github/workflows/sync-from-gemini-platform.yml`.
3. Vendor `scripts/create-signed-commit.py` to `.github/scripts/create-signed-commit.py`
   in the consumer, by hand. This file is deliberately **not** a sync target:
   it is the only code the trusted job runs while holding the App token, and
   the workflow's isolation depends on it being consumer-owned rather than
   upstream-writable. Update it by reviewed PR, never by sync. A consumer
   already running another sync of this family will have the file — check it
   accepts `--config` and `--base-sha-file` before reusing it.
4. Point `SYNC_APP_ID` / `SYNC_APP_PRIVATE_KEY` at a GitHub App with
   `contents: write` and `pull_requests: write` on the consumer repo. An
   organization secret with `private` visibility cannot be read from a public
   repository, so public consumers may need a different secret than private
   ones.
5. Confirm `UPSTREAM_REF` resolves. The workflow fails closed if it names
   neither a tag nor a branch on this repo.
6. Configure the daily scheduled sync workflow to keep all skills and rules
   continuously up to date.

---

## License & DCO

- Licensed under [Apache 2.0](LICENSE).
- All commits require [Developer Certificate of Origin (DCO)](DCO) sign-off (`git commit -s`).
