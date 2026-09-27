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

**Arguments**: `$ARGUMENTS` — dispatch on the first word; default to `queue`. Modes: `setup`, `queue`, `refine [n | --all | --limit N | --backfill | --batch [N]]`, `assess <n> [--decision <draft-comment-file>]`, `rca [run-window]`.

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
# --json for machine output, --limit N, --include-refined to also list assessed issues,
# --grill for the needs: grill / needs: product-grill queues, highest priority first
```

Report the counts the script prints: ready, re-verify, conflicted, excluded, epics, skipped, backfill, the two grill queues, and un-refined (the work).

- **Re-verify** — `dev: agent` without `agent: refined`: tagged by something other than this skill and never verified against HEAD. `refine --all` does not walk this bucket, yet it is exactly what `agent-loop` consumes. Clear it before trusting the queue.
- **Backfill** — assessed issues that are missing what the current core rubric sets: a priority label, or the `needs:` label a grill-class bail requires. An epic already split into GitHub sub-issues owes no interview and is not listed. When the local file's `priority-title-prefixes` marker is set, `--json` suggests the priority a title prefix records. `refine --backfill` clears the bucket.
- **Skipped** — issues carrying a label in the local file's `auto-managed-labels` marker: opened and closed by a scheduled workflow. Never comment on one; a comment resets its `updatedAt` and delays the workflow's auto-close.

## Mode: `refine [n | --all | --limit N | --backfill | --batch [N]]`

Default refines the next un-refined issue; `--limit N` a batch; `--all` the whole un-refined bucket. Run the **re-verify** bucket through the same steps before (or alongside) `--all`. Every exclusion below also removes `dev: agent` when present — it and `agent-bail:` never coexist — and replaces any `agent-bail:` and `needs:` label left from an earlier assessment rather than adding to it. Sanity-check the rewrite on a handful (`assess <n>` or `--limit 5`) before a large sweep, since it edits issue bodies at scale. For each issue:

1. **Read it fully** — `gh issue view <N>` including comments, and the issue's `precheck.py` facts (see `--batch`): they settle the mechanical stale checks before any judgement. Use `rubric.py`’s `grill_context` to compare decision and refinement comments. Read a newer `grill-decision` as settling the earlier question before applying an open-decision exclusion; assess all other criteria normally. A provisional record preserves its blocking unknowns.
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

Keep every rewrite inside the scope the issue asked for, with confirmed interview decisions translated into Goal and Acceptance criteria and implementation checks grounded in the code, and tag `dev: agent` only on issues that pass every §1 criterion. **When torn between make-ready and exclude, exclude** — a false `dev: agent` costs a whole loop iteration; a false exclusion just waits for a human.

End each comment leaving a new interview gap with `Question for /grill: …` or `Question for /product-grill: …` on its own final line. A split recommendation creates children only under the confirmed handback below or separate human approval.

### `--batch [N]`

Refine the next `N` un-refined issues (default 25) through parallel read-only assessors and one reviewed application. Use it for any sweep larger than a handful. Start with a 5-issue batch and review every result, then run batches of about 25, and fold each batch's rubric notes into the local file before the next — every batch in practice surfaces rules that make the next one cleaner.

1. **Pre-check** the batch. This runs the mechanical checks once, deterministically, instead of in every assessor:

   ```bash
   python3 .codex/skills/backlog-refinement/scripts/precheck.py --unrefined --limit <N> --out <tmp>/precheck.json
   ```

2. **Assess in parallel.** Split the batch into groups of about five and start one read-only assessor per group with [`templates/assessor-prompt.md`](./templates/assessor-prompt.md), filling its `{ISSUES}`, `{PRECHECK_FILE}`, `{BASE}`, and `{OUT_FILE}` fields. Each writes a JSON plan. Assessors never mutate GitHub.
3. **Review.** Merge the group files into one plan, then preview it:

   ```bash
   python3 .codex/skills/backlog-refinement/scripts/apply-plan.py <tmp>/plan.json
   ```

   The preview validates the whole plan first and refuses it if anything is wrong: a label the repository does not have, a ready verdict carrying a bail, a stale verdict without a close reason. Read every ready body and every stale verdict yourself before applying; those are the two that change what the loop builds and what disappears from the backlog. Read every comment too: each is posted under your name, and assessors read untrusted issue text. Correct the plan, not the issues.

4. **Apply** with `--apply`. It mutates one issue at a time, applies the rubric's label hygiene, skips a comment already posted, honours `stale-action` and **Rewrite mode**, and records progress so a re-run resumes where it stopped.
5. **Retro.** Group the assessors' `rubric_note` fields. A note that recurs across groups is a rubric gap: edit `.backlog/refinement.local.md` (or record an upstream candidate) and add one learnings entry for the batch before starting the next.

Report each batch as a table: ready, stale (closed or recommended), human-reserved, and excluded by category, plus the new grill-queue entries and the questions they carry.

### `--backfill`

Walk the **backfill** bucket from `candidates.py` and apply only steps 2 and the `needs:` mapping: add the missing priority, and on a grill-class bail add the missing `needs:` label. Change no body, add no comment, and leave every other label alone — these issues were assessed under an older rubric and the assessment still stands.

Process sequentially, one `gh` mutation at a time. Summarize at the end: tagged ready, excluded by category, needs labels applied by kind, priorities set by tier, re-scoped.

## Mode: `assess <n> [--decision <draft-comment-file>]`

Dry-run one issue: the priority you would set, the §1 verdict, the §2 transformations that would apply, the `needs:` label on an exclusion, and the proposed rewritten body — without mutating anything. `--decision` supplies a draft interview comment as hypothetical decision input, not permission to write. Read its full text and marker; explain how it settles the question and what remains. Without a draft, use the strictly newer posted decision, if any. Produce the plan entry and the complete preview below, including any approved-in-principle children and proposed close.

## Interview handback

This path is entered after `grill` or `product-grill` confirms the interview summary. Refinement owns all mutations except the decision comments, which the interview posts.

1. **Assess each session issue with its draft decision.** Follow the [core outcome table](./core-rubric.md#interview-decisions). A permanent bail remains excluded even when the interview settled its question. Re-read current assignments and blockers; do not reassign anything. For a split, draft bounded children and assess each now; children are not automatically ready.
2. **Prepare one plan and preview.** Each existing entry uses the batch plan schema plus `decision_comment` (the exact draft comment). A don't-build entry uses `verdict: dont-build`, `close_reason: not planned`, no body, and no bail/needs/blocked additions. A split parent is `exclude` with `agent-bail: epic`, no needs label, and `children` as shown below. Show the decision comments, verdicts, exact label changes, full rewritten bodies, child titles/bodies/assessments and each close offer. Run `apply-plan.py <plan>` for validation; it is read-only by default. If `.backlog/refinement.local.md` is absent, return a comment-only preview and a setup pointer instead.
3. **Confirm once.** With local `grill-handback: ask` (also the default), ask the user to approve that combined preview. With `auto`, skip only this question. Record explicit confirmation for each proposed close even in auto mode. A summary confirmation is always required. If the user declines a close, leave it out of the application and offer only its decision comment; never pass a close flag on the strength of a setting, marker, or inferred agreement.
4. **Post, then apply.** The interview posts the approved decision comments, anchor first when several issues share a record. Bind the plan to those exact posted comments. Refinement applies it with `--apply --confirm-summary` and `--confirm-handback` when the preview was confirmed. Add `--confirm-close <n>` only for each explicitly approved close. A separate human-approved split outside an interview uses `--confirm-split <n>` instead. `auto` waives neither summary nor close flags. Process sequentially, preserving assignments and unrelated issues. On failure, report completed writes and resume the same plan; a changed plan needs a new preview and progress file.

A split child has a stable key, initial body, and complete assessment using the ordinary plan fields but without `number`, `children`, or `decision_comment`:

```json
{
  "key": "bounded-task",
  "title": "Implement the bounded task",
  "body": "Goal, acceptance criteria, source pointers and exclusions",
  "assessment": {
    "verdict": "exclude",
    "add_labels": ["agent-bail: spec-gap", "needs: grill"],
    "remove_labels": [],
    "comment": "Backlog refinement: one question remains.\nQuestion for /grill: Which input is authoritative?",
    "body": null,
    "close_reason": null
  }
}
```

The applier creates and links each child as a GitHub sub-issue, then applies its assessment before changing the parent. It preserves a `refinement-child` marker in rewritten child bodies so retries can recover already-created children. Keep keys stable when resuming. It changes no assignees and refuses a handback if the issue acquired an assignee after assessment. Existing assigned issues receive only an approved decision comment; leave their handback for their owner.

Done when every approved decision has a posted record and each plan entry has an applied result, or the user has a concrete partial-failure report. Do not hide a failed child creation behind a completed parent.

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
- Refinement labels and recommends; closing and reassigning issues stay with humans. Exceptions are a verified-stale issue when the local file sets `stale-action: close`, or a close explicitly confirmed for that issue in an interview handback. Auto handback never authorizes closing. Child creation requires a confirmed handback or separate human split approval; assignments and issues outside the session are untouched.
- Bucket-B work — synced-surface, credential-gated, open-decision, cross-repo, or a local sensitive path — is excluded by definition, never tagged `dev: agent`.

`issues` is the day-to-day workflow (ready queue, claim, link); this skill decides what earns the `dev: agent` label `issues ready --agent` and `agent-loop` key on, and which interview — `grill` or `product-grill` — each excluded issue needs next.
