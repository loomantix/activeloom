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
credentials or confidential context. Pass each required unfiltered test/build
command with `--check`; these run without a shell, in the review worktree, after
each worker and before attestation. Include coverage thresholds and other
repository-required gates explicitly. Commands are published in pass summaries,
so do not embed credentials in them. Scoped tests are not a substitute.

Example for a repository whose required baseline is `pnpm check`:

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

The runner uses the existing Claude/Agy launchers without overriding their model,
effort or permission flags. The new Codex one-pass launcher retains configured
model/provider choices and uses ephemeral noninteractive execution. Its unattended
permissions match the existing agent-loop use case; a dedicated worktree is not
a security sandbox. See the repository's [OpenAI documentation setup](../../docs/openai-docs.md)
for current official documentation sources.

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
review worktree; model and provider choices retain the launcher defaults.

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

State, private logs and result files live under the Git common directory's
`activeloom-review/<owner>-<repo>-<pr>/`. A per-PR lock prevents concurrent
runners in linked worktrees. Keep this directory for recovery; do not delete
it to obtain another budget. Plans, actor, tier, base and control hashes are
checked on resume. Changes to the pinned runner require deliberate migration,
not execution from an unverified replacement.

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

The runner never marks ready or merges, even on success. It reports the evidence
to the caller, who follows the repository's finalization policy.

`--resume` can rerun a failed validation command and reconcile an attestation
posted before a checkpoint write, without relaunching the worker. Each launch
attempt records its execution boundary, known exit status, and structured
failure reason. A crash between the boundary write and process creation remains
unknown; it is not evidence that a retry is safe. Local logs name the failing
checkout and changed paths, and blocked output prints the exact resume command.

After repairing a proven preflight-only failure, add `--recover-preflight` to
that command. Recovery rechecks the live head and ledger, preserves the run ID,
round, completed passes, original comment snapshots and attempt history, and
launches only the owed pass. The retry has its own directory. It consumes the
same remaining run budget. A missing/blocked result after execution, unknown
exit, interrupted reviewer, changed head or changed evidence still requires
reconciliation; none is silently retried or converted into passing evidence.

### Migrate an existing checkpoint

Version 1 checkpoints require an explicit adoption step. Validate the new
controller in a clean checkout at a full commit SHA, then invoke that checkout's
runner with the original arguments plus `--resume --migrate-controller <sha>`.
The migration validates old control hashes, saves `state-v1.json`, retains the
old control directory, and records the new hashes and source revision. It does
not restart or renumber the run. Future resumes use the newly printed command.

For a version 1 Gemini attempt with only the supported dirty-checkout rejection,
an operator can additionally supply `--recover-preflight
--reconcile-legacy-preflight <worker-log-sha256>`. This narrow reconciliation
requires the recognized old controller/launcher hashes, the exact pre-execution
diagnostic, unchanged head and ledger, and no reviewer output. Inspect the log
and old launcher before supplying its hash. All other legacy failures remain
unknown; migration alone never grants permission to relaunch them.

The controller never automatically starts a replacement run or replenishes a
budget. Keep the entire checkpoint directory until recovery is complete.

Process timeouts and ordinary cancellation stop the owned process group.
After host failure or an uncatchable kill, an operator must reconcile any
surviving reviewer before recovery; no script can guarantee progress while its
host is down. The guarantee is that a running controller advances verified
passes without another conversational turn, not that workers cannot fail.
