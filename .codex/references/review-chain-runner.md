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
posted before a checkpoint write, without relaunching the worker. A missing or
blocked result, failed launcher, unknown worker exit, or externally changed head
stops at the owed pass. Resume does not automatically retry that reviewer or
convert a blocked result into clean evidence. Preserve its files and use the
ledger's supported recovery procedure; if recovery needs new authority, ask.
The controller never automatically starts a replacement run.

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
