---
name: agent-loop
description: Autonomous issue implementation loop with strict issue allowlisting, one linked worktree and draft PR per issue, bounded local Codex/Claude review rounds, inline thread traceability, and fresh-base validation. Use for a bounded GitHub issue queue without hosted AI reviewers.
argument-hint: '[iterations] [--iterations N] [--issues N,N,...] [--include-assigned|--resume] [--dry-run]'
disable-model-invocation: true
---

# Agent Loop

Run isolated issue workers and publish one reviewed pull request per issue. The
wrapper owns selection, claiming, worktrees, base integration, draft PR
creation, local review convergence, and final readiness. A worker only implements, validates, refactors, and
commits locally — by default it is the Claude CLI, but any command can be
substituted via `worker_hook`.

## Usage

```bash
.claude/skills/agent-loop/scripts/agent-loop.sh --issues 5105,5106 --iterations 2

.claude/skills/agent-loop/scripts/agent-loop.sh --issues 5105,5106 --dry-run
```

| Option               | Behavior                                                                                                                                                                      |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--issues N,N,...`   | Restrict selection to exactly these issue numbers. Never fall through to unrelated ready work.                                                                                |
| `--iterations N`     | Process at most `N` issues. A legacy numeric first argument remains accepted.                                                                                                 |
| `--include-assigned` | Include an eligible issue assigned only to the current user. The deprecated `--resume` spelling remains an alias.                                                             |
| `--dry-run`          | Show selections, dependency decisions, worktree/branch paths, hooks, and publication without claiming, fetching, creating worktrees, running hooks, pushing, or creating PRs. |

Omitting `--issues` retains the ready-queue behavior for backward
compatibility. Use an allowlist for every scoped or retrospective-driven run.

Collection branches and worker-side publication are removed. Every selected
issue gets a unique `agent-loop/issue-<N>-<run>` branch and linked worktree.

## Required Consumer Files

- `agent-loop-instructions.md`: repository conventions and worker safety rules.
- `.claude/skills/agent-loop/prompt.txt`: prompt containing `{ISSUE_ID}`.
  Require a local commit and forbid push/PR creation.
- `.claude/skills/agent-loop/agent-loop.config`: hook and base configuration.
- `.claude/skills/issues/scripts/ready.py`: ready-queue provider (synced with
  the `/issues` skill).

These consumer files are bootstrapped with `create_if_missing: true`; merge
template changes manually into existing consumers.

## Config Interface

The config is parsed as literal `key = value` lines and is never sourced.
Unknown or duplicate keys fail closed. Hook values are shell commands executed
with the issue worktree as the current directory.

| Key                                              | Purpose                                                                                                     |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `base_branch`                                    | Integration branch; env `AGENT_LOOP_BASE_BRANCH` overrides it.                                              |
| `setup_hook`                                     | Isolated bootstrap, such as `pnpm install --frozen-lockfile`. Never symlink mutable dependency directories. |
| `validation_hook`                                | Bounded validation after the worker, after each review, and after fresh-base integration.                   |
| `review_contract_version`                        | New and migrated consumers use `3`; version `2` remains temporarily accepted for staged sync compatibility. |
| `review_max_rounds`                              | Maximum complete Codex→Claude rounds. Default `4`; cap exhaustion preserves the draft PR.                   |
| `claude_review_hook`                             | Required local Claude PR review. Reads the ledger, comments before fixes, pushes, replies, and resolves.    |
| `codex_review_hook`                              | Required local Codex PR review with the same ledger contract.                                               |
| `worker_hook`                                    | Optional worker command override. Default is the Claude CLI in headless, auto-approving mode.               |
| `worker_model`, `worker_fallback_model`          | Primary and capacity-fallback models for the default worker.                                                |
| `worker_retries`                                 | Retries after clean capacity/timeout failures. Default `1`.                                                 |
| `worker_timeout_seconds`, `hook_timeout_seconds` | Bounded execution time.                                                                                     |
| `retry_on_timeout`, `retry_delay_seconds`        | Timeout retry policy.                                                                                       |
| `dependency_gate`                                | `ready` (legacy) or `merged-to-base`.                                                                       |
| `branch_prefix`, `worktree_root`, `log_root`     | Isolated path/ref controls.                                                                                 |
| `log_max_kb`, `output_max_lines`                 | Bound captured logs and displayed failure tails.                                                            |

Hooks receive `AGENT_LOOP_ISSUE_ID`, `AGENT_LOOP_BASE_BRANCH`,
`AGENT_LOOP_BRANCH`, `AGENT_LOOP_WORKTREE`, `AGENT_LOOP_LOG_DIR`, and
`AGENT_LOOP_PROMPT`. Review hooks also receive `AGENT_LOOP_REVIEW_BASE` after a
fresh fetch plus `AGENT_LOOP_PR_NUMBER`, `AGENT_LOOP_PR_URL`,
`AGENT_LOOP_PR_HEAD_SHA`, `AGENT_LOOP_REVIEW_ENGINE`, and
`AGENT_LOOP_REVIEW_ROUND`, `AGENT_LOOP_REVIEW_BASE_SHA`, and under contract v3
`AGENT_LOOP_REVIEW_ACTOR` plus `AGENT_LOOP_REVIEW_RESULT_FILE`. Every hook writes
a structured clean, changed, or blocked result. The wrapper validates its exact
SHAs and finding fingerprints, verifies resolved v3 dispositions, and owns the
canonical pass/completion attestation. A missing, invalid, or blocked result
stops even when the hook exits zero. Validation hooks
must leave a clean tree; work they write but do not commit is not in the
reviewed head and would be discarded with the worktree.

The wrapper accepts machine-readable findings, replies, and clean-pass evidence
only from the authenticated GitHub actor resolved at startup. Review hooks must
post ledger evidence with that same identity.

For a non-mutating consumer smoke test from an upstream development worktree,
set `AGENT_LOOP_PROJECT_DIR=/path/to/consumer` and pass `--dry-run`. Do not use
that override for a mutating run; execute the consumer's synced script instead.

Do not put secrets, credentials, PHI, customer identifiers, or user data in
config values or hook output. The wrapper deliberately uses a generic PR body
and never copies issue bodies, model logs, or findings into GitHub.

## Default Worker and the Invocation Lock

When `worker_hook` is unset, the wrapper runs the Claude CLI in
`--permission-mode bypassPermissions --print` mode against the issue prompt.
That is the only `claude` invocation in the script, and it is bracketed by
`# claude-cli-invocations:start` / `:end` markers. The upstream CI gate
`.claude/lint-claude-cli-invocations.py` hashes the locked region and refuses
to pass unless the hash is listed for this path in
`.claude/claude-cli-invocations.allowlist`. Any change to the flags, model
handling, or prompt wiring rotates the hash and must be re-approved by a
byte-level review of the region in the same PR — the diff is the audit trail.
A consumer that sets `worker_hook` supplies its own runner and the Claude CLI
is not required on `PATH`.

## Deterministic Phase Order

1. Select and dependency-gate an eligible issue.
2. Claim it, detecting assignment races.
3. Create a unique worktree and branch from `origin/<base>`.
4. Run the isolated setup hook.
5. Run the worker and require a clean local commit.
6. Fetch and merge the base, inspect the diff, validate, push, and open a draft PR.
7. Run a Codex pass and then a Claude pass against the PR ledger. Each hook
   comments before fixes, pushes normally, posts structured fix and final-lane
   completion evidence, then resolves.
8. If either engine made material fixes, restart from Codex. Stop after
   `review_max_rounds` and preserve the draft.
9. Require a complete clean round plus replies and resolutions on every marked
   thread, then mark the PR ready.

Do not invoke Gemini, Copilot, `reviewit`, or any GitHub-hosted AI reviewer.

## Dependency Gate

With `dependency_gate = merged-to-base`, parse `Blocked by #N`, `Depends on #N`,
`Blocked by PR #N`, and `Depends on PR #N`. A PR dependency passes only when
GitHub reports it merged to the configured base and its merge commit is an
ancestor of the current `origin/<base>`. An issue dependency passes only when
one of its closing PRs meets the same condition. Closed issues alone do not
pass. `dependency_gate = ready` (the default) preserves the legacy ready-queue
semantics.

## Failure and Recovery

On any non-zero worker exit, inspect whether the worktree is dirty or contains
new commits. Preserve all changed or committed work and stop with recovery
commands. Retry capacity/timeouts only when the worktree is unchanged. Review,
setup, integration, and validation failures also preserve the worktree. Never
reset, reuse, clean, or delete a dirty recovery worktree.

Successful publication removes the clean linked worktree but retains the local
branch. Interrupted runs preserve the active worktree.

## Migration From the Collection-Branch Loop

The previous loop pushed every iteration to a shared collection branch and
opened one summary PR at the end. That model is gone:

- The removed `[collection-branch]` positional now errors. Scope a run with
  `--issues N,N,...` instead. The numeric `[iterations]` positional still works,
  and `--iterations N` is its explicit form.
- Each issue now gets its own branch, worktree, and PR — there is no summary PR.
- `agent-loop.config`, `prompt.txt`, and `agent-loop-instructions.md` are
  `create_if_missing` targets, so existing consumers keep their old copies. They
  must be migrated by hand: set `review_contract_version = 3`, add
  `review_max_rounds`, and update both review hooks to the PR-ledger contract.
  Old hooks that prohibit all pushes will fail closed because each fix must be
  pushed normally to the exact draft-PR branch. Every v3 hook must write its
  structured result; it must not post pass/completion attestations because the
  wrapper validates the result and owns those markers.

## Test Guidance

Use focused commands and bounded output. For Vitest 4, target a test with:

```bash
pnpm --filter frontend test:run TestName
```

Do not insert `--` before `TestName`; that can run the full suite.

## Source of Truth

This directory is upstream-owned and synced to consumers. Change reusable
mechanics here, not in a consumer's synced copy.
