# Recover an agent-loop review budget

New run checkpoints use schema 4. `reviewBudget` records the original limit,
remaining seconds, and an append-only execution history. Review hooks and
budgeted validation hooks reserve their timeout before execution. An observed
ordinary return charges rounded-up monotonic elapsed seconds and refunds the
unused reservation. Waiting between hooks, operator investigation, and time
while the controller is stopped do not spend that budget. Existing independent
setup, worker, and final-publication validation limits still apply.

Timeouts, signals, power loss, or a crash before the completion checkpoint retain
the full reservation. The controller draws that line at the hook's exit status:
only a status below 124 is settled and refunded, so a hook that itself exits
with 124 or above, such as 126, 127, or 255, is handled as an interruption. The timeout's existing 15-second forced-cleanup grace is
additional to the execution limit. No refund is inferred from wall-clock time,
file modification time, event logs, a new timeout configuration, or a reboot.
A changed boot identity or a backwards monotonic reading refuses a refund.
Each observed invocation costs at least one second; repeated resumes cannot
increase the saved balance. A zero balance cannot launch another hook. The
Claude controller also retains its existing 120-second minimum-start threshold.

## Prerequisites

Adopt this change through the normal reviewed upstream sync into the consumer.
Keep all selected harnesses from the same upstream revision. Do not patch the
consumer's synced scripts by hand. An older controller cannot read schema 4;
keep the new helpers and controller installed together. Codex contract-v4
controllers must also satisfy their existing base-pinned tooling requirements.

Stop the controller and all its worker, reviewer, validation, and timeout
descendants. Check the actual process state; an absent terminal or stale PID
file does not prove they stopped. Preserve the issue worktree, checkpoints,
results, logs, and parent batch. If files were lost from temporary storage,
restore their original paths and private permissions from a verified archive,
without overwriting newer evidence.

Use the helper belonging to the original engine. The examples use `.claude`;
substitute `.codex` or `.agents` consistently. `show` validates the private file
without changing it. These commands are local checkpoint operations, not PR
attestations or permission to resume execution.

```bash
helper=.claude/skills/agent-loop/scripts/agent-loop-state.py
state=/absolute/original/log-directory/run-state.json
python3 -I "$helper" show --file "$state"
sha256sum "$state"
```

## Legacy checkpoints, including already-expired deadlines

Legacy checkpoints (Claude/Codex schema 2, Gemini schema 3) fail closed on resume, whether their absolute deadline is
expired, in the future, or absent. A deadline does not establish how much active
execution occurred. Migration always requires an explicit operator decision;
there is no automatic fresh budget.

Inspect the preserved execution evidence and select a conservative remaining
allowance. If the evidence cannot establish remaining time, stop or explicitly
authorize a bounded recovery allowance and record that uncertainty in the reason.
The operator-supplied limit must be no greater than the original configured
budget, and the remaining allowance must not exceed it. The tool enforces
`0 <= remaining <= limit <= 86400` seconds; it cannot reconstruct the original
configuration from a checkpoint which never stored it. Record zero if exhausted.

For example, after explicitly deciding that at most 900 seconds may remain from
an original 7200-second budget:

```bash
python3 -I "$helper" budget-migrate --file "$state" \
  --expected-sha256 <sha256-from-inspection> \
  --limit-seconds 7200 --remaining-seconds 900 \
  --confirm-stopped \
  --reason 'Evidence reference and operator rationale for this bounded allowance'
```

If the checkpoint has no saved round cap, also supply `--max-rounds N`, using
the original cap (1–4). A saved cap cannot be changed. Migration acquires the
same exclusive directory lock as resume, compares the inspected checksum,
preserves the original bytes in `run-state.json.legacy-<sha256>.json`, and
atomically installs the budget with the checksum, old deadline, operator UID,
timestamp, and reason. It preserves run identity, issue/PR/head/base bindings,
round, phase, reviewer settings, result hashes, and external attempt evidence.
Migration cannot run twice or replenish a schema-4 budget. A failure after
backup creation can be retried only while the original checkpoint is unchanged.

## Interrupted schema-4 execution

An unfinished reservation blocks further execution, even if other budget remains.
After verifying that all descendants stopped, explicitly reconcile it:

```bash
python3 -I "$helper" budget-reconcile --file "$state" \
  --expected-sha256 <sha256-from-inspection> --confirm-stopped \
  --reason 'Evidence reference confirming controller and descendants stopped'
```

This records the operator decision and marks the attempt abandoned. It does
**not** refund any time. A recorded controller PID still present in the same
boot blocks reconciliation, including ambiguous PID reuse. Resolve that
ambiguity before proceeding. Existing rounds, result recovery, attestation,
validation, and exhaustion rules still apply.

## Resume the original run or batch

Recheck the consumer's required configuration, reviewer/validation contracts,
issue eligibility, current PR and base, and existing review evidence. Budget
recovery fixes only time accounting; it does not repair other prerequisites.

For a standalone run:

```bash
.claude/skills/agent-loop/scripts/agent-loop.sh --resume-run "$state"
```

For a batch, migrate/reconcile only its active child's checkpoint, then resume
the **original parent** so its cursor and allowlist remain authoritative:

```bash
python3 -I "$helper" batch-show --file /absolute/original/batch.json
.claude/skills/agent-loop/scripts/agent-loop.sh \
  --resume-batch /absolute/original/batch.json
```

Use the consumer's established launcher and durable monitor when required.
Neither resume form supports `--dry-run`. A selection dry-run does not validate
checkpoint recovery. Tests on relocated copies do not attest a live PR or
constitute an actual resumed run.
