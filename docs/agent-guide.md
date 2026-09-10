# ActiveLoom agent entry guide

Use this page to decide which part of ActiveLoom helps the current task. ActiveLoom supplies repository engineering workflows and distribution tooling. Your agent client executes those workflows using its own tools and permissions.

## Copy into a session

```text
Read https://github.com/loomantix/activeloom/blob/main/README.md and
https://github.com/loomantix/activeloom/blob/main/docs/agent-guide.md.
Assess how ActiveLoom can help with the task in this repository.

Read the repository's own agent instructions first. Inspect existing harness
roots, ActiveLoom or legacy sync configuration, and relevant installed skills.
Identify the smallest useful workflow, its prerequisites, and missing setup.
Distinguish what is installed from what current upstream documentation describes.
Report the recommended entry point and why it fits. Do not install files,
enable automation, or start reviewers solely because this assessment names them.
Proceed with implementation or review only within the task's existing scope.
```

When the session already has an implementation or review task, append its concrete scope and completion criteria. The prompt above alone requests an assessment, not installation or a review run.

## Establish the local facts

1. Read the consumer repository's `AGENTS.md`, `CLAUDE.md`, or equivalent. Preserve its provider choices, worktree rules, validation requirements, and action boundaries.
2. Inspect `.activeloom-config.yml`, or the legacy `.platform-config.yml`, `.codex-platform-config.yml`, and `.gemini-platform-config.yml`. Identify selected harnesses and existing sync workflows before proposing another installation.
3. Locate the relevant installed `SKILL.md` and its supporting references. The distribution roots are `.claude/`, `.codex/`, and `.agents/`; personal installations can also supply skills. Do not assume the skill loaded by the session is the repository copy.
4. Establish the installed revision where available: configured upstream ref, latest merged sync commit, and ledger version/integrity files for review work. A movable tag name alone does not prove which bytes are installed.
5. Read only the selected skill and the references it requires. Check tools, authentication, and prerequisites before attempting its workflow. An installed prompt is not evidence that a reviewer launcher is usable.

If ActiveLoom is absent, use [Getting started](getting-started.md) to prepare the appropriate installation. If it is already present, prefer the installed contract and diagnose missing or outdated pieces before replacing files.

## Select by task

Names below identify skills; use the invocation supported by the current harness. Availability must be checked in that harness's directory or the CLI's `add --harness <id>` listing.

| Need                                         | Entry point                                | Boundary                                                                                                |
| -------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| Clarify a design before code                 | `grill`                                    | An interview and design exercise; does not implement the feature                                        |
| Investigate a reproducible bug               | `diagnosing-bugs`                          | Establish the symptom and a tight reproduction before changing code                                     |
| Build a feature with architectural discovery | `feature-dev`                              | Requires a concrete user objective and repository context                                               |
| Fill project-specific onboarding fields      | `onboard`                                  | Draft evidence-based values for confirmation; do not invent domain rules                                |
| Manage issue dependencies and readiness      | `issues`                                   | Uses GitHub issue state; follow the command's read/write scope                                          |
| Prepare issues for autonomous work           | `backlog-refinement`                       | Curates the queue; does not authorize implementing every open issue                                     |
| Implement a bounded issue queue              | `agent-loop`                               | Requires an explicit issue allowlist, harness-specific worker/review setup, and its own launch contract |
| Execute a supplied implementation packet     | `task-packet`                              | Follow the packet's acceptance criteria and action boundaries                                           |
| Clean up or review an existing PR            | `refactorpass`, `critique`, `deepcritique` | Read the review workflow and ledger first; resolve the tier before running                              |
| Automate a local review relay                | `review-chain-runner.py`                   | Requires an authorized plan, clean dedicated worktree, draft PR, reviewers, and validation commands     |
| Handle hosted review comments                | `copilot-review` or `reviewit`             | Only when the task and repository policy permit that hosted workflow                                    |

Specialized skills also exist, such as Actions usage auditing, mobile installation, accessibility review, and package publication. Their presence varies by harness; inspect their requirements rather than treating the toolkit as a uniform menu.

Not every artifact here is a skill: [`claude/github-api-usage.md`](../claude/github-api-usage.md) is drop-in guidance on rate-limit-aware GitHub API usage, written to be pasted into a project's own agent guide.

## Review an existing PR

Read the installed harness's `REVIEW_WORKFLOW.md`, then `references/local-review-ledger.md`, then the selected review skill. Upstream entry points are [Claude](../.claude/REVIEW_WORKFLOW.md), [Codex](../.codex/REVIEW_WORKFLOW.md), and [Gemini/Agy](../.agents/REVIEW_WORKFLOW.md).

The protocol uses a draft PR as shared review context. Reviewers record verified findings before fixes, then push, reply with validation, and resolve threads. Evidence belongs to a specific head commit. Read both resolved and unresolved findings so another engine does not rediscover or contradict prior work without examining it.

Lean and Deep describe review scope and budgets. Resolve them from the installed workflow's triggers; the size of a diff or the presence of a `deepcritique` skill is not sufficient to choose a tier. The workflow defines how explicit deep-review requests are handled.

For automatic chains, follow the [runner reference](../.codex/references/review-chain-runner.md). The runner lives in the Codex control surface and can coordinate the supported engines. Selecting only Claude or Gemini at installation does not install that controller. A skill copied with `add` is also not proof that all peer reviewers and their supporting skills are installed.

Use checked-in launchers and the required validation commands. Launcher failure, missing evidence, and exhausted budgets must be reported as such. A fixed plan can complete without independent convergence. Success does not authorize merge, deployment, or a new run with a reset budget.

## Install only the scope you need

| Scope               | Appropriate when                                 | What changes                                                                                                     |
| ------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| Personal `add`      | One developer wants selected skills              | Home-directory skills and supporting harness files; existing files are preserved unless replacement is requested |
| Repository `init`   | The team should share reviewed workflow files    | Selected harness trees, shared targets, and consumer configuration                                               |
| `init --sync`       | The repository should receive update proposals   | Repository setup plus a scheduled GitHub Actions workflow                                                        |
| `init --sync --app` | App identity or signed sync commits are required | App-backed workflow plus separately configured permissions and credentials                                       |

Use explicit `--harness` selections and preview the change. `init --dry-run` on a new consumer cannot render the full tree until its config exists; its output reports that limitation. Review the actual generated diff after installation and fill `TODO(activeloom)` fields before relying on the resulting project guidance.

Scheduled sync requires its workflow on the default branch and the appropriate repository permissions. A generated workflow file is not proof of an enabled or successful schedule. See [setup](getting-started.md) for requirements and [sync](sync.md) for ownership and migration.

## Know which source to edit

| Source                                    | Purpose                                                      | Editing rule                                                                                   |
| ----------------------------------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| `prompts/skills/` and `prompts/profiles/` | Shared skill sources and harness vocabulary                  | Edit here for rendered skills, then run the renderer                                           |
| `prompts/rendered-files.txt`              | Generated ownership inventory                                | Inspect to identify generated files; do not maintain by hand                                   |
| `.claude/`, `.codex/`, `.agents/`         | Distributed prompts, references, and helpers                 | Some files are generated or vendored; establish ownership first                                |
| `scripts/sync-targets.yml`                | Consumer destination and ownership contract                  | Controls shared and selected-harness targets                                                   |
| `cli/`                                    | Installer and detection code                                 | Separate from the content it downloads                                                         |
| `imports/`                                | Imported engine trees                                        | New consumers use the root manifest and harness trees, not separate installs from these copies |
| Consumer `.activeloom-config.yml`         | Harness selection, substitutions, path permissions, opt-outs | Owned by that consumer                                                                         |
| Consumer `.review/addendum.local.md`      | Repository-specific review lessons                           | Keep local context here; sync preserves it                                                     |

Review prompts intentionally retain engine-specific implementations. Do not consolidate them merely because their names match. Read [prompt rendering](prompt-rendering.md), [decision records](decisions/README.md), and [Contributing](../CONTRIBUTING.md) before changing the shared source.

## Report a useful assessment

Return the applicable skill or controller, the installed harness and source you inspected, the missing prerequisites, the files an installation would change, and the next action justified by the user's task. For a review result, also distinguish completed work from missing current-head evidence and name the actual runner outcome when used.

Keep secrets, private project details, and operational logs out of public ActiveLoom issues and contributions. Generic lessons may belong upstream; repository-specific lessons belong in the consumer. The [review learning loop](review-learning-loop.md) explains that distinction.
