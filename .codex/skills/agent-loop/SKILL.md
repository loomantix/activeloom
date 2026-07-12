---
name: agent-loop
description: Autonomous issue implementation loop with strict issue allowlisting, one linked worktree per issue, configurable setup and local review hooks, fresh-base validation, and publication only after convergent local Codex deepgrill and Claude reviews. Use when Codex should implement a bounded GitHub issue queue without hosted AI reviewers.
---

# Agent Loop

Run isolated issue workers and publish one reviewed pull request per issue. The
wrapper owns selection, claiming, worktrees, local reviews, base integration,
push, and PR creation. A worker only implements, validates, refactors, and
commits locally.

## Usage

```bash
.codex/skills/agent-loop/scripts/agent-loop.sh \
  --issues 5105,5106 --iterations 2

.codex/skills/agent-loop/scripts/agent-loop.sh \
  --issues 5105,5106 --dry-run
```

Options:

| Option             | Behavior                                                                                                                                                                      |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--issues N,N,...` | Restrict selection to exactly these issue numbers. Never fall through to unrelated ready work.                                                                                |
| `--iterations N`   | Process at most `N` issues. A legacy numeric first argument remains accepted.                                                                                                 |
| `--resume`         | Permit an eligible issue already assigned only to the current user.                                                                                                           |
| `--dry-run`        | Show selections, dependency decisions, worktree/branch paths, hooks, and publication without claiming, fetching, creating worktrees, running hooks, pushing, or creating PRs. |

Omitting `--issues` retains the ready-queue behavior for backward compatibility.
Use an allowlist for every scoped or retrospective-driven run.

Collection branches and worker-side publication are removed. Every selected
issue gets a unique `agent-loop/issue-<N>-<run>` branch and linked worktree.

## Required Consumer Files

- `agent-loop-instructions.md`: repository conventions and worker safety rules.
- `.codex/skills/agent-loop/prompt.txt`: prompt containing `{ISSUE_ID}`. Require
  a local commit and forbid push/PR creation.
- `.codex/skills/agent-loop/agent-loop.config`: hook and base configuration.
- `.codex/skills/issues/scripts/ready.py`: ready-queue provider.

These consumer files are bootstrapped with `create_if_missing: true`; merge
template changes manually into existing consumers.

## Config Interface

The config is parsed as literal `key = value` lines and is never sourced.
Unknown or duplicate keys fail closed. Hook values are shell commands executed
with the issue worktree as the current directory.

| Key                                              | Purpose                                                                                                                        |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `base_branch`                                    | Integration branch; env `AGENT_LOOP_BASE_BRANCH` overrides it.                                                                 |
| `setup_hook`                                     | Isolated bootstrap, such as `pnpm install --frozen-lockfile`. Never symlink mutable dependency directories.                    |
| `validation_hook`                                | Required non-mutating validation after the worker, every review pass, and fresh-base integration.                              |
| `claude_review_hook`                             | Required fresh local Claude review. It must fix confirmed findings, commit fixes, fail on unresolved findings, and never push. |
| `codex_review_hook`                              | Required fresh Codex `deepgrill` against `$AGENT_LOOP_REVIEW_BASE_SHA`. It has the same fix/commit/fail/no-push contract.      |
| `review_contract_version`                        | Required opt-in to the current hook contract. Set to `1`; missing or unsupported versions fail before issue selection.         |
| `review_max_rounds`                              | Positive cap on Codex-then-Claude rounds. Default `4`; cap exhaustion preserves the worktree and blocks publication.           |
| `worker_hook`                                    | Optional worker command override. Default is `codex exec`.                                                                     |
| `worker_model`, `worker_fallback_model`          | Primary and capacity-fallback models for the default worker.                                                                   |
| `worker_retries`                                 | Retries after clean capacity/timeout failures. Default `1`.                                                                    |
| `worker_timeout_seconds`, `hook_timeout_seconds` | Bounded execution time.                                                                                                        |
| `retry_on_timeout`, `retry_delay_seconds`        | Timeout retry policy.                                                                                                          |
| `dependency_gate`                                | `ready` (legacy) or `merged-to-base`.                                                                                          |
| `branch_prefix`, `worktree_root`, `log_root`     | Isolated path/ref controls.                                                                                                    |
| `log_max_kb`, `output_max_lines`                 | Bound captured logs and displayed failure tails.                                                                               |

Hooks receive `AGENT_LOOP_ISSUE_ID`, `AGENT_LOOP_BASE_BRANCH`,
`AGENT_LOOP_BRANCH`, `AGENT_LOOP_WORKTREE`, `AGENT_LOOP_LOG_DIR`, and
`AGENT_LOOP_PROMPT`. Review hooks also receive `AGENT_LOOP_REVIEW_BASE`, the
fully qualified fetched remote ref, the immutable `AGENT_LOOP_REVIEW_BASE_SHA`
captured after the round's fresh fetch, plus `AGENT_LOOP_REVIEW_ROUND` and
`AGENT_LOOP_REVIEW_ENGINE`. Both hooks must scope against the SHA so a mid-round
remote update cannot give the engines different bases.

The wrapper treats exit 0, a clean tree, an unchanged attached issue branch,
and an unchanged HEAD as a hook's no-fix signal. It cannot verify that an
arbitrary hook command launched the named engine, started a fresh session,
reviewed the required SHA, or resolved every valid finding. Consumer hook
commands must provide those guarantees and exit nonzero otherwise. Validation
hooks must not change HEAD, switch branches, or leave worktree changes.

## Existing Consumer Migration

The wrapper is upstream-owned, but config, worker instructions, and the prompt
are `create_if_missing` consumer files. Existing consumers must therefore merge
the current templates manually before the synced wrapper can run:

1. Update the Codex hook to run a fresh `deepgrill`, then the Claude hook to run
   a fresh adversarial review. Scope both to `$AGENT_LOOP_REVIEW_BASE_SHA`.
2. Make both hooks fix and commit confirmed findings, leave the issue branch
   attached and clean, avoid publication, and exit nonzero if findings remain.
3. Configure a non-mutating `validation_hook`, add
   `review_contract_version = 1`, and optionally override
   `review_max_rounds = 4` with another positive cap.
4. Merge the current local-only wording from the instruction and prompt
   templates. Sync will not overwrite those consumer-owned files.

For a non-mutating consumer smoke test from an upstream development worktree,
set `AGENT_LOOP_PROJECT_DIR=/path/to/consumer` and pass `--dry-run`. Do not use
that override for a mutating run; execute the consumer's synced script instead.

Do not put secrets, credentials, PHI, customer identifiers, or user data in
config values or hook output. The wrapper deliberately uses a generic PR body
and never copies issue bodies, model logs, or findings into GitHub.

## Deterministic Phase Order

1. Select and dependency-gate an eligible issue.
2. Claim it, detecting assignment races.
3. Create a unique worktree and branch from `origin/<base>`.
4. Run the isolated setup hook.
5. Run the worker and require a clean local commit.
6. Validate, then run a fresh Codex `deepgrill` followed by a fresh Claude
   review, validating after each pass.
7. If either reviewer commits a fix, restart at Codex. Convergence requires one
   entire Codex-then-Claude round with no commits on the same HEAD. Exhausting
   `review_max_rounds` blocks publication and preserves the worktree.
8. Fetch and merge the base again, inspect a bounded diff, and revalidate.
9. Confirm no worker/hook pushed the branch; only then push and open the PR.

Do not invoke Gemini, Copilot, `reviewit`, or any GitHub-hosted AI reviewer.

## Dependency Gate

With `dependency_gate = merged-to-base`, parse `Blocked by #N`,
`Depends on #N`, `Blocked by PR #N`, and `Depends on PR #N`. A PR dependency
passes only when GitHub reports it merged to the configured base and its merge
commit is an ancestor of the current `origin/<base>`. An issue dependency passes
only when one of its closing PRs meets the same condition. Closed issues alone
do not pass.

## Failure and Recovery

On any non-zero worker exit, inspect whether the worktree is dirty or contains
new commits. Preserve all changed or committed work and stop with recovery
commands. Retry capacity/timeouts only when the worktree is unchanged. Review,
setup, integration, validation mutation, detached/wrong-branch state, and
review non-convergence failures also preserve the worktree. Never reset, reuse,
clean, or delete a dirty recovery worktree.

Successful publication removes the clean linked worktree but retains the local
branch. Interrupted runs preserve the active worktree.

## Test Guidance

Use focused commands and bounded output. For Vitest 4, target a test with:

```bash
pnpm --filter frontend test:run TestName
```

Do not insert `--` before `TestName`; that can run the full suite.

## Source of Truth

This directory is upstream-owned and synced to consumers. Change reusable
mechanics here, not in a consumer's synced copy.
