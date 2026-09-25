---
name: backlog-refinement
description: 'Prepare a GitHub backlog for autonomous agent-loop completion — assess each open issue against the agent-readiness rubric, set its priority, rewrite agent-shaped issues into agent-ready form and tag dev: agent, exclude the rest with agent-bail reasons and a needs label naming the interview that unblocks them, and run the post-loop RCA that sharpens the rubric from every bail. Use when the user says refine backlog or refine-backlog, asks to set up backlog refinement, or before agent-loop, after issue triage, or after a loop run.'
---

# backlog-refinement

Maximize how much of the backlog `agent-loop` can complete unattended, route everything else to the step that would make it buildable, and **learn from every failure** so the backlog and the loop both get smarter over time.

```
backlog-refinement (prep)  →  dev: agent queue  →  agent-loop (consume)
        ▲                                                   │
        └──────────  RCA sharpens the rubric  ◀── agent-bail:* on bail
```

**Arguments**: `$ARGUMENTS` — dispatch on the first word; default to `queue`. Modes: `setup`, `queue`, `refine [n | --all | --limit N | --backfill]`, `assess <n>`, `rca [run-window]`.

## Two layers: the core rubric and the repo's local files

| File                                 | Owner                    | Holds                                                                                                                                                  |
| ------------------------------------ | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [`core-rubric.md`](./core-rubric.md) | upstream (synced)        | Criteria true of **any** repo: readiness, make-ready transformations, the bail taxonomy, priority tiers, needs labels, the issue-body template.        |
| `.backlog/refinement.local.md`       | this repo (never synced) | This repo's **instances**: integration branch, sensitive-path disqualifiers, extra transformations, priority label names and definitions, skip labels. |
| `.backlog/learnings.local.md`        | this repo (never synced) | The append-only RCA log.                                                                                                                               |

Both `.backlog/` files sit at the repository root and are read identically by every harness, so a lesson recorded during a Codex run is applied by the Claude run that follows it. **Read `core-rubric.md` and `.backlog/refinement.local.md` fully before acting.** Where they disagree, the local file wins — it is the repo narrowing or extending the core, never the reverse.

### Preflight — every mode except `setup`

Check that `.backlog/refinement.local.md` exists at the repository root.

- **Present** → continue.
- **Absent, but a legacy `RUBRIC.md` exists** under any `*/skills/backlog-refinement/` → the repo predates the local layer. Say so, and offer `setup` to migrate it. `queue` and `assess` may continue against the legacy file; `refine` and `rca` wait for the migration, because they write to files that are about to move.
- **Neither** → the repo has never been set up. Offer `setup`. `queue` may continue on core defaults; every other mode waits.

## Mode: `setup`

Walk the user through creating or migrating the local files and the labels. The full procedure is in [`SETUP.md`](./SETUP.md) — read it and follow it. It is done when both `.backlog/` files exist with every `TODO(backlog):` marker either filled or explicitly deferred by the user, every label the core and local rubrics name exists in the repo, and any legacy rubric has been migrated and removed with the user's agreement.

## Mode: `queue` (default)

Show what is left to refine.

```bash
python3 .codex/skills/backlog-refinement/scripts/candidates.py
# --json for machine output, --limit N, --include-refined to also list assessed issues
```

Report the counts the script prints: ready, re-verify, conflicted, excluded, epics, skipped, backfill, and un-refined (the work).

- **Re-verify** — `dev: agent` without `agent: refined`: tagged by something other than this skill and never verified against HEAD. `refine --all` does not walk this bucket, yet it is exactly what `agent-loop` consumes. Clear it before trusting the queue.
- **Backfill** — assessed issues that are missing what the current core rubric sets: a priority label, or the `needs:` label a grill-class bail requires. `refine --backfill` clears it.
- **Skipped** — issues carrying a label in the local file's `auto-managed-labels` marker: opened and closed by a scheduled workflow. Never comment on one; a comment resets its `updatedAt` and delays the workflow's auto-close.

## Mode: `refine [n | --all | --limit N | --backfill]`

Default refines the next un-refined issue; `--limit N` a batch; `--all` the whole un-refined bucket. Run the **re-verify** bucket through the same steps before (or alongside) `--all`. Every exclusion below also removes `dev: agent` when present — it and `agent-bail:` never coexist — and replaces any `agent-bail:` and `needs:` label left from an earlier assessment rather than adding to it. Sanity-check the rewrite on a handful (`assess <n>` or `--limit 5`) before a large sweep, since it edits issue bodies at scale. For each issue:

1. **Read it fully** — `gh issue view <N>` including comments.
2. **Set priority** (core rubric, _Priority_). Skip when the issue already carries one of the local file's priority labels — a priority a human set stands. Otherwise apply exactly one, judged from impact and urgency alone. Readiness does not change priority: an excluded issue gets one too.
3. **Early-exit excludes** — if the title/body matches a Bucket-B disqualifier on its face (core §3 or a local one), apply `agent: refined` + the `agent-bail:` label + for a grill-class category the `needs:` label the core rubric maps it to, comment one line citing the clause, and move on.
4. **Verify against HEAD** (core §2). Fetch the integration branch the local file names:
   - **Already fixed** → `agent: refined` + `agent-bail: stale`, comment with the evidence (commit / PR / `file:line`) and recommend close. Closing is the human's call, unless the local file's `stale-action` marker is `close`: then close it yourself as `completed` or `not planned`, after the evidence comment.
   - **Partially shipped** → rewrite the body to the residual and assess the residual.
   - **Still open** → continue.
5. **External-dependency check** (core §2) — a dependency that is not published and consumable from this repo → `status: blocked` + `agent-bail: cross-repo`, comment, stop.
6. **Assess against core §1 and the local additions.** A Bucket-B failure → exclude with the matching `agent-bail:` label, its `needs:` label when grill-class, and a comment. A Bucket-A failure a §2 transformation can fix → apply it.
7. **Rewrite** a passing issue to the core §5 template:
   - Preserve the original verbatim under a `> ### Original report` blockquote.
   - Fill Goal / Acceptance criteria / Files-entry-points / Out-of-scope from real `grep` and file reads.
   - Write the body to a repo-scoped temp path, then `gh issue edit <N> --body-file <path>`.
   - Apply `dev: agent` + `agent: refined`, and remove any `agent-bail:` and `needs:` label left from an earlier bail, plus the `status: blocked` a `cross-repo` bail added.
   - If the local file's **Rewrite mode** is `suggest`, post the body as a comment instead and leave the issue body untouched.

Keep every rewrite inside the scope the issue asked for, with acceptance criteria grounded in the code, and tag `dev: agent` only on issues that pass every §1 criterion. **When torn between make-ready and exclude, exclude** — a false `dev: agent` costs a whole loop iteration; a false exclusion just waits for a human.

### `--backfill`

Walk the **backfill** bucket from `candidates.py` and apply only steps 2 and the `needs:` mapping: add the missing priority, and on a grill-class bail add the missing `needs:` label. Change no body, add no comment, and leave every other label alone — these issues were assessed under an older rubric and the assessment still stands.

Process sequentially, one `gh` mutation at a time. Summarize at the end: tagged ready, excluded by category, needs labels applied by kind, priorities set by tier, re-scoped.

## Mode: `assess <n>`

Dry-run one issue: the priority you would set, the §1 verdict, the §2 transformations that would apply, the `needs:` label on an exclusion, and the proposed rewritten body — without mutating anything.

## Mode: `rca [run-window]` — close the learning loop

Run after a `agent-loop` run.

```bash
python3 .codex/skills/backlog-refinement/scripts/bail-report.py                    # every agent-bail:* issue
python3 .codex/skills/backlog-refinement/scripts/bail-report.py --since 2026-01-01  # a window
```

For each bailed issue and its `<!-- agent-loop-rca ... -->` stub (core §4):

1. **Re-ask the two questions** (core rubric, top) on the actual outcome; confirm the bucket rather than trusting the inner agent's self-classification.
2. **Bucket A bail = a refinement miss** — the most valuable signal. Name the §2 transformation or §1 check that would have caught it at prep time.
3. **Repeated Bucket-B shape = a dull disqualifier.** Sharpen it so refinement excludes that shape on sight.
4. **Loop-mechanics bail = an instructions or script gap.** Record the fix for `agent-loop-instructions.md`; a fix to a synced script is an upstream change.
5. **Route the edit.** Ask: _would this rule still be correct in a repo that has never heard of this project?_ **Yes** → it belongs in `core-rubric.md` upstream; record it in the local file under _Upstream candidates_ until it lands. **No** → edit `.backlog/refinement.local.md` and bump its local rubric version.
6. **Append a dated entry to `.backlog/learnings.local.md`** for each distinct lesson.

An RCA over a run that produced bails is complete when every bail has a learnings entry and every entry names its edit.

```markdown
### <date> — #<issue> — <short title> [bucket A|B | <agent-bail category>]

- **Outcome:** PREVENTABLE | INHERENT
- **What could we have done differently:** <the answer, or "nothing — inherent">
- **Rubric/loop change:** <the edit, and whether it is local or an upstream candidate>
- **Evidence:** <commit / file:line / comment link>
```

## Boundaries

- Synced files — this skill, `core-rubric.md`, the `agent-loop` skill and scripts, shared instruction files — are edited upstream. Repo changes go in `.backlog/` or `agent-loop-instructions.md`.
- Refinement labels and recommends; closing and reassigning issues stay with humans. The one exception is a verified-stale issue in a repository whose local file sets `stale-action: close`.
- Bucket-B work — synced-surface, credential-gated, open-decision, cross-repo, or a local sensitive path — is excluded by definition, never tagged `dev: agent`.

`issues` is the day-to-day workflow (ready queue, claim, link); this skill decides what earns the `dev: agent` label `issues ready --agent` and `agent-loop` key on, and which interview — `grill` or `product-grill` — each excluded issue needs next.
