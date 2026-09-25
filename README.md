# ActiveLoom

**Reusable, multi-harness engineering workflows and review protocols for coding agents, distributed from one upstream to many repositories.**

ActiveLoom supplies portable skills, adversarial review protocols, supporting scripts, and repository configuration across multiple coding agent harnesses and frontier model families. It natively supports **Claude Code** (Anthropic Claude models), **OpenAI Codex** (OpenAI GPT and reasoning models), and **Google Gemini** through Antigravity/Agy (Gemini models), alongside hosted review integration for **GitHub Copilot**.

It helps an existing agent plan work, investigate bugs, implement bounded tasks, and review pull requests with traceable, ledger-backed findings. Its installer and sync engine keep those workflows consistent while each repository owns its project context.

This is the unified project formerly named **claude-platform**. New consumers use `loomantix/activeloom`, one `.activeloom-config.yml`, and the harnesses and models they select. Separate engine upstreams are not required.

**Point an agent here:** [Agent entry guide](docs/agent-guide.md). It contains a copyable session prompt, a task-to-skill map, prerequisites, and a source ownership map. For installation, use [Getting started](docs/getting-started.md). To go from nothing installed to an agent working your backlog, use [Set up for autonomous development](docs/autonomous-development.md).

## What it provides

| Capability                 | What you get                                                                                                                                                                                                            |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Engineering skills         | Instructions and helpers for design, diagnosis, implementation, issue management, and review. Load the skill relevant to the task.                                                                                      |
| Multi-harness & models     | First-class support for Claude Code (`.claude/`), OpenAI Codex (`.codex/`), and Antigravity/Agy (`.agents/`), running across Anthropic, OpenAI, and Google model families. Shared skills are rendered across harnesses. |
| Cross-model PR review      | Draft-PR-first adversarial review where independent models critique code to eliminate single-model blind spots, with a shared ledger tying review evidence to a specific commit.                                        |
| Model & role configuration | Configure separate worker and reviewer models, adjust reasoning effort tiers (`low`, `medium`, `high`), and enable automatic model fallbacks via `review-setup` and `review-config`.                                    |
| Bounded automation         | A deterministic runner for explicitly requested automatic review chains, plus a separate `agent-loop` workflow for an allowlisted issue queue.                                                                          |
| Repository distribution    | A CLI for personal or repository installation, and a sync engine that proposes downstream changes through pull requests.                                                                                                |
| Local customization        | Consumer-owned configuration, deterministic validation contracts, and review addenda for project rules and domain knowledge.                                                                                            |

## What it does not provide

- **An agent runtime or hosted model service.** Bring your own agent clients, model subscriptions, and API keys (Anthropic, OpenAI, Google). ActiveLoom supplies the prompt architectures, skills, and review protocols executed by those tools.
- **A hosted engineering platform.** The repository ships files and local/CI tooling, not a hosted workspace, dashboard, or managed execution service.
- **A general-purpose agent application SDK.** Its focus is engineering work in repositories; it is not an application framework for building arbitrary agents.
- **Identical behavior across engines.** Review prompts deliberately differ by harness. Skill availability, launch requirements, and telemetry support also differ.
- **Automatic knowledge of your project.** The installer detects facts and leaves judgment-dependent fields for the `onboard` skill and a maintainer to resolve.
- **A guarantee of correctness, security, or compliance.** Review evidence complements tests and human judgment. Local automation runs with the reviewer's permissions; it is not a security sandbox.
- **Implicit permission to ship.** A converged review is evidence about a commit. The automatic review runner does not mark ready or merge, and sync proposes changes for review.

## Choose a starting point

| Your task                                  | Start with                                                    | Read next                                                                        |
| ------------------------------------------ | ------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Decide whether this fits an agent session  | The agent entry guide                                         | [Task selection and boundaries](docs/agent-guide.md)                             |
| Have agents work the backlog unattended    | The setup guide's session prompt                              | [Set up for autonomous development](docs/autonomous-development.md)              |
| Try a workflow personally                  | CLI `add <skill> --harness <id>`                              | [Personal installation](docs/getting-started.md#tier-0-try-it)                   |
| Give a repository a shared setup           | CLI `init --harness <id>` and the `onboard` skill             | [Repository installation](docs/getting-started.md#tier-1)                        |
| Keep multiple repositories current         | CLI `init --sync`                                             | [Sync setup](docs/getting-started.md#tier-2) and [sync contract](docs/sync.md)   |
| Configure models, effort, and review order | CLI `review-config` or `review-setup` skill                   | [Review profile setup](docs/getting-started.md#referencing-the-synced-docs)      |
| Review an existing PR                      | The installed harness's `REVIEW_WORKFLOW.md` and review skill | [Review entry points](docs/agent-guide.md#review-an-existing-pr)                 |
| Run an automatic review chain              | The deterministic review runner                               | [Runner requirements and outcomes](.codex/references/review-chain-runner.md)     |
| Improve the toolkit itself                 | Source ownership map and contribution guide                   | [Contributing](CONTRIBUTING.md) and [prompt rendering](docs/prompt-rendering.md) |

## Set up for autonomous development

Ask any agent, in any harness: _"Help me set up ActiveLoom for autonomous development in this repository."_ Point it at [the setup guide](docs/autonomous-development.md), which it follows stage by stage:

```
install + sync → onboard → review-setup → backlog-refinement setup → agent-loop config
      → refine the backlog → agent-loop on an allowlist → rca feeds lessons back
```

The agent briefs you first: what the pipeline does, what it needs (`gh`, and both the Claude and Codex CLIs for `agent-loop`), and what it will change. It asks before creating labels, rewriting issues, or claiming them. It detects how far a repository already is and resumes from the first incomplete stage, so the same request repairs a partial setup. The decisions that steer an unattended agent — integration branch, sensitive paths, priority scheme — come to you as questions with a recommended answer, never as guesses.

`backlog-refinement` decides which issues an agent can finish and routes the rest to the interview they need (`grill` or `product-grill`). `agent-loop` implements an explicit list of ready issues, one reviewed draft PR each. Neither merges, deploys, or closes anything.

## Run the installer today

**Publication status, verified September 10, 2026:** the public npm registry does not currently serve the `activeloom` package. The CLI implementation is in [`cli/`](cli/). Use it from a source checkout; `npx activeloom` is the intended invocation after publication, not a working prerequisite today.

```bash
git clone https://github.com/loomantix/activeloom.git
cd activeloom
node cli/bin/activeloom.js tiers
node cli/bin/activeloom.js add --harness codex
node cli/bin/activeloom.js add diagnosing-bugs --harness codex --dry-run
```

The last command previews a personal installation. Remove `--dry-run` to install it. Choose `claude`, `codex`, or `gemini` explicitly to avoid relying on machine detection. `add` also installs supporting harness files; it does more than copy one `SKILL.md`.

The CLI requires Node 18.17 or later; `init` also requires Python 3.9 or later with PyYAML. Fetching upstream content requires network access. Running a skill has additional requirements described in that skill, such as an authenticated agent client and GitHub CLI for PR operations.

For repository setup, run the CLI against the **consumer repository**, not the ActiveLoom checkout. The [full walkthrough](docs/getting-started.md) provides commands and explains the four adoption tiers:

| Tier                | Scope                                                    | Update method                               | Additional sync identity                                 |
| ------------------- | -------------------------------------------------------- | ------------------------------------------- | -------------------------------------------------------- |
| 0 — Personal        | Selected skills and support files in your home directory | Rerun `add`; replacement requires `--force` | None                                                     |
| 1 — Repository      | Selected harness trees and shared configuration          | Rerun `init`, review and commit             | None                                                     |
| 2 — Scheduled sync  | Repository setup plus a GitHub Actions workflow          | Scheduled propagation PRs                   | Built-in `GITHUB_TOKEN`; repository permissions required |
| 3 — App-backed sync | Scheduled sync with GitHub-signed commits                | Propagation PRs from a configured App       | GitHub App; separate read access for a private upstream  |

Tier 2 is the recommended tier for scheduled repository updates (`init --sync`); `init --sync --app` selects Tier 3. For sync setup, a GitHub App is only ever needed at Tier 3. Personal evaluation and manual adoption do not require one.

These are adoption tiers, not the **Lean/Deep review tiers**. Choose personal or manual installation for evaluation; choose scheduled sync when maintaining shared repository workflows is the objective.

## Supported harnesses and models

ActiveLoom provides unified engineering workflows across distinct agent environments, CLIs, and model families:

| CLI/config ID | Agent harness / client       | Supported model families        | Distributed root | Entry document                                           |
| ------------- | ---------------------------- | ------------------------------- | ---------------- | -------------------------------------------------------- |
| `claude`      | Anthropic Claude Code        | Anthropic Claude                | `.claude/`       | [Claude review workflow](.claude/REVIEW_WORKFLOW.md)     |
| `codex`       | OpenAI Codex CLI             | OpenAI GPT and reasoning models | `.codex/`        | [Codex review workflow](.codex/REVIEW_WORKFLOW.md)       |
| `gemini`      | Google Antigravity / Agy CLI | Google Gemini                   | `.agents/`       | [Gemini/Agy review workflow](.agents/REVIEW_WORKFLOW.md) |

Use the selected harness's actual skill files as the authority for invocation and prerequisites. A `gemini` selection refers to this project's Agy integration; it is not a promise of compatibility with every Gemini client.

### Cross-model review and role orchestration

ActiveLoom's review protocols are built on the principle that no single model grades its own homework:

- **Adversarial review relays**: An authoring worker (e.g. Claude) implements a feature, and an independent reviewer from another model family (e.g. Codex or Gemini) performs adversarial critique. Findings are posted inline on the draft PR and recorded in an append-only ledger before any fix is applied.
- **Worker vs. reviewer model tuning**: Pair fast or code-optimized models for drafting and implementation with high-effort reasoning models for code review and verification.
- **Per-developer and per-repo profiles**: Configure reviewer models, worker models, reasoning effort levels (`low`, `medium`, `high`), and engine review order (e.g. `order.lean=codex,claude`) via the `review-setup` skill or `activeloom review-config`.
- **Capacity and rate-limit fallbacks**: Configure fallback model-and-effort pairs so automatic review runs handle upstream API capacity or rate-limit rejections without aborting the review run.
- **Hosted reviewer integration**: The `copilot-review` and `reviewit` skills incorporate GitHub Copilot and hosted multi-model reviews on pull requests alongside local review passes where repository policy permits.

Local review starts from a draft PR and resolves Lean or Deep before reviewers run. Cleanup changes the code's shape; adversarial review examines the resulting code for defects. Independent engines share a ledger protocol, while retaining different review prompts. Material fixes require fresh evidence under that protocol.

For explicitly requested automatic chains, the checked-in [review runner](.codex/references/review-chain-runner.md) controls pass order, validation, checkpoints, and termination. Its Codex control surface must be installed even when another engine starts the chain. A completed fixed plan, an exhausted budget, and convergence are different outcomes. The separate `agent-loop` implements issues; it is not the same controller.

The hosted `reviewit` skill remains an explicit option where repository policy permits it. Installation does not trigger hosted reviewers, and hosted review is not a prerequisite for the local chain.

## How updates and ownership work

The installer contains code, not the prompt payload. By default it downloads this repository at `sync-v2`; scheduled sync uses the same distribution ref. `sync-v2` is a movable release gate, not an immutable version. `main` may contain work not yet distributed, and consumers update only when they install or merge a sync PR. Explicit refs, harness selections, and consumer substitutions can produce different installed content.

`sync-v1` is a frozen historical pin from the pre-`sync-v2` protocol with no remaining consumers. See [migration guidance](docs/sync.md#migrating-from-the-pre-sync-v2-config-files) if you are reviving an old checkout.

| If you need to change…                             | The owner is…                                                                      |
| -------------------------------------------------- | ---------------------------------------------------------------------------------- |
| A generated skill                                  | `prompts/skills/` and `prompts/profiles/`; regenerate the harness outputs          |
| A harness-specific review prompt                   | The corresponding harness source; follow its authoring notes and parity rules      |
| Which files consumers receive                      | `scripts/sync-targets.yml`                                                         |
| A repository's stack, rules, or selected harnesses | Its `.activeloom-config.yml`                                                       |
| Repository-specific review lessons                 | Its consumer-owned `.review/addendum.local.md`                                     |
| A vendored ledger contract or helper               | `packages/review-ledger`, rebuilt into every harness root; do not patch the bundle |

Managed consumer files are overwritten on sync. Files marked `create_if_missing` are bootstrapped once and remain consumer-owned. See [sync ownership](docs/sync.md), [prompt rendering](docs/prompt-rendering.md), and the [review learning loop](docs/review-learning-loop.md).

## Contributing and license

ActiveLoom is public, Apache 2.0, and requires DCO sign-off on every contribution. Keep examples reusable and free of private project details. Start with [CONTRIBUTING.md](CONTRIBUTING.md); report vulnerabilities using [SECURITY.md](SECURITY.md). See [LICENSE](LICENSE) and [NOTICE](NOTICE).
