---
name: agent-loop
description: Autonomous issue implementation loop with strict issue allowlisting, one linked worktree per issue, draft-PR-first local Codex and Claude review hooks, inline finding traceability, bounded convergence, and fresh-base validation. Use when Codex should implement a bounded GitHub issue queue without hosted AI reviewers.
---

# Agent Loop

Run isolated issue workers and open one draft pull request per issue before
local review starts. The wrapper owns selection, claiming, worktrees, base
integration, initial publication, review-head attestation, and readiness. A
worker only implements, validates, refactors, and commits locally.

## Cross-engine mode

Resolve `auto` or `handoff` from the user's request or repository instructions
before a mutating run. Contract-v4 auto mode requires `config_doctor = true` and
`claude_effort_policy = low`; the pinned launcher owns that policy and the
doctor verifies its contract query. Never substitute another effort or bypass
the doctor. Handoff mode must stop after
the Codex leg, post `local-review-handoff:v1`, and return control so the user can
start the Claude review in a new terminal. Do not silently change modes during
a round. Until the wrapper implements that pause point, refuse an `agent-loop`
handoff-mode mutating run and direct the user to the interactive review skills;
do not fall back to its automatic hook.

## Usage

```bash
.codex/skills/agent-loop/scripts/agent-loop.sh \
  --issues 5105,5106 --iterations 2

.codex/skills/agent-loop/scripts/agent-loop.sh \
  --issues 5105,5106 --dry-run
```

Options:

| Option                | Behavior                                                                                                                                                                      |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--issues N,N,...`    | Restrict selection to exactly these issue numbers. Never fall through to unrelated ready work.                                                                                |
| `--iterations N`      | Process at most `N` issues. A legacy numeric first argument remains accepted.                                                                                                 |
| `--include-assigned`  | Include an eligible issue assigned only to the current user. The deprecated `--resume` spelling remains an alias.                                                             |
| `--resume-run FILE`   | Resume review/finalization from a private contract-v3/v4 run-state file after re-attesting its issue, worktree, branch, PR, base, and head.                                   |
| `--resume-batch FILE` | Resume an ordered contract-v3/v4 allowlist from its private batch-state file. It cannot be combined with `--resume-run`, `--issues`, or `--dry-run`.                          |
| `--dry-run`           | Show selections, dependency decisions, worktree/branch paths, hooks, and publication without claiming, fetching, creating worktrees, running hooks, pushing, or creating PRs. |

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
- `.claude/skills/deepcritique/SKILL.md`: separately managed Claude-native
  review guidance. Codex platform sync does not install or overwrite it.

The config, instructions, and prompt are bootstrapped with
`create_if_missing: true`; merge template changes manually into existing
consumers. `ready.py` is upstream-owned and overwritten by sync.

## Config Interface

The config is parsed as literal `key = value` lines and is never sourced.
Unknown or duplicate keys fail closed. Hook values are shell commands executed
with the issue worktree as the current directory.

| Key                                              | Purpose                                                                                                                      |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `base_branch`                                    | Integration branch; env `AGENT_LOOP_BASE_BRANCH` overrides it.                                                               |
| `setup_hook`                                     | Isolated bootstrap, such as `pnpm install --frozen-lockfile`. It must not change HEAD or leave Git-visible worktree changes. |
| `validation_hook`                                | Required non-mutating validation after the worker, every review pass, and fresh-base integration.                            |
| `claude_review_hook`                             | Pinned trusted-launcher invocation for the fresh local Claude review. Consumer overrides are rejected.                       |
| `codex_review_hook`                              | Pinned trusted-launcher invocation for the fresh local Codex review. Consumer overrides are rejected.                        |
| `review_contract_version`                        | Required hook contract. New and migrated consumers use `4`; versions `2` and `3` remain accepted for staged migration.       |
| `config_doctor`                                  | Run the non-mutating consumer compatibility doctor before selection or claim. Contract-v3/v4 consumers set `true`.           |
| `claude_effort_policy`                           | Auto mode requires `low`; v3 verifies the hook and v4 verifies the pinned launcher's contract.                               |
| `review_max_rounds`                              | Positive cap on Codex-then-Claude rounds. Default `4`; cap exhaustion preserves the worktree and blocks publication.         |
| `worker_hook`                                    | Optional worker command override. Default is `codex exec`.                                                                   |
| `worker_model`, `worker_fallback_model`          | Primary and capacity-fallback models for the default worker.                                                                 |
| `worker_retries`                                 | Retries after clean capacity/timeout failures. Default `1`.                                                                  |
| `worker_timeout_seconds`, `hook_timeout_seconds` | Positive bounded execution time; zero is rejected because GNU `timeout 0` disables the bound.                                |
| `retry_on_timeout`, `retry_delay_seconds`        | Timeout retry policy.                                                                                                        |
| `dependency_gate`                                | `ready` (legacy) or `merged-to-base`.                                                                                        |
| `branch_prefix`, `worktree_root`, `log_root`     | Isolated path/ref controls.                                                                                                  |
| `log_max_kb`, `output_max_lines`                 | Bound captured logs and displayed failure tails.                                                                             |

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
`AGENT_LOOP_REVIEW_RESULT_FILE` and `AGENT_LOOP_REVIEW_PUSH_HELPER` for
contract v3 or v4. Both hooks must scope against the SHA so a
mid-round remote update cannot give the engines different bases.

Contract v4 resolves both reviewers through the synced
`.codex/skills/agent-loop/scripts/run-codex-review.sh` launcher in the source
checkout. Immediately before execution the wrapper byte-compares the launcher
with its immutable base-commit blob and invokes it directly without a login
shell or `eval`. The launcher materializes `.codex` for Codex, `.claude` for
Claude, and applicable `AGENTS.md`/`CLAUDE.md` files from base-tree blobs into a
fresh empty session root. It passes absolute paths to that engine-native guidance and
disables Claude's slash-command and Skill-tool loading, project/local settings,
added-directory instructions, and auto memory. The launcher therefore names an
absolute path into the pinned snapshot rather than a skill name it would have
to resolve, so neither engine resolves review instructions from
worker-authored files in the issue worktree. The wrapper pins both hook
strings byte-for-byte; an existing consumer must migrate them before
contract-v4 auto mode will run. Contract v3 retains its configurable hook
semantics for staged migration.

The wrapper treats the selected Codex and Claude reviewer processes as trusted
participants. Its command guards and review-push helper constrain cooperative
review execution; they are not credential or same-UID isolation. In particular,
they do not defend against a reviewer deliberately rewriting its own tools or a
hook delegating work to an unrelated pre-existing user service. That stronger
boundary requires the credential-isolation redesign tracked in platform issue
#37. The Linux subreaper does fail closed and removes ordinary hook descendants,
including detached and double-forked children, before the next phase starts.

Every successfully completed clean or changed contract-v3/v4 review hook calls
`review-ledger.js write-result`; the helper derives the complete
same-engine/same-round fingerprint set, including fixed, deferred, and
dismissed dispositions, and atomically writes the structured result. A blocked
hook instead uses `review-ledger.js write-blocked-result` with an owner-only
blocker file and
must not claim a clean or changed pass. The wrapper validates the engine, round, pinned base, observed
before/after SHAs, classification, finding fingerprints, and final-lane status;
verifies changed fingerprints against resolved v3 dispositions; and posts the
canonical pass/completion marker itself. Material means any substantive
behavior, correctness, security/privacy, data-safety, compatibility,
deployment/sync, or review-integrity change; it is not inferred from file type.
Minor means low-risk, non-behavioral cleanup, clarity, or test/docs polish. Only
material fixes restart at Codex. A missing, invalid, or blocked result stops
clearly even if the hook process exits zero. Accepted result bytes remain
unchanged through final validation.

The wrapper pins `GH_REPO` from the current checkout before any
repository-scoped GitHub operation. Setup, worker, and validation hooks cannot
push or call ordinary `gh`; review hooks run only after draft PR creation and
are instructed to mutate that PR and publish a committed fix through
`$AGENT_LOOP_REVIEW_PUSH_HELPER`. The helper owns the exact fully qualified
destination and rejects arguments, force, stale remote heads, rewritten
history, and the wrong branch. After each review hook, the wrapper
requires exit 0, a clean attached issue branch, append-only ancestry, unchanged
origin identity, and identical local, remote, and PR head SHAs. At convergence
it queries GitHub review threads and fails if any local-review thread lacks a
disposition reply or remains unresolved.

## Existing Consumer Migration

The wrapper is upstream-owned, but config, worker instructions, and the prompt
are `create_if_missing` consumer files. Existing consumers must therefore merge
the current templates manually before the synced wrapper can run:

1. Set `review_contract_version = 4` and both hooks to the dedicated trusted launcher exactly as shown in the
   current config template. Do not wrap, extend, or replace those commands; the
   launcher selects the engine, scopes the review to the immutable base and
   exact head, and owns the contract-v4 result path.
2. Configure a non-mutating `validation_hook`, retain `config_doctor = true` and
   `claude_effort_policy = low`, and optionally override
   `review_max_rounds = 4` with another positive cap. The launcher and reviewer
   skill own the result, push, and blocked-result contracts. Before issue
   selection or claim, the doctor requires the fetched base to contain both
   Codex and Claude native `deepcritique` skills as regular blobs.
3. Merge the current local-only wording from the instruction and prompt
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
7. Run a fresh Codex `deepcritique` followed by a fresh Claude review on that PR,
   validating and attesting the PR head after each pass.
8. If either reviewer commits a material fix, restart at Codex. Minor-only fixes
   are validated and retained without restarting. Convergence requires one
   entire Codex-then-Claude round with no material fixes. Exhausting
   `review_max_rounds` blocks publication and preserves the worktree.
9. If the base advances, integrate and push it on the draft PR before restarting
   at Codex. A non-fast-forward base move stops the loop.
10. Re-attest unchanged issue requirements/readiness while excluding only the
    wrapper-captured PR from the open-PR addressed check, require every marked
    review thread to contain a reply and be resolved, then mark the PR ready.

For an ordered contract-v3/v4 allowlist, the wrapper also writes a private batch
checkpoint containing the allowlist, cursor, per-issue status, and child run
state path. Resume with `agent-loop.sh --resume-batch <batch-state.json>`. It
will not advance to the next issue until the current issue is safely finalized
or an operator explicitly records it as bailed; uncertain push, PR, or ledger
state always stops recovery.

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

Contract v3 creates an owner-only atomic `run-state.json` after draft PR
publication and checkpoints every review round plus convergence. On a review or
finalization interruption, use the exact `--resume-run <state-file>` command
printed by the wrapper. Recovery re-attests repository identity, issue
assignment and original requirement digests, worktree ancestry, branch, PR
identity, base, and head before it continues. The original run and every resume
hold one exclusive run-scoped lock through finalization; recovery never re-runs
the worker or blindly recreates a remote branch.

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
