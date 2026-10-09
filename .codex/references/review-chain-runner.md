# Automatic PR review runner

Use the runner for an authorized automatic chain on an existing, self-authored,
same-repository draft PR in a clean dedicated worktree. It launches actual
reviewer processes; an interactive model does not decide whether to continue.
It is POSIX same-user automation, not isolation from a malicious reviewer.
Run it only where the configured reviewers may edit, commit, push and comment.

## Choose the plan and gates

Resolve Lean/Deep using REVIEW_WORKFLOW.md; the script enforces the resulting
budget but does not infer consequence-based triggers from filenames. Supply all
Deep trigger ids. Lean allows two passes per engine, Deep four. A fixed plan
that exceeds the selected cap is rejected, never silently promoted.

Place the user's authorization, scope and tier rationale in a public-safe text
file outside the worktree. Its contents are posted to the PR. Do not include
credentials or confidential context. Prefer the repository-declared validation
contract below. Its selected commands run without a shell, in the review
worktree, after each worker and before attestation, unless the pass cites an
earlier gate as described below. Commands are published in
pass summaries, so they must not contain credentials. Scoped tests are not a
substitute for the declared gate.

### Repository-declared validation

An opted-in consumer owns `.activeloom-review.json` at its repository root. For
a new run, the runner pins that policy from the pull request's current target
commit and separately verifies that `--base` is the merge base of the target and
head. A stale feature branch therefore receives policy newly adopted on its
target, while the pull request cannot supply or weaken its own contract. After
each reviewer the runner matches every path changed between the merge base and
the exact reviewed head. All matching gates are additive. If any path is
unmatched, or the diff is empty, the mandatory fallback gate is added.

```json
{
  "schema_version": 1,
  "fallback_gate": "full",
  "gates": {
    "baseline": {
      "always": true,
      "commands": [{ "argv": ["just", "typecheck"] }]
    },
    "backend": {
      "paths": ["apps/backend/**", "packages/shared/**"],
      "environment": { "NODE_ENV": "development" },
      "commands": [{ "argv": ["just", "test", "backend"] }]
    },
    "full": { "commands": [{ "argv": ["just", "test"] }] }
  }
}
```

The fallback gate has neither `paths` nor `always`. Every other gate declares
either at least one path or `"always": true`, never both. Always gates run
alongside selected path gates but do not claim ownership of paths, so an
unmatched file still selects the fallback. Commands are nonempty argv arrays,
never shell strings. Gate environments accept only explicit, one-line values.
Common credential-like names and controller variables are refused as defense in
depth, but name filtering cannot prove a value is safe: this committed public
file must never contain a secret. The runner starts from a small allowlist of
local process values (`PATH`, home/user/shell, locale and timezone), pins those
values in the checkpoint, and adds the gate's declared environment. Other
ambient values do not reach validation commands. A resume with different
allowlisted values blocks instead of silently changing the gate.

Route documentation and other non-executable paths to a gate of their own.
Matching is default-deny, so a path no gate claims selects the fallback on
every pass, and when the fallback is the full suite one edited Markdown file
costs a full run per pass. Give those paths a cheap gate that runs whatever
actually reads them: a link or prose linter, or a drift check for a generated
document.

```json
"docs": {
  "paths": ["docs/**", "*.md"],
  "commands": [{ "argv": ["just", "lint-docs"] }]
}
```

`*` does not cross `/`, so `*.md` claims root-level Markdown only. Prefer that
to `**/*.md`, which would also claim Markdown that tests or builds read from
directories no other gate owns and silently drop the fallback for it. There is
no ignore list: a path is either validated by a gate or it fails closed.

The contract is consumer-owned and is not created or rewritten by ActiveLoom
sync. A pull request that first adds or changes it continues to use the pinned
target policy; the new contract takes effect after it reaches the target branch.
Validate the proposed worktree copy in that pull request before merging it:

```bash
python3 .codex/skills/critique/scripts/review-chain-runner.py --validate-contract
```

Repositories without this file retain the legacy interface: pass every required
unfiltered command with repeatable `--check`. Legacy commands retain their
historical ambient environment and should be migrated to the contract. Once the
pinned target policy contains the contract, the runner rejects `--check` instead of
allowing an ad hoc gate to bypass repository policy.

Start the runner from the repository worktree root. It refuses a package or
subdirectory working directory so a repository command cannot silently acquire
narrower package-manager semantics.

Legacy example for a repository that has not adopted the contract:

```bash
python3 .codex/skills/critique/scripts/review-chain-runner.py \\
  --repo example/project --pr 42 --base <pinned-base-sha> \\
  --author codex --tier lean \\
  --chain codex,claude,codex,claude \\
  --check 'pnpm check' --authorization-file /absolute/path/review-authorization.txt

python3 .codex/skills/critique/scripts/review-chain-runner.py \\
  --repo example/project --pr 42 --base <pinned-base-sha> \\
  --author codex --tier deep --trigger 3 \\
  --cycle codex,claude --until-converged \\
  --check 'pnpm check' --authorization-file /absolute/path/review-authorization.txt

## Same options and authorization, plus --resume, continue a saved run.
```

These are alternative plans, not consecutive commands for the same active run.

- `--chain` executes every listed step, including repeats, without early exit.
  Per-engine round numbers count that engine's occurrences, not list positions.
  A one-engine plan is supported but cannot establish independent convergence.
- `--cycle` repeats two or three distinct engines until verified convergence or
  the tier cap. The legacy controller `--sequence` remains an alias for this
  cyclic policy; it does not mean a finite list.
- `antigravity` normalizes to `gemini`. Only Codex, Claude and Gemini have
  approved adapters; arbitrary commands cannot acquire review-attestation roles.
- `--author` is the actual author engine, not necessarily the first reviewer.
  Existing roster membership must match; reconcile changes explicitly first.
- `--scope-decision keep|split` records the decision `start-run` requires when
  its scope checkpoint fires (see REVIEW_WORKFLOW.md). It becomes part of the
  saved plan, so pass the same value with `--resume`.
- `--restart` explicitly authorizes a fresh run after the prior authenticated
  run has ended. The runner archives the terminal checkpoint beside its old
  location under a name containing the full run ID, then creates a fresh
  checkpoint and forwards `--restart` to `start-run`. It refuses to archive an
  active or incomplete checkpoint, or overwrite an existing archive. The old
  evidence remains available for inspection.

## Reviewer settings

Reviewer model and effort come from the user's review profile, which the
`review-setup` skill writes through `review-profile.py`. During its first preflight
the runner resolves each selected engine once, records the settings in the
checkpoint, passes them to every launch of that engine, and names them in each
pass attestation. A missing or invalid profile blocks the run before anything is
posted. Profile edits apply to the next run; resuming keeps the pinned settings.
Launchers validate the values against each CLI's accepted effort levels and keep
their permission, output and timeout flags fixed. `inherit` omits the model flag
so the engine CLI's own configured model applies.

`review-profile.py order --tier <lean|deep> --repo <owner/repo>` prints the user's
preferred engine order for `--cycle` when the user has not named a plan.

Codex can pin an optional capacity fallback alongside its primary model:

```bash
python3 -I .codex/skills/review-setup/scripts/review-profile.py set \
  codex.fallback.model=gpt-5.6-sol codex.fallback.effort=medium
```

No fallback is enabled by default. Set `codex.fallback=none` to disable it.
The runner recognizes the terminal model-capacity rejection from Codex JSON events.
After a confirmed exit with completed process-group cleanup, it verifies the
unchanged local/remote/PR head, clean worktree, unchanged comments and review
threads, missing result, and identical owed pass. It then switches once to the
pinned fallback for the remainder of the run. The failed attempt and snapshots
remain in the checkpoint; the retry uses the same round and remaining budget.
Attestations name the fallback model and effort. A second capacity rejection,
changed evidence, unknown failure, authentication error, or timeout blocks.
Standalone launchers do not perform this model fallback.

A Claude reviewer that reached execution and then exits 1 with only the CLI's
`API Error: 500 Internal server error.` diagnostic may retry once in
`<pass>/provider-retry`. A 500 taken while the launch marker still reads
`preflight` is a preflight failure and takes that path instead, and a 500 whose
launch marker is missing or does not match this attempt blocks rather than
retrying. The runner first
confirms process cleanup, the unchanged local/remote/PR head, clean worktree,
unchanged comments and review threads, no result or recovery sidecar, and the
same owed pass. It keeps the run, round, model, and original attempt evidence.
The retry rechecks that evidence immediately before launch and survives an
interrupted preparation via `--resume`. A second 500, other output, any partial
review evidence, a timeout, or an unknown exit blocks for reconciliation.
Standalone launchers do not retry.

The Codex one-pass launcher uses ephemeral noninteractive execution; its provider
remains the Codex CLI configuration. Its unattended
permissions match the existing agent-loop use case; a dedicated worktree is not
a security sandbox. See the repository's [OpenAI documentation setup](../../docs/openai-docs.md)
for current official documentation sources.

The automatic Claude launcher sets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`
for its worker, overriding an inherited value. Claude waits for shell commands
and subagents in the foreground instead of leaving background work unfinished
when the one-shot session exits. Foreground subagents can still run concurrently
when dispatched together. This setting is local to the launched process; it
does not change interactive sessions. It is a completion workaround, not a
guarantee that a worker writes its result: the runner still verifies that file.
See [Claude Code's environment-variable reference](https://code.claude.com/docs/en/env-vars).

## Preflight and installed review tools

Before posting a run or launching its first reviewer, the runner preflights every
selected engine. Launchers repeat their checks immediately before each review.
Missing executables, dirty surfaces, failed provenance checks, and incompatible
skill contracts stop the chain before review time is spent.

The runner owns its review installation under the checkpoint directory. Native
Codex/Claude surfaces are archived from a recorded consumer commit and checked
against a pinned file manifest. Gemini uses a separate clean checkout at the
Agy launcher's existing trusted ActiveLoom commit. Its first installation needs
network access to the canonical public upstream. Review prompts address those
files directly instead of depending on mutable development trees or global
skill symlinks. Consumer instructions and review addenda still come from the
review worktree; model and effort are the run's pinned profile settings.

The managed Gemini checkout is a standalone Git repository, not a linked
worktree of a developer's upstream clone. Moving that clone cannot invalidate
the installation's Git metadata. For other linked worktrees, run
`git worktree repair` after moving their primary clone, then verify each
worktree's head and status before resuming.
Keep controller output as well as worker logs when pausing a legacy run.

If an installation is damaged, add `--repair-installation` to the printed resume
command. The runner preserves the old directory and builds a replacement at
the same pins. It never cleans or discards a developer's modified checkout.

## Results and recovery

Each worker writes a canonical result, not an attestation. The runner verifies
the result and complete thread ledger, checks local/remote/PR heads, runs the
required gates, then attests. It snapshots control scripts outside the worktree
so a worker's source changes cannot replace the next launcher. DCO is checked
when the repository has its standard DCO workflow, or with `--require-dco`.
It never repairs DCO by rewriting history.

The runner does not repeat a gate it has already passed on the same commit. When
a pass returns clean and leaves the head and worktree unchanged, and this runner
process recorded the same resolved gates passing at that exact head under the
same validation contract and policy revision, the pass cites that earlier gate
instead of running it again. Its `validated.json` names the cited pass and the
digest of that pass's receipt, and its attestation lists the gate commands, the
head, and the pass that ran them. Every other pass runs its gates: one that
changed the head, the first pass of a run, the first pass validated after a
restart or `--resume`, and a validation repair. A pass interrupted after its
receipt was saved keeps that receipt on resume, and a saved citation is
rechecked against the cited receipt. A gate an engine ran inside its own pass
is never cited. One consequence: a suite is no longer run several times on one
commit, so a flaky test that only fails on a repeat run goes unnoticed.

State, private logs and result files live under the Git common directory's
`activeloom-review/<owner>-<repo>-<pr>/`. A per-PR lock prevents concurrent
runners in linked worktrees. Keep this directory for recovery; do not delete
it to obtain another budget. Plans, actor, tier, base and control hashes are
checked on resume. Repository-declared validation also pins the target policy
revision, resolved schema, and allowlisted execution environment. Changes to the
pinned runner require deliberate migration, not execution from an unverified
replacement.

### Conversation updates

Run setup publishes its tier, engine order and authorization together in the
run-start comment. A matching v2 cross-engine roster is reused across runs;
initial or legacy rosters get one declaration naming the author and independent
reviewers. Run-end comments show the outcome and head, including an explicit
absence of convergence for aborted or exhausted runs. Historical evidence is
preserved.

### Pass telemetry

The runner owns each launched pass's telemetry boundary. Before the launcher
starts, it mints the `review` pass key and takes the start snapshot with the
worker engine's own usage helper into `<pass>/telemetry-boundary/`, records the
key and snapshot digest in the checkpoint, and hands the directory to the worker
as `$AGENT_LOOP_TELEMETRY_DIR`. Workers run in ephemeral sessions with no usage
log, so the snapshot deliberately names a log that never exists. Every delta
that passes that log as `--session-log` then reports `unavailable` rather than
measuring another session.

For a worker observed returning successfully, the checkpoint also records its
elapsed launcher time using a monotonic clock. Fallback telemetry uses that
interval when extraction is enabled and no usage-derived duration exists. It
includes launcher setup and cleanup, excludes later controller validation and
operator waiting, and does not imply measured tokens or model identity. Older
checkpoints and attempts without an observed successful return keep duration
unavailable.

If the worker already published a record with a null duration, the runner uses
`enrich-telemetry-duration` to fill that field on the same comment after its
observed return. The ledger checks the actor, pass key, engine, round, base and
head, verifies the original body before updating, then reads it back. Existing
measurements, findings, token counts and timestamps are preserved. The operation
is idempotent and nonfatal; emission and extraction opt-outs both prevent it.
It does not infer token counts or model identity from elapsed time.

For managed Gemini passes, the Agy launcher retains a numeric-only receipt from
its successful single-turn JSON result. The runner binds the receipt to the
attempt and its observed return hash, aggregates every invocation in an
automatic retry of the same pass, then publishes after the worker exits.
Worker emission is disabled for that managed boundary to avoid an earlier
unavailable record consuming the same idempotency key. Extraction and emission
opt-outs still apply independently. Missing, changed, resumed-session, or invalid
receipts remain unavailable; no transcript or response text is retained.

Agy's aggregate counts have no observed model or per-lens identity. They use a
v3 telemetry record with one `model: null`, `effort: null` bucket. Input, output,
cache reads and thinking remain separate; thinking is not added to output.
The CLI total is preserved as `providerBuckets.total_tokens`; cache writes stay
unknown. Its token source is `terminal-json`, not `session-log-delta`. Existing
known-model records remain v1; v2 remains reserved for the assurance contract.
Analytics readers must support v3 before enabling this producer; older pinned chains keep their old behavior.
Standalone Gemini helpers without a managed receipt still report unavailable.

When the pass settles, the runner publishes the record itself if no marker
carries that key yet. A counted pass reports its result's status. A pass whose
worker returned without a result or with a blocked one, or failed mid-review,
reports `blocked`. Launches that the runner retries automatically, and workers
whose exit is unknown, are not settled and emit nothing: the worker may still
publish under the key. The runner derives finding counts from this pass's own threads.
It emits nothing when a count is unknowable: cleanup findings sharing the pass,
or a new fingerprint on a PR that already has a `fixed` disposition, which
needs the reviewer's blame trace. Telemetry failures are logged and never
block or fail a pass. A checkpoint written before this boundary existed has no
key, so its pending pass leaves telemetry to the reviewer.

Exit outcomes:

| Outcome       | Exit | Meaning                                                                                   |
| ------------- | ---- | ----------------------------------------------------------------------------------------- |
| converged     | 0    | Required steps and current-head ledger/coverage gates passed.                             |
| plan-complete | 3    | Every fixed step ran, but independent/current-head/non-material evidence is insufficient. |
| exhausted     | 3    | The cyclic plan reached its cap without convergence.                                      |
| blocked       | 2    | A process, result, gate, head, or permission boundary needs reconciliation.               |

For compatibility with the vendored run-end grammar, an un-converged completed
fixed plan records `exhausted` as its terminal marker; checkpoint/output retain
the more precise `plan-complete` reason. Neither grants extra passes.

Agy's print mode ends the session when the root agent ends its turn, and
terminates background shell commands after a short drain timeout (5 seconds).
The Agy launchers therefore prohibit subagents and background review lanes during
print-mode passes. Each review lane runs sequentially in series within the primary
session. In each lane pass, the worker posts verified findings inline, applies
justified fixes, and validates before proceeding to the next lane, ensuring subsequent
lanes evaluate the updated code and prior findings without wasted repetition. Every
shell command and test suite executes synchronously in the foreground. The worker
writes the canonical result and ends the turn only after all lanes and validation
finish. When a Gemini worker still exits 0 without a result, the runner verifies
the execution-phase launch marker, unchanged head and owed pass, absent result
and recovery sidecar, unchanged review threads, and unchanged issue comments.
It then relaunches that pass once with the same round, budget and pinned
settings. A single exact-head Gemini no-op cleanup marker is the only permitted
comment delta because cleanup precedes the review lanes and that marker is
idempotent. A second incomplete exit, any other changed evidence, a partial
result, or a failed exit blocks.

Agy's idle-termination lines remain useful for classifying the incomplete exit,
but recovery no longer depends on model or CLI prose. Recognizing those lines is
plain text matching, not the structured-event parse the Codex capacity check
uses. Some Agy builds omit the runtime diagnostics and instead leave only
repeated root-agent messages that they will wait for unfinished subagents. The
runner recognizes two or more of those anchored messages as the same idle-exit
class; a single mention is insufficient. In every case, the structured launch
boundary and live evidence re-verification authorize the one retry; a missing
or preflight-only launch marker never does.

Workers never inherit the runner's stdin: the runner starts them on `/dev/null`,
and the Codex launcher detaches its own stdin as well. `codex exec` reads a
non-TTY stdin to end of file before it starts, so a caller holding a pipe open
would otherwise hang the pass indefinitely. Codex also emits `thread.started`
about a second after launch. If a Codex worker has not written that event after
three minutes, the runner stops its process group instead of waiting out the
pass timeout. Nothing that can post, commit or push runs before that event. So,
after cleanup completes, the runner verifies the same unchanged evidence as the
capacity fallback and relaunches the pass once in `<pass>/stall-retry`, with
the same round, budget and pinned settings. A second stall, a log that did
record `thread.started`, a partial result, changed evidence or denied cleanup
blocks. Other engines have no startup watchdog: they emit no comparable early
event.

The runner never marks ready or merges, even on success. It reports the evidence
to the caller, who follows the repository's finalization policy.

When a completed review fails final result verification, the ledger helper can
preserve a blocked result and `<result-file>.recovery.json`. The runner pins that
sidecar's digest at a successful worker return. It invokes `recover-result` to
recheck the original identity, blocked bytes, pre-pass snapshot and live ledger,
then continues ordinary validation and attestation in the same pending pass.
No reviewer is relaunched and the run ID, round and budget stay unchanged.
This requires a helper that implements `recover-result`; update the published
bundle through its normal verified distribution path, never patch it locally.

### Automatic validation repair

When a reviewer returns a clean canonical result at the unchanged head and a
required controller validation command then exits ordinarily with status 1–123,
the runner automatically offers that same reviewer **one repair attempt for the
owed pass**. This applies to Codex, Claude and Gemini. The reviewer receives the
failed gate log as diagnostic evidence, must post verified findings before fixes,
and may repair necessary fixtures within the authorized task. It may not weaken
gates or expand unrelated scope. The controller reruns every required gate on
the resulting head before attestation; the earlier candidate is never attested.

The original result, failed log, command, exit status and ledger snapshots remain
in the original pass directory. The retry lives in `validation-retry`. The run
ID, base, engine, model pins, round, round cap and previous attempts are retained.
The repair slot uses the existing per-launch timeout (at most 3660 seconds
including launcher cleanup); it is persisted and cannot be renewed by resume.
This is a bounded extra attempt, not an automatic abort/restart or fresh run.
New controllers include this behavior in the authorized automatic-chain policy;
older pinned controllers retain their original recovery behavior.

An interrupted staging transaction resumes that same slot. Changed heads or
ledger evidence, modified saved evidence, failed cleanup, unknown exits,
timeouts and signals block automatic repair. A candidate that already changed
the head also requires ordinary explicit recovery: this narrow path does not
rewrite its unfinalized transition. When the repair is refused before its retry
is staged, the slot is forfeited and the pass returns to the ordinary gate
rerun: `--resume` reruns the failed gates and attests the original candidate
only if they pass. A pending pass saved without pre-pass ledger digests gets no
repair either. A second failed gate cannot launch another
repair. `--resume` may rerun its gates after an environmental fix, but cannot
replenish the repair slot. No abort, new budget, ready transition or merge is
implied. Independent exact-head review remains required after a repair commit.

`--resume` can retry finalization, rerun a failed validation command and reconcile an attestation
posted before a checkpoint write, without relaunching the worker. Each launch
attempt records its execution boundary, known exit status, and structured
failure reason. A crash between the boundary write and process creation remains
unknown; it is not evidence that a retry is safe. Local logs name the failing
checkout and changed paths, and blocked output prints the exact resume command.

After repairing a proven preflight-only failure, add `--recover-preflight` to
that command. Proof requires a recorded unsuccessful launcher exit and completed
process-group cleanup; a preflight marker alone cannot authorize a retry.
Recovery rechecks the live head and ledger, preserves the run ID,
round, completed passes, original comment snapshots and attempt history, and
launches only the owed pass. The retry has its own directory. It consumes the
same remaining run budget. Outside the Codex capacity fallback, Claude provider-500
retry, Codex startup-stall retry, and Agy idle-exit or incomplete-exit retry above, a missing result,
a blocked result without a sealed completed candidate, unknown exit, interrupted
reviewer, changed head, or changed evidence still requires reconciliation; none
is silently retried or converted into passing evidence.

Recovery preparation records its intent before staging files so an interruption
can resume the same transaction. Each launch rechecks the saved run and owed
pass; the launcher's `authorize-pass --run-id` check also rejects a replacement
run at the same head and round.

If a worker exits successfully but process-group cleanup is denied, the runner
preserves the observed exit separately from cleanup and seals the completed
result, worker log, and any result-recovery receipt. It still stops immediately.
Once the group has disappeared, `--resume` uses a read-only group probe and
checks the sealed evidence before continuing normal result, head, ledger, and
validation checks. It does not send further signals, relaunch the worker, or
spend another pass. A surviving group, denied probe, changed evidence, missing
result, failed exit, or interruption cannot use this recovery path.

Older checkpoints that recorded the exit as unknown lack this evidence and
remain blocked. A success message in a worker log does not replace a recorded
process exit; do not edit the checkpoint to mark it returned.

### Migrate an existing checkpoint

Version 1 checkpoints require an explicit adoption step. Validate the new
controller in a clean checkout at a full commit SHA, then invoke that checkout's
runner with the original arguments plus `--resume --migrate-controller <sha>`.
The migration validates old control hashes, saves `state-v1.json`, retains the
old control directory, and records the new hashes and source revision. It does
not restart or renumber the run. Future resumes use the newly printed command.

For a version 1 Gemini attempt with a supported preflight rejection,
an operator can additionally supply `--recover-preflight
--reconcile-legacy-preflight <worker-log-sha256>`. This narrow reconciliation
requires the recognized old controller/launcher hashes, the exact pre-execution
diagnostic, unchanged head and ledger, and no reviewer output. Inspect the log
and old launcher before supplying its hash. Supported evidence is:

- The sole `agy relay surface checkout must be clean` diagnostic, whose pinned
  launcher path exits 1 before review.
- The sole Git `fatal: not a git repository:` diagnostic naming an absolute
  `.git/worktrees/<name>` path or Git 2.54–2.55's `(null)` target, together
  with `--legacy-controller-log <path> <sha256>`. The original controller log must
  end with the matching Gemini round/head announcement and the controller's
  `bash exited 128` terminal message for this checkpoint. This also proves
  the pinned controller completed cleanup; interrupted or denied cleanup has
  a different terminal message. A worker Git diagnostic alone is insufficient.

Reconciliation pins both log hashes and records the actual failure reason and
exit. It preserves the failed pass and creates a separate retry directory.
Never reconstruct missing controller output or broaden the proof to arbitrary
exit-128 failures. All other legacy failures remain unknown; migration alone
never grants permission to relaunch them.

Before starting a new automatic run from a long-lived feature branch, verify
that its installed controller includes all-engine preflight, managed reviewer
installations, and structured attempt recovery. Receive updates through the
normal reviewed upstream sync before pinning a new run. An existing v1 run
needs the explicit migration above; updating a checkout does not replace its
pinned controller or reset its budget.

The controller never automatically starts a replacement run or replenishes a
budget. Keep the entire checkpoint directory until recovery is complete.

Process timeouts and ordinary cancellation stop the owned process group unless
a cleanup signal is denied. If any cleanup signal is denied, whether or not the
worker has exited, the runner probes the group without sending a signal. In
that case cleanup succeeds only if the probe confirms that the group no longer
exists. A surviving group or a denied probe blocks progression immediately,
without further escalation. The diagnostic includes the group ID, the worker
exit status once the worker has exited, and the timeout, interruption, or
failed exit that started cleanup, for reconciliation.
After host failure or an uncatchable kill, an operator must reconcile any
surviving reviewer before recovery; no script can guarantee progress while its
host is down. The guarantee is that a running controller advances verified
passes without another conversational turn, not that workers cannot fail.

### Abort an interrupted run, then separately authorize restart

A worker that exits without `result.json` can leave a nonterminal checkpoint
that neither `--resume` nor `--restart` can advance. Use the recovery interface
from the **original review worktree root**, with a reviewed controller that
supports these commands. Diagnosis and abort read the checkpoint's original
pinned helpers; they do not migrate its controller or launch reviewers.

```bash
python3 /path/to/reviewed/checkout/.codex/skills/critique/scripts/review-chain-runner.py \
  --repo example/project --pr 42 --diagnose
```

Diagnosis is read-only. It reports the local and authenticated ledger states,
current and checkpoint heads, worker attempts, potential surviving workers,
blockers, and an `evidence_sha256`. Inspect the discrepancy and the preserved
files at the printed checkpoint path. Exit 2 with a report means the listed
blockers need reconciliation. Exit 2 with only a `review-chain blocked:` line
means a mandatory condition failed and no digest was produced. A clean dedicated worktree, matching local/remote/PR heads,
unchanged authenticated actor, original controller hashes, and the same run
identity are mandatory. Linux uses readable `/proc` process evidence. macOS
uses the system `ps` and `lsof` tools plus `KERN_PROCARGS2` for process
environments. It checks working directories before environments: a process in
the worktree is always a potential worker. For a hidden environment outside the
worktree, macOS can establish that the process or its session leader predates
the authenticated run by more than one minute. This follows the runner's
one-shot, new-session launch contract; it does not authorize reviewers to hand
work to existing desktop sessions. Process creation time survives `exec`, so a
readable review identity still overrides age. The margin accommodates small
clock skew; recovery assumes the host and GitHub clocks agree within that
margin. A second exception covers launchd-owned Apple services at system service
paths whose live kernel code-signing flags prove a valid, restricted platform
binary with no untrusted helpers and no debug allowance. A name or path alone
does not qualify. A third exception covers the `/usr/bin/caffeinate` session
helper Claude Code keeps running: with the same code-signing proof, and whose
parent is an ancestor of the diagnosing command other than launchd. The runner
starts each worker as its own child and an orphaned worker is reparented to
launchd, so no worker has such a parent. Shells also hide their environment and
are never exempt, because a session shell that launched the runner can still
run commands after it. The helper's descendants are still checked individually.
Zombies have already exited and cannot mutate the review.
Unknown creation times, new review sessions, unverified services, and unreadable
working directories remain blockers. Other platforms are unsupported. Recovery must run
in the process namespace the review ran in: inside a sandbox with its own PID
namespace the probes cannot see a surviving worker, so an empty `workers` list
from there proves nothing. A same-user process
that hides its environment and cannot be excluded by the platform's evidence
checks is named by PID, and diagnosis stops until it is reconciled.
Any other same-user process whose working directory is inside the worktree is
reported as a potential worker, including a second shell, an editor, or the
other side of a pipe such as `--diagnose | tail` started from the worktree;
close them and run the command unpiped. An unknown exit without
an explicit cleanup-completed receipt, live process groups, unreadable process evidence, unfinished terminal writes,
or conflicting terminal markers refuse abort. Do not edit a checkpoint to
manufacture proof of a worker exit or discard it to obtain another budget.

After explicitly authorizing abandonment of that run at the inspected head,
copy its exact run ID and evidence digest into:

```bash
python3 /path/to/reviewed/checkout/.codex/skills/critique/scripts/review-chain-runner.py \
  --repo example/project --pr 42 --abort-run <diagnosed-run-id> \
  --evidence-sha256 <diagnosed-evidence-sha256>
```

This command rechecks the evidence and records an intent in `abort.json` before
posting the authenticated `aborted` terminal marker. It never marks a failed
attempt successful. The original `state.json`, logs, results, snapshots,
findings and attestations remain intact. An evidence change before the remote
abort requires fresh read-only diagnosis, inspection and explicit authorization
using the new digest. The command preserves the prior intent under
`.abort-staging/intent-<old-digest>.json` before replacing it; changed checkpoint
files remain a blocker. If the authenticated aborted marker already exists,
repeat the command with the digest recorded in `abort.json`, not a freshly
diagnosed one: it completes the receipt despite later PR
conversation or commits, provided the marker matches that intent and
the preserved files are unchanged. Once `abort.json` exists, `--resume` is
refused for this run. Conflicting terminal evidence requires
manual reconciliation, not deletion. If interrupted after the intent or after
the remote marker was posted, repeat the **same abort command**. An identical
completed invocation is a no-op after live verification. Do not run standalone
reviewers or change the PR during recovery; the per-PR lock excludes other
controllers but is not a lock on all GitHub writers.

Abort stops there. Only after separate authorization, run the normal full plan
command with `--restart --restart-aborted <aborted-run-id>`, the desired pinned base and a fresh authorization
file. For example, in a repository with a validation contract:

```bash
python3 .codex/skills/critique/scripts/review-chain-runner.py \
  --repo example/project --pr 42 --base <pinned-base-sha> \
  --author codex --tier deep --trigger 3 \
  --cycle codex,claude --until-converged \
  --authorization-file /absolute/path/new-review-authorization.txt \
  --restart --restart-aborted <aborted-run-id>
```

Restart requires the completed abort: a receipt in the `aborted` phase, the
authenticated `aborted` marker at its head, and unchanged preserved files. If
the abort is still only prepared, repeat the same abort command first. Comments,
thread changes or commits made on the PR after the completed abort do not block
restart. The first restart repeats the diagnosis, so its mandatory conditions
and process checks apply again at the live head. It then
archives the **entire directory** as `<owner>-<repo>-<pr>-run-<full-run-id>`. It creates a
**new budget**; previous convergence is not implied and stale-head passes are
not current-head evidence. The aborted run ID is the restart idempotency key. Repeating the same command
resumes or reports that one successor; it never creates another budget, even
after the successor ends. Its printed `--resume` command also preserves this key.
An interrupted archive rename preserves the old directory; rerunning restart
can initialize the empty active location. If the remote start succeeds before the local run ID is saved, the same command
replays only the matching authenticated successor. A different successor, plan
or terminal outcome blocks reconciliation rather than granting another budget.

Same-run adoption of later standalone attestations is **unsupported** here.
Their presence in an authenticated ledger does not bind them to the failed
local worker's result, validation receipts, head transitions and attempt
budget. Diagnosis displays the ledger decision; abort preserves it. It does
not synthesize a canonical result or count those passes in the old checkpoint.

Consumer rollout: verify that the installed runner contains `--diagnose`,
`--abort-run` and `--restart-aborted`. For a run pinned before the update,
invoke the reviewed checkout's entry point from the original consumer worktree
for all three steps and retain the original controls. Test diagnosis first,
obtain explicit abort and restart authorization separately, and retain the
archived directory.
