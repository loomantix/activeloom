# ActiveLoom

**Reusable engineering workflows for coding agents, distributed from one upstream to many repositories.**

ActiveLoom supplies skills, review protocols, supporting scripts, and repository configuration for Claude Code, Codex, and Gemini through Antigravity/Agy. It helps an existing agent plan work, investigate bugs, implement bounded tasks, and review pull requests with traceable findings. Its installer and sync engine keep those workflows consistent while each repository owns its project context.

This is the unified project formerly named **claude-platform**. New consumers use `loomantix/activeloom`, one `.activeloom-config.yml`, and the harnesses they select. Separate engine upstreams are not required.

**Point an agent here:** [Agent entry guide](docs/agent-guide.md). It contains a copyable session prompt, a task-to-skill map, prerequisites, and a source ownership map. For installation, use [Getting started](docs/getting-started.md).

## What it provides

| Capability              | What you get                                                                                                                                   |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Engineering skills      | Instructions and helpers for design, diagnosis, implementation, issue management, and review. Load the skill relevant to the task.             |
| PR review protocol      | Draft-PR-first review, verified findings recorded before fixes, and a shared ledger tying review evidence to a specific commit.                |
| Bounded automation      | A deterministic runner for explicitly requested automatic review chains, plus a separate `agent-loop` workflow for an allowlisted issue queue. |
| Multiple harnesses      | Harness-specific prompts and tools under `.claude/`, `.codex/`, and `.agents/`; shared skills are rendered where their behavior can be shared. |
| Repository distribution | A CLI for personal or repository installation, and a sync engine that proposes downstream changes through pull requests.                       |
| Local customization     | Consumer-owned configuration and review addenda for project rules, validation commands, and domain knowledge.                                  |

## What it does not provide

- **An agent runtime or model service.** Bring the supported agent clients, model access, and tools required by the selected skill. Installing prompts does not install or authenticate reviewers.
- **A hosted engineering platform.** The repository ships files and local/CI tooling, not a hosted workspace, dashboard, or managed execution service.
- **A general-purpose agent application SDK.** Its focus is engineering work in repositories; it is not an application framework for building arbitrary agents.
- **Identical behavior across engines.** Review prompts deliberately differ by harness. Skill availability, launch requirements, and telemetry support also differ.
- **Automatic knowledge of your project.** The installer detects facts and leaves judgment-dependent fields for the `onboard` skill and a maintainer to resolve.
- **A guarantee of correctness, security, or compliance.** Review evidence complements tests and human judgment. Local automation runs with the reviewer's permissions; it is not a security sandbox.
- **Implicit permission to ship.** A converged review is evidence about a commit. The automatic review runner does not mark ready or merge, and sync proposes changes for review.

## Choose a starting point

| Your task                                 | Start with                                                    | Read next                                                                        |
| ----------------------------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Decide whether this fits an agent session | The agent entry guide                                         | [Task selection and boundaries](docs/agent-guide.md)                             |
| Try a workflow personally                 | CLI `add <skill> --harness <id>`                              | [Personal installation](docs/getting-started.md#tier-0-try-it)                   |
| Give a repository a shared setup          | CLI `init --harness <id>` and `onboard`                       | [Repository installation](docs/getting-started.md#tier-1)                        |
| Keep multiple repositories current        | CLI `init --sync`                                             | [Sync setup](docs/getting-started.md#tier-2) and [sync contract](docs/sync.md)   |
| Review an existing PR                     | The installed harness's `REVIEW_WORKFLOW.md` and review skill | [Review entry points](docs/agent-guide.md#review-an-existing-pr)                 |
| Run an automatic review chain             | The deterministic review runner                               | [Runner requirements and outcomes](.codex/references/review-chain-runner.md)     |
| Improve the toolkit itself                | Source ownership map and contribution guide                   | [Contributing](CONTRIBUTING.md) and [prompt rendering](docs/prompt-rendering.md) |

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

## Harnesses and review

| CLI/config ID | Distributed root | Entry document                                           |
| ------------- | ---------------- | -------------------------------------------------------- |
| `claude`      | `.claude/`       | [Claude review workflow](.claude/REVIEW_WORKFLOW.md)     |
| `codex`       | `.codex/`        | [Codex review workflow](.codex/REVIEW_WORKFLOW.md)       |
| `gemini`      | `.agents/`       | [Gemini/Agy review workflow](.agents/REVIEW_WORKFLOW.md) |

Use the selected harness's actual skill files as the authority for invocation and prerequisites. A `gemini` selection refers to this project's Agy integration; it is not a promise of compatibility with every Gemini client.

Local review starts from a draft PR and resolves Lean or Deep before reviewers run. Cleanup changes the code's shape; adversarial review examines the resulting code for defects. Independent engines share a ledger protocol, while retaining different review prompts. Material fixes require fresh evidence under that protocol.

For explicitly requested automatic chains, the checked-in [review runner](.codex/references/review-chain-runner.md) controls pass order, validation, checkpoints, and termination. Its Codex control surface must be installed even when another engine starts the chain. A completed fixed plan, an exhausted budget, and convergence are different outcomes. The separate `agent-loop` implements issues; it is not the same controller.

The hosted `reviewit` skill remains an explicit option where repository policy permits it. Installation does not trigger hosted reviewers, and hosted review is not a prerequisite for the local chain.

## How updates and ownership work

The installer contains code, not the prompt payload. By default it downloads this repository at `sync-v2`; scheduled sync uses the same distribution ref. `sync-v2` is a movable release gate, not an immutable version. `main` may contain work not yet distributed, and consumers update only when they install or merge a sync PR. Explicit refs, harness selections, and consumer substitutions can produce different installed content.

`sync-v1` is the frozen compatibility route for older consumers. See [migration guidance](docs/sync.md#migrating-from-the-pre-sync-v2-config-files) before changing an existing setup.

| If you need to change…                             | The owner is…                                                                           |
| -------------------------------------------------- | --------------------------------------------------------------------------------------- |
| A generated skill                                  | `prompts/skills/` and `prompts/profiles/`; regenerate the harness outputs               |
| A harness-specific review prompt                   | The corresponding harness source; follow its authoring notes and parity rules           |
| Which files consumers receive                      | `scripts/sync-targets.yml`                                                              |
| A repository's stack, rules, or selected harnesses | Its `.activeloom-config.yml`                                                            |
| Repository-specific review lessons                 | Its consumer-owned `.review/addendum.local.md`                                          |
| A vendored ledger contract or helper               | The upstream package identified by its version/integrity files; do not patch the bundle |

Managed consumer files are overwritten on sync. Files marked `create_if_missing` are bootstrapped once and remain consumer-owned. See [sync ownership](docs/sync.md), [prompt rendering](docs/prompt-rendering.md), and the [review learning loop](docs/review-learning-loop.md).

## Contributing and license

ActiveLoom is public, Apache 2.0, and requires DCO sign-off on every contribution. Keep examples reusable and free of private project details. Start with [CONTRIBUTING.md](CONTRIBUTING.md); report vulnerabilities using [SECURITY.md](SECURITY.md). See [LICENSE](LICENSE) and [NOTICE](NOTICE).
