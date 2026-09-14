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
| `config_doctor`                                  | Run the non-mutating compatibility preflight before issue selection or claim, including that each review hook's CLI resolves on `PATH`.                                                                      |
| `claude_effort_policy`                           | Optional literal Claude effort policy enforced by the doctor.                                                                                                                                                |
| `review_max_rounds`                              | Codex→Claude round cap from `1` through the hard ceiling `4`. Default `4`; exhaustion preserves the draft PR.                                                                                                |
| `review_timeout_seconds`                         | Positive wall-clock budget for one issue's review, persisted across resume. Default `7200`; each review pass and its validation is capped at the smaller of the remaining budget and `hook_timeout_seconds`. |
| `claude_review_hook`                             | Required local Claude PR review. Reads the ledger, comments before fixes, publishes through `$AGENT_LOOP_REVIEW_PUSH_HELPER`, replies, and resolves.                                                         |
| `codex_review_hook`                              | Required local Codex PR review with the same ledger contract.                                                                                                                                                |
| `worker_hook`                                    | Optional worker command override. Default is the Claude CLI in headless, auto-approving mode.                                                                                                                |
| `worker_model`, `worker_fallback_model`          | Primary and capacity-fallback models for the default worker.                                                                                                                                                 |
| `worker_effort`                                  | `--effort` for the default worker. Empty means the CLI or environment default (the doctor warns); when `claude_effort_policy` is set the two must match.                                                     |
| `worker_retries`                                 | Retries after clean capacity/timeout failures. Default `1`.                                                                                                                                                  |
| `worker_timeout_seconds`, `hook_timeout_seconds` | Bounded execution time.                                                                                                                                                                                      |
| `retry_on_timeout`, `retry_delay_seconds`        | Timeout retry policy.                                                                                                                                                                                        |
| `dependency_gate`                                | `ready` (legacy), `merged-to-base`, or `batch-stack`.                                                                                                                                                        |
| `batch_on_issue_failure`                         | `stop` (default) or `park`. See "Parking a failed batch issue".                                                                                                                                              |
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
stops even when the hook exits zero, with one exception: a hook that exits zero
without writing any result, and left no commit, push, ledger thread, or PR
comment behind, is retried once in the same round (`retry:
hook-ended-without-result`). The retry draws on the same review budget. A
second empty pass, or an empty pass that changed anything, stops with the
category `no-result/hook-ended-early` and the tail of the hook log. Validation hooks
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

## Review Budget

`review_timeout_seconds` is a whole-run budget for **one issue's** review. It is
reset the moment that issue's draft PR opens and persisted to run state as
`reviewDeadlineEpoch`, so `--resume-run` and `--resume-batch` continue the
original clock rather than restarting it.

Every review pass **and the validation that follows it** draws from that budget.
A round is therefore two hook invocations plus two validations, and fresh-base
integration is budgeted too.

Each pass is bounded at `min(remaining budget, hook_timeout_seconds)`. Two
consequences are easy to miss:

- **`review_max_rounds` is a ceiling, not an allowance.** The budget has to fund
  every round. If a round costs more than `review_timeout_seconds / review_max_rounds`,
  the later rounds are unreachable no matter what the cap says. When
  `hook_timeout_seconds` equals `review_timeout_seconds`, a single pass may also
  legally consume the entire budget. Size the budget against an observed round
  cost, and keep `hook_timeout_seconds` well below it so one pass cannot spend
  everything.
- **Running out mid-pass does not look like running out.** The clean
  "exhausted its configured whole-run time budget" stop only fires when the
  budget is under `REVIEW_PASS_MIN_SECONDS` (120s) _at the start_ of a pass. A
  pass that starts with more than that and then hits its bound is killed by
  `timeout`, writes no result, and is reported as a hook failure. The wrapper
  logs the remaining budget and the applied bound before each pass so the two
  can be told apart.

### Validation is not repeated on an unchanged head

The validation hook is treated as a function of the head and of the base it is
diffed against. Once a `(head, base)` pair has passed, the wrapper does not run
the hook on it again: the initial base integration that reports "Already up to
date", a clean pass that committed nothing, and a resumed leg at an already
validated head all print `validation skipped` instead of spending budget. The
exception is the final reviewed-head gate, which always runs on the exact head
that is marked ready — a hook that damaged the worktree environment without
committing is still caught there. A changed pass, a real base integration, or a
base that moved since the last validation runs the hook as before.

### The validation hook is the gating run

Under agent-loop, the wrapper's validation hook is the gating suite for every
review pass: it runs on the exact head after each pass, and a red run stops the
pass before convergence. Review hooks therefore need only focused checks for
their own fixes. Each run, skip, or failure appends a line to
`validation.jsonl` in the run's log directory with the label, head, base,
outcome (`passed`, `failed`, or `reused` with the label it reused), and the
SHA-256 of the configured `validation_hook`. The ready PR body names the
reviewed head and base the final gate passed on.

### Launcher headroom

Hooks that shell out to a _launcher_ which enforces its own bound read that
bound from `LOCAL_REVIEW_PASS_TIMEOUT_SECONDS`. It is set to
`REVIEW_PASS_TIMEOUT_SECONDS - REVIEW_PASS_LAUNCHER_MARGIN_SECONDS` (60s of
headroom) and then **clamped to 3600**, because the launchers that consume it
reject anything higher. The margin exists so the launcher's own clock expires
first and it can still write a structured result instead of being killed.

That protection applies only to hooks that actually read the variable. A hook
that invokes a CLI directly — the common case — ignores it, so its only bound is
the wrapper's `timeout`, and hitting that bound kills it with no result file.
Consumers wanting a structured result on timeout must have the hook honor
`LOCAL_REVIEW_PASS_TIMEOUT_SECONDS` itself.

## Default Worker and the Invocation Lock

When `worker_hook` is unset, the wrapper runs the Claude CLI in
`--permission-mode bypassPermissions --print` mode against the issue prompt,
adding `--model` from `worker_model` and `--effort` from `worker_effort` when
they are set.
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

| Aspect         | Where the model is chosen                | Effort control                                                          |
| -------------- | ---------------------------------------- | ----------------------------------------------------------------------- |
| Default worker | `worker_model` / `worker_fallback_model` | `worker_effort`, validated against `claude_effort_policy` when both set |
| Codex review   | inside `codex_review_hook`               | inside the same command                                                 |
| Claude review  | inside `claude_review_hook`              | inside the same command, and validated against `claude_effort_policy`   |

A review hook is a literal shell command, so reviewer model and effort are
ordinary flags on that command rather than dedicated config keys. These
fragments show flag placement only; a working hook must also carry
`AGENT_LOOP_REVIEW_PUSH_HELPER`, `AGENT_LOOP_REVIEW_RESULT_FILE`, and
`write-result`, or contract-v3 preflight rejects it:

```
claude_review_hook = claude --print --effort low --model <model-id> /deepcritique ... </dev/null
codex_review_hook  = codex exec -c model_reasoning_effort=medium ... /deepcritique ... </dev/null
```

Every hook and the default worker run with stdin redirected from `/dev/null`;
the wrapper does this itself, so the trailing `</dev/null` above is belt and
braces for a hook string that is also run by hand. It matters because
`codex exec` reads stdin to EOF when it is not a TTY: a hook that inherits an
open pipe from whatever launched the wrapper produces no output and no result
until the pass times out, which is indistinguishable from a slow review from
outside. A hook that genuinely needs input must supply it inside the command.

The wrapper also exports `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` for every hook
and the default worker, overriding an inherited value. A one-shot Claude CLI
that moves a long command, such as a test suite, to the background can end its
turn with that command still running: it exits 0 and writes no result. The
doctor warns when a hook sets the variable to anything else or unsets it.

`claude_effort_policy` constrains `claude_review_hook` and, when the default
worker is in use, `worker_effort` — both only when `config_doctor = true`, since
the doctor is what enforces it and the key is inert without it. An empty
`worker_effort` is not neutral either: the worker then runs at whatever the CLI
or the launching environment defaults to, and nothing records which.

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

Preflight enforces that both hook strings are non-empty and carry the contract
tokens, and the config doctor resolves each hook's first command word on `PATH`
before selection or claim, so a run missing a reviewer CLI stops at startup
instead of claiming the issue, opening the draft PR, and failing at that
engine's leg. A hook whose first word is shell syntax (`if`, `:`, a variable)
is not resolved statically; the doctor also warns when `codex exec` appears
without `</dev/null`.

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
6. Fetch and merge the base, inspect the diff, validate, push, and open a draft PR
   titled with the worker's first commit subject (so a merge-commit consumer gets
   a conventional merge subject), falling back to `agent-loop: resolve #N`.
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

`dependency_gate = batch-stack` is for ordered batches, where order alone is
only a timing constraint. An issue that declares `Depends on #A` on an earlier
batch entry that is finalized but not merged is built on that entry: its
worktree starts at #A's reviewed head, its draft PR targets #A's branch, and
review and the publication diff cover only its own commits. The batch records
the stack (`stackedOn`), and the ready PR names the branch it is stacked on.
Retargeting the PR to the base after #A merges is left to the operator; the
wrapper prints the command. A stack has one parent: a dependency on two
unmerged batch entries, or on a later entry, stops. A dependency that is
parked or bailed in the batch is never built from the base instead; the issue
is parked as `blocked-by-dependency` under `batch_on_issue_failure = park`, and
stops the batch otherwise. A dependency outside the batch follows
`merged-to-base`.

Every batch run warns at creation when an issue's body mentions an earlier
batch issue without declaring `Depends on`, since that issue starts from the
base however the prose reads.

## Failure and Recovery

A worker bails by writing its classification and operator handoff to
`$AGENT_LOOP_HANDOFF_FILE` (`operator-handoff.md` in the run's log directory)
and making no commit. The file is authoritative, whatever the exit status: the
wrapper prints the `agent-bail:` classification and the handoff path, releases a
claim it added, removes the unused worktree and branch, records a batch entry as
`bailed` with that classification, and continues with the next issue. It makes
no label or comment changes; those stay operator actions from the handoff. A
handoff alongside changed or committed work is an ambiguous bail and stops.

On any other non-zero worker exit, inspect whether the worktree is dirty or contains
new commits. Preserve all changed or committed work and stop with recovery
commands. Retry capacity/timeouts only when the worktree is unchanged. Review,
setup, integration, and validation failures also preserve the worktree. Never
reset, reuse, clean, or delete a dirty recovery worktree.

Successful publication removes the clean linked worktree but retains the local
branch. Contract-v3 allowlist batches persist their ordered issues, cursor,
per-issue statuses, and child run-state paths. Recovery advances only after the
current issue is safely finalized or explicitly bailed; uncertain push, PR, or
ledger mutation stops the batch.

A pass can be killed after it commits a fix but before its push lands. The
worktree is then ahead of a remote branch and PR head that still sit at the
checkpoint. Resume recovers that shape instead of refusing, but only when all
of these hold: the phase is `reviewing`, the worktree is clean and on the issue
branch, and the local head descends from the checkpoint. No review thread or PR
comment may mention a stranded commit. The wrapper keeps the stranded commits
on `refs/agent-loop/rescue/<run-id>/<engine>-r<round>`, which it never deletes
and which outlives the worktree. It resets to the checkpoint and replays the
interrupted pass under the same-round rules below, then lists the ref when the
run completes. Any other divergence still stops, such as a remote ahead of the
checkpoint or ledger evidence for a stranded commit.

Resuming a run interrupted in the Claude leg of any round, with that round's
Codex result on disk and the head unchanged, re-verifies the Codex evidence and
runs only the Claude leg of the same round; it does not consume a round. If the
base advanced since the checkpoint, the integrated head is no longer the head
Codex reviewed, so the resumed round starts again at Codex.

### Parking a failed batch issue

With `batch_on_issue_failure = park`, an ordered batch does not halt on an
issue that failed in a state `--resume-run` can pick up. The wrapper parks the
issue and continues with the next one. It decides from what it can observe,
never from hook output. Every one of these must hold:

- the stop category is `no-result/hook-ended-early`, `validation-red`,
  `hook-timeout`, `review-cap-exhausted`, or `budget-exhausted`;
- the issue has a valid review checkpoint in the `reviewing` or `converged`
  phase;
- the worktree is clean and on the issue branch;
- the local head, the remote branch, and the open draft PR head are equal;
- a head that moved past the checkpoint is explained by that pass's result
  (its before and after SHAs);
- the pass's push checkpoint matches the remote head.

Anything else, including any doubt about a push, the PR, or the ledger, still
stops the batch.

A parked entry records its stop category and keeps its worktree. A later issue
that declares `Depends on #N` on a parked entry is parked as
`blocked-by-parked` without being claimed. When the batch reaches its end with
parked entries, the wrapper lists each one with its `--resume-run` command and
the `batch-update` that closes it out, then exits `3`.

## Liveness and Timing

The run's log directory carries two files for anything watching from outside:

- `wrapper.pid` — the wrapper's PID, written when the directory is created and
  again on `--resume-run`. Test it with `kill -0`, never with `pgrep -f`: a
  `pgrep -f` pattern also matches the shell running the monitor, so a
  "wrapper gone" check built on it can never fire.
- `phases.jsonl` — one JSON line per phase event: `start` and `end` for every
  bounded hook and validation (epoch, duration in seconds, exit status), and
  `skipped` for a validation reused on an unchanged head. Phase durations no
  longer have to be reconstructed from log file mtimes.

### Event stream

Structured events are the supported way to supervise a run. The glyph lines on
the console are for people and may change. Each event is one JSON object per
line, appended with `fsync`, carrying `event`, `epoch`, and `runTag`:

- an ordered batch writes `<batch-state>-events.jsonl` beside its batch state
  file, including the events of every child run it resumes;
- any other run writes `<log_root>/<repo>-run-<run-tag>-events.jsonl`;
- a standalone `--resume-run` appends to `events.jsonl` in the run's log
  directory.

| Event                                       | Fields                                                                                |
| ------------------------------------------- | ------------------------------------------------------------------------------------- |
| `batch_start`                               | `issues`, `configSha256`, `resumed`                                                   |
| `batch_end`                                 | `exit`, `finalized`, `bailed`, `parked`                                               |
| `issue_start`                               | `issue`, `index` or `round`, `resumed`, `runState`                                    |
| `phase_start`, `phase_end`, `phase_skipped` | `issue`, `phase`, and on `phase_end` `seconds` and `exit`                             |
| `pass_result`                               | `issue`, `round`, `engine`, `status`, `classification`, `before`, `after`             |
| `retry`                                     | `issue`, `round`, `engine`, `reason`                                                  |
| `stop`                                      | `issue`, `category`, `message`, `resumable`, `resumeCommand`, `hookPhase`, `hookTail` |
| `parked`                                    | `issue`, `category`, `resumeCommand`                                                  |
| `bail`                                      | `issue`, `classification`, `handoffPath`                                              |
| `recovered`                                 | `issue`, `kind`, `ref`                                                                |
| `pr_ready`                                  | `issue`, `pr`, `head`                                                                 |

Every stop names a category from a fixed list: `no-result/hook-ended-early`,
`invalid-result`, `review-blocked`, `ledger-evidence`,
`push-checkpoint-mismatch`, `heads-misaligned`, `worktree-state`,
`validation-red`, `hook-failed`, `hook-timeout`, `budget-exhausted`,
`review-cap-exhausted`, `worker-ambiguous-bail`, `worker-no-commit`,
`worker-failed`, `setup-failed`, `merge-conflict`, `publication-diff`,
`base-diverged`, `dependency-blocked`, `issue-changed`, `checkpoint-failed`,
`uncertain-mutation`, `child-resume-failed`, `batch-incomplete`,
`interrupted`, or `internal-error`. A stop that follows a hook carries that
hook's phase and last output. The output is bounded like the console tail, to
`output_max_lines` lines of at most 400 characters with control characters
removed. Events never copy issue titles, bodies, or findings. A resumed run
prints `▶ Issue #N (resumed, round R)`.

One `jq` pass turns a batch stream into a per-issue table:

```bash
jq -rs '[.[] | select(.issue != null)] | group_by(.issue)[] as $e
  | ($e | map(.event)) as $t
  | [($e[0].issue | tostring),
     (if ($t | index("pr_ready")) then "finalized" elif ($t | index("bail")) then "bailed"
      elif ($t | index("parked")) then "parked" else "stopped" end),
     ([$e[] | select(.event == "pass_result") | .round] | max // 0 | tostring),
     ([$e[] | select(.event == "phase_end") | .seconds] | add // 0 | tostring),
     ([$e[] | select(.event == "stop") | .category] | last // ""),
     ([$e[] | select(.event == "parked") | .resumeCommand] | last // "")] | @tsv' \
  <batch-state>-events.jsonl
```

Two things that look like liveness signals are not. **Review log size:** both
`codex exec` and `claude --print` buffer their output, so a review log sits at
0 bytes for the whole pass and then jumps; use the newest file time in the log
directory, the reviewer's CPU time, or `phases.jsonl` instead. **An agent
session's background task:** multi-hour runs launched as a background task of
an interactive agent session have been killed by that session's memory guard
with tens of gigabytes free. Launch long runs detached — `tmux`, `systemd-run`,
or an equivalent — with stdin closed.

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
