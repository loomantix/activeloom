---
name: agent-loop
description: Autonomous issue implementation loop with strict issue allowlisting, one linked worktree and draft PR per issue, bounded local Codex/Claude review rounds, inline thread traceability, and fresh-base validation. Use for a bounded GitHub issue queue without hosted AI reviewers.
argument-hint: '[iterations] [--iterations N] [--issues N,N,...] [--include-assigned|--resume] [--resume-run FILE|--resume-batch FILE] [--dry-run]'
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

| Option                | Behavior                                                                                                                                                                      |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--issues N,N,...`    | Restrict selection to exactly these issue numbers. Never fall through to unrelated ready work.                                                                                |
| `--iterations N`      | Process at most `N` issues. A legacy numeric first argument remains accepted.                                                                                                 |
| `--include-assigned`  | Include an eligible issue assigned only to the current user. The deprecated `--resume` spelling remains an alias.                                                             |
| `--resume-run FILE`   | Resume one contract-v3 review/finalization checkpoint.                                                                                                                        |
| `--resume-batch FILE` | Resume an ordered contract-v3 allowlist from its private batch-state file. It cannot be combined with `--resume-run`, `--issues`, or `--dry-run`.                             |
| `--dry-run`           | Show selections, dependency decisions, worktree/branch paths, hooks, and publication without claiming, fetching, creating worktrees, running hooks, pushing, or creating PRs. |

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

| Key                                              | Purpose                                                                                                                                                                                                      |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `base_branch`                                    | Integration branch; env `AGENT_LOOP_BASE_BRANCH` overrides it.                                                                                                                                               |
| `setup_hook`                                     | Isolated bootstrap, such as `pnpm install --frozen-lockfile`. Never symlink mutable dependency directories.                                                                                                  |
| `validation_hook`                                | Bounded validation after the worker, after each review, and after fresh-base integration.                                                                                                                    |
| `review_contract_version`                        | New and migrated consumers use `3`; version `2` remains temporarily accepted for staged sync compatibility.                                                                                                  |
| `config_doctor`                                  | Run the non-mutating compatibility preflight before issue selection or claim.                                                                                                                                |
| `claude_effort_policy`                           | Optional literal Claude effort policy enforced by the doctor.                                                                                                                                                |
| `review_max_rounds`                              | Codex→Claude round cap from `1` through the hard ceiling `4`. Default `4`; exhaustion preserves the draft PR.                                                                                                |
| `review_timeout_seconds`                         | Positive wall-clock budget for one issue's review, persisted across resume. Default `7200`; each review pass and its validation is capped at the smaller of the remaining budget and `hook_timeout_seconds`. |
| `claude_review_hook`                             | Required local Claude PR review. Reads the ledger, comments before fixes, publishes through `$AGENT_LOOP_REVIEW_PUSH_HELPER`, replies, and resolves.                                                         |
| `codex_review_hook`                              | Required local Codex PR review with the same ledger contract.                                                                                                                                                |
| `worker_hook`                                    | Optional worker command override. Default is the Claude CLI in headless, auto-approving mode.                                                                                                                |
| `worker_model`, `worker_fallback_model`          | Primary and capacity-fallback models for the default worker.                                                                                                                                                 |
| `worker_retries`                                 | Retries after clean capacity/timeout failures. Default `1`.                                                                                                                                                  |
| `worker_timeout_seconds`, `hook_timeout_seconds` | Bounded execution time.                                                                                                                                                                                      |
| `retry_on_timeout`, `retry_delay_seconds`        | Timeout retry policy.                                                                                                                                                                                        |
| `dependency_gate`                                | `ready` (legacy) or `merged-to-base`.                                                                                                                                                                        |
| `branch_prefix`, `worktree_root`, `log_root`     | Isolated path/ref controls.                                                                                                                                                                                  |
| `log_max_kb`, `output_max_lines`                 | Bound captured logs and displayed failure tails.                                                                                                                                                             |

Hooks receive `AGENT_LOOP_ISSUE_ID`, `AGENT_LOOP_BASE_BRANCH`,
`AGENT_LOOP_BRANCH`, `AGENT_LOOP_WORKTREE`, `AGENT_LOOP_LOG_DIR`, and
`AGENT_LOOP_PROMPT`. Review hooks also receive `AGENT_LOOP_REVIEW_BASE` after a
fresh fetch plus `AGENT_LOOP_PR_NUMBER`, `AGENT_LOOP_PR_URL`,
`AGENT_LOOP_PR_HEAD_SHA`, `AGENT_LOOP_REVIEW_ENGINE`, and
`AGENT_LOOP_REVIEW_ROUND`, `AGENT_LOOP_REVIEW_BASE_SHA`, and under contract v3
`AGENT_LOOP_REVIEW_ACTOR`, `AGENT_LOOP_REVIEW_RESULT_FILE`, and
`AGENT_LOOP_REVIEW_PUSH_HELPER`. Every successfully completed clean or changed
hook calls `review-ledger.js write-result`, which derives the complete
same-engine/same-round fixed, deferred, and dismissed fingerprint set and writes
the canonical result. A changed result requires at least one fixed finding. A
blocked hook instead uses `review-ledger.js write-blocked-result` with an
owner-only blocker file and must not claim a clean or changed pass. The wrapper validates its exact
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

## Model Selection

The loop runs three model-backed aspects, and they are configured in two
different places. This is the most common onboarding question, so it is spelled
out here.

| Aspect         | Where the model is chosen                | Effort control                                                        |
| -------------- | ---------------------------------------- | --------------------------------------------------------------------- |
| Default worker | `worker_model` / `worker_fallback_model` | none — the default worker invocation takes a model only               |
| Codex review   | inside `codex_review_hook`               | inside the same command                                               |
| Claude review  | inside `claude_review_hook`              | inside the same command, and validated against `claude_effort_policy` |

A review hook is a literal shell command, so reviewer model and effort are
ordinary flags on that command rather than dedicated config keys. These
fragments show flag placement only; a working hook must also carry
`AGENT_LOOP_REVIEW_PUSH_HELPER`, `AGENT_LOOP_REVIEW_RESULT_FILE`, and
`write-result`, or contract-v3 preflight rejects it:

```
claude_review_hook = claude --print --effort low --model <model-id> /deepcritique ...
codex_review_hook  = codex exec -c model_reasoning_effort=medium ... /deepcritique ...
```

`claude_effort_policy` constrains only `claude_review_hook`, and only when
`config_doctor = true` — the doctor is what enforces it, so the key is inert
without it. It does not apply to the worker, which has no effort control.

`worker_model` and `worker_fallback_model` configure the **default** worker
only. When `worker_hook` is set the wrapper runs that hook verbatim and both
keys are ignored, so a hook pins its own model, in the hook. For the default
worker, `worker_fallback_model` is used only after a clean capacity failure that
left the worktree unchanged; a timeout retries on the primary model.

### Choosing per aspect

Measured across real issues, wall clock splits roughly as:

- review passes: **75-84%** (of which the two engines split about 2:1)
- worker: **10-17%**
- validation: **9-12%**

So reviewer choice dominates _cost_, while worker choice dominates _how many
rounds are needed_ — round one consistently produces the most findings, and a
cleaner first draft is what removes a round. A round costs far more than a
worker pass, so the cheapest slot is usually the one worth upgrading.

Leaving `worker_model` empty is not a neutral default: the default worker then
runs on whatever the CLI currently defaults to, which moves with CLI releases.
Pin it.

### Current limitation: the engine roster is fixed

Both `claude_review_hook` and `codex_review_hook` are **required**, and the
roster and order are hardcoded as Codex then Claude. There is no key for a third
engine and no supported way to omit one.

What preflight enforces is that both hook strings are non-empty and carry the
contract tokens; it never checks that a reviewer CLI is installed. So a run
missing one CLI starts, claims the issue, completes the worker pass and
validation, pushes, opens the draft PR, and only then fails at that engine's
leg — leaving a claimed issue and an abandoned draft behind.

This is a wrapper limitation rather than a contract one: `review-ledger.js`
already treats `gemini` and `antigravity` as first-class engine identities, and
`run-agy-review.sh` already accepts a wrapper-supplied per-pass bound. A hook
that substitutes one engine's CLI for another's would record the pass under the
wrong engine identity and corrupt the ledger's roster, so it is not a workaround.

## Deterministic Phase Order

1. Select and dependency-gate an eligible issue.
2. Claim it, detecting assignment races.
3. Create a unique worktree and branch from `origin/<base>`.
4. Run the isolated setup hook.
5. Run the worker and require a clean local commit.
6. Fetch and merge the base, inspect the diff, validate, push, and open a draft PR.
7. Run a Codex pass and then a Claude pass against the PR ledger. Each hook
   comments before fixes, publishes committed fixes only through the wrapper-owned
   safe-push helper, posts structured fix and final-lane
   completion evidence, then resolves.
8. If either engine made material fixes, restart from Codex. Stop after
   `review_max_rounds` or the persisted whole-run deadline and preserve the draft.
9. Re-attest the exact issue contract and dependencies, excluding only the
   wrapper-captured PR from the addressed-by-open-PR check. Require a complete
   clean round plus replies and resolutions on every marked thread, then mark
   the PR ready.

Do not invoke `reviewit`, Copilot, or any GitHub-hosted AI reviewer, including
hosted Gemini. This bans _hosted_ review, not the local `gemini` engine identity
that the ledger and `run-agy-review.sh` already support; the wrapper simply has
no roster slot to run it from today.

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
branch. Contract-v3 allowlist batches persist their ordered issues, cursor,
per-issue statuses, and child run-state paths. Recovery advances only after the
current issue is safely finalized or explicitly bailed; uncertain push, PR, or
ledger mutation stops the batch.

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
  Old hooks that compose their own pushes will fail closed because each fix must
  use `$AGENT_LOOP_REVIEW_PUSH_HELPER`, which owns the exact fully qualified
  draft-PR destination and rejects force, ambiguity, stale heads, and the wrong
  branch. Every clean or changed v3 hook must call the ledger helper's
  `write-result` command; a blocked hook must call `write-blocked-result`. Hooks
  must not post pass/completion attestations because the wrapper validates the
  result and owns those markers.

## Test Guidance

Use focused commands and bounded output. For Vitest 4, target a test with:

```bash
pnpm --filter frontend test:run TestName
```

Do not insert `--` before `TestName`; that can run the full suite.

## Source of Truth

This directory is upstream-owned and synced to consumers. Change reusable
mechanics here, not in a consumer's synced copy.
