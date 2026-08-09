---
name: agent-loop
description: Autonomous issue implementation loop with strict issue allowlisting, one linked worktree per issue, draft-PR-first local Codex and Claude review hooks, inline finding traceability, bounded convergence, and fresh-base validation. Use when Codex should implement a bounded GitHub issue queue without hosted AI reviewers.
---

# Agent Loop

Run isolated issue workers and open one draft pull request per issue before
local review starts. The wrapper owns selection, claiming, worktrees, base
integration, initial publication, review-head attestation, and readiness. A
worker only implements, validates, refactors, and commits locally.

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

The config, instructions, and prompt are bootstrapped with
`create_if_missing: true`; merge template changes manually into existing
consumers. `ready.py` is upstream-owned and overwritten by sync.

## Config Interface

The config is parsed as literal `key = value` lines and is never sourced.
Unknown or duplicate keys fail closed. Hook values are shell commands executed
with the issue worktree as the current directory.

| Key                                              | Purpose                                                                                                                                                         |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `base_branch`                                    | Integration branch; env `AGENT_LOOP_BASE_BRANCH` overrides it.                                                                                                  |
| `setup_hook`                                     | Isolated bootstrap, such as `pnpm install --frozen-lockfile`. It must not change HEAD or leave Git-visible worktree changes.                                    |
| `validation_hook`                                | Required non-mutating validation after the worker, every review pass, and fresh-base integration.                                                               |
| `claude_review_hook`                             | Required fresh local Claude review on the draft PR. It must post confirmed findings inline before fixes, push, reply, resolve, and fail on undisposed findings. |
| `codex_review_hook`                              | Required fresh Codex `deepgrill` on the draft PR against `$AGENT_LOOP_REVIEW_BASE_SHA`, with the same thread contract.                                          |
| `review_contract_version`                        | Required opt-in to the current hook contract. Set to `2`; missing or unsupported versions fail before issue selection.                                          |
| `review_max_rounds`                              | Positive cap on Codex-then-Claude rounds. Default `4`; cap exhaustion preserves the worktree and blocks publication.                                            |
| `worker_hook`                                    | Optional worker command override. Default is `codex exec`.                                                                                                      |
| `worker_model`, `worker_fallback_model`          | Primary and capacity-fallback models for the default worker.                                                                                                    |
| `worker_retries`                                 | Retries after clean capacity/timeout failures. Default `1`.                                                                                                     |
| `worker_timeout_seconds`, `hook_timeout_seconds` | Positive bounded execution time; zero is rejected because GNU `timeout 0` disables the bound.                                                                   |
| `retry_on_timeout`, `retry_delay_seconds`        | Timeout retry policy.                                                                                                                                           |
| `dependency_gate`                                | `ready` (legacy) or `merged-to-base`.                                                                                                                           |
| `branch_prefix`, `worktree_root`, `log_root`     | Isolated path/ref controls.                                                                                                                                     |
| `log_max_kb`, `output_max_lines`                 | Bound captured logs and displayed failure tails.                                                                                                                |

Hooks receive `AGENT_LOOP_ISSUE_ID`, `AGENT_LOOP_ISSUE_TITLE`,
`AGENT_LOOP_ISSUE_BODY`, `AGENT_LOOP_BASE_BRANCH`, `AGENT_LOOP_BRANCH`,
`AGENT_LOOP_WORKTREE`, `AGENT_LOOP_LOG_DIR`, and `AGENT_LOOP_PROMPT`. Because
ordinary `gh` commands are masked inside hooks, the worker reads its issue from
`AGENT_LOOP_ISSUE_TITLE` and `AGENT_LOOP_ISSUE_BODY` rather than the API. Both
are byte-exact copies of the issue text, including trailing whitespace; the
pre-publication re-attestation compares them against the live issue, so a hook
that rewrites them in place will fail that check. Review
hooks also receive `AGENT_LOOP_PR_NUMBER`, `AGENT_LOOP_PR_URL`,
`AGENT_LOOP_PR_HEAD_SHA`, `AGENT_LOOP_REVIEW_BASE`, the fully qualified fetched remote
ref, the immutable `AGENT_LOOP_REVIEW_BASE_SHA` captured after the round's fresh
fetch, `AGENT_LOOP_REVIEW_ROUND`, `AGENT_LOOP_REVIEW_ENGINE`, and
`AGENT_LOOP_REVIEW_OUTCOME_FILE`. Both hooks must scope against the SHA so a
mid-round remote update cannot give the engines different bases.

When a review hook commits fixes, it writes exactly `material` or `minor` to
`$AGENT_LOOP_REVIEW_OUTCOME_FILE`. Material means any substantive behavior,
correctness, security/privacy, data-safety, compatibility, deployment/sync, or
review-integrity change. Minor means low-risk, non-behavioral cleanup, clarity,
or test/docs polish. If a hook commits without writing the file, the wrapper
defaults to `material`; a no-commit pass is clean and must not write an outcome.
Only material fixes restart at Codex. Minor fixes are retained and validated but
count toward convergence so low-severity polish cannot keep the loop running.
The outcome must remain unchanged through post-review validation; a validator
or later reviewer that creates, removes, or changes an accepted classification
blocks publication. The wrapper re-attests both records after final validation.

The wrapper pins `GH_REPO` from the current checkout before any
repository-scoped GitHub operation. Setup, worker, and validation hooks cannot
push or call ordinary `gh`; review hooks run only after draft PR creation and
may mutate that PR and its head branch. After each review hook, the wrapper
requires exit 0, a clean attached issue branch, append-only ancestry, unchanged
origin identity, and identical local, remote, and PR head SHAs. At convergence
it queries GitHub review threads and fails if any thread bearing the
`local-review:v1` marker lacks a reply or remains unresolved.

## Existing Consumer Migration

The wrapper is upstream-owned, but config, worker instructions, and the prompt
are `create_if_missing` consumer files. Existing consumers must therefore merge
the current templates manually before the synced wrapper can run:

1. Update the Codex hook to run a fresh `deepgrill
$AGENT_LOOP_PR_NUMBER`, then the Claude hook to run a fresh adversarial review
   on the same PR. Scope both to `$AGENT_LOOP_REVIEW_BASE_SHA`.
2. Make both hooks load `.codex/references/local-review-ledger.md`, read all
   prior threads, post confirmed findings inline before editing, commit and push
   fixes, reply with the fix and validation, resolve the threads, leave the
   issue branch attached and clean, and exit nonzero if findings remain.
3. Configure a non-mutating `validation_hook`, add
   `review_contract_version = 2`, and optionally override
   `review_max_rounds = 4` with another positive cap.
4. Merge the current local-only wording from the instruction and prompt
   templates, including the local bail-record/operator-handoff contract. Sync
   will not overwrite those consumer-owned files.

For a non-mutating consumer smoke test from an upstream development worktree,
set `AGENT_LOOP_PROJECT_DIR=/path/to/consumer` and pass `--dry-run`. Do not use
that override for a mutating run; execute the consumer's synced script instead.

Do not put secrets, credentials, PHI, customer identifiers, or user data in
config values or hook output. The wrapper deliberately uses a generic PR body
and never copies issue bodies, model logs, or findings into GitHub.

## Deterministic Phase Order

1. Select and dependency-gate an eligible issue.
2. Claim it, refetch title/body/eligibility, rerun ready/dependency gates, and
   detect assignment races. If eligibility changes, roll back only the
   assignment added by this run; a rollback failure stops before worktree
   creation for operator recovery.
3. Create a unique worktree and branch from `origin/<base>`.
4. Run the isolated setup hook.
5. Run the worker and require a clean local commit.
6. Integrate the fresh base, validate, push, and open a draft PR.
7. Run a fresh Codex `deepgrill` followed by a fresh Claude review on that PR,
   validating and attesting the PR head after each pass.
8. If either reviewer commits a material fix, restart at Codex. Minor-only fixes
   are validated and retained without restarting. Convergence requires one
   entire Codex-then-Claude round with no material fixes. Exhausting
   `review_max_rounds` blocks publication and preserves the worktree.
9. If the base advances, integrate and push it on the draft PR before restarting
   at Codex. A non-fast-forward base move stops the loop.
10. Re-attest unchanged issue requirements/readiness, require every marked
    review thread to contain a reply and be resolved, then mark the PR ready.

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
commands. A worker that bails writes the consumer-defined local classification
record there; the operator applies any required GitHub labels/comments after
the guarded run exits. Retry capacity/timeouts only when the worktree is
unchanged. Review, setup, integration, validation mutation,
detached/wrong-branch state, review-thread failures, and review
non-convergence failures also preserve the worktree and draft PR. Never reset,
reuse, clean, or delete a dirty recovery worktree.

If a newly added claim cannot be rolled back before worktree creation, stop and
manually inspect/unassign it. Publication is not atomic: after a push succeeds,
an attestation, upstream-setting, or PR-creation failure can leave the captured
SHA on the remote issue branch, and a failed post-create attestation can leave
an open PR when automatic close also fails. Preserve the worktree, inspect both
remote branch and PR state, and retry or clean up only after explicit SHA
verification; a blind rerun will reject the existing remote branch.

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
