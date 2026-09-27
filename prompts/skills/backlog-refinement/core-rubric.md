# Agent-readiness core rubric

> **Criteria true of any repository** for deciding whether a GitHub issue is _agent-intelligible_ — completable end-to-end by an autonomous `<<INVOKE>>agent-loop` session without a human in the loop — and for routing everything else. `<<INVOKE>>backlog-refinement` (which prepares the backlog) and `<<INVOKE>>agent-loop` (which consumes it) both read this file **together with** the repository's own `.backlog/refinement.local.md`. Where the two disagree, the local file wins.
>
> This file is synced from upstream and overwritten on every sync. A rule that names this repository's paths, labels, or past incidents belongs in `.backlog/refinement.local.md`; a rule that would hold in any repository belongs here, proposed upstream.
>
> **Core rubric version: 4.** (v4: bail precedence, the stale-action setting, verify-against-HEAD sub-checks, the child-closure check, decomposed epics without a `needs:` label, and rules for assigned issues, split recommendations, orphaned decisions, `[Decision]`/`[Spike]` titles, index issues, and body drift. v3: the rubric split into this synced core plus a repo-owned local file; added priority tiers and `needs:` routing labels. v2: added the §2 _re-verify pre-tagged queue_ transformation.)

## The two questions (the continuous-improvement contract)

When an iteration **bails without a PR** or a PR **fails for a non-code reason**, the RCA asks exactly two questions, in order:

1. **"What could we have done differently to make automation succeed _on this issue_?"**
   If there is an answer — the issue _was_ doable and prep or the loop failed for an avoidable reason — the fix is a **make-ready transformation** (§2) or a **loop fix** (`agent-loop-instructions.md`). The issue can re-enter the queue once fixed. → outcome **PREVENTABLE**.

2. **If the honest answer is "nothing" — the issue is inherently not agent-completable as written —** ask: **"How do we hone the rubric so an issue of this _shape_ is recognized and excluded at refinement time, before it ever costs a loop iteration?"** The fix is a sharpened **disqualifier** (§3). → outcome **INHERENT**.

Every RCA terminates in one concrete edit: a §2 transformation, a §3 disqualifier, or a line in `agent-loop-instructions.md` — made locally, or proposed upstream when it holds for any repository.

## Label model

| Label                    | Meaning                                                                                                                         | Who sets it                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `dev: agent`             | **Ready for the loop.** The loop's pickup signal.                                                                               | Refinement after a passing assessment                                            |
| `agent: refined`         | Refinement has assessed this issue, whether it ended ready or excluded. Prevents re-processing; measures backlog coverage.       | Refinement on every issue it assesses                                            |
| `agent-bail: <category>` | **Assessed and excluded**, with a reason from §3.                                                                               | Refinement at prep time, **or** the inner loop agent at bail time (§4)           |
| `needs: grill`           | Excluded until a technical interview settles _how_ to build it. Run `<<INVOKE>>grill` on it, then re-refine.                    | Refinement, alongside a grill-class `agent-bail:` label (see _Needs labels_)     |
| `needs: product-grill`   | Excluded until a product interview settles _whether_ and _what_ to build. Run `<<INVOKE>>product-grill` on it, then re-refine. | Refinement, alongside a grill-class `agent-bail:` label (see _Needs labels_)     |
| priority (one of four)   | Impact and urgency — independent of agent-readiness. Label names come from the local file's `priority-labels` marker.          | Refinement, only where no priority is already set (see _Priority_)               |

`dev: agent` and `agent-bail: *` are mutually exclusive. A `dev: agent` issue that the loop later bails on loses `dev: agent` and gains the `agent-bail:` reason — _that removal is itself a high-signal RCA trigger_. `needs:` labels only ever accompany an `agent-bail:` label; an issue that re-refines to `dev: agent` loses its `needs:` label in the same edit.

### Priority

Every assessed issue carries exactly one priority. The tiers, highest first:

| Tier     | Use when                                                                                                            |
| -------- | ------------------------------------------------------------------------------------------------------------------- |
| critical | A security vulnerability, data loss or corruption, or production down / a core flow broken for everyone. Do next.  |
| high     | A user-facing bug or regression, a compliance or contractual obligation, or work that is blocking other work.      |
| medium   | A worthwhile improvement, or a bug with a reasonable workaround.                                                   |
| low      | Polish, nice-to-have, or speculative work.                                                                          |

- **An existing priority stands.** Refinement sets a priority only where none of the repository's priority labels is present. An issue carrying two is reported as conflicted for a human; refinement leaves both in place.
- **Judge impact and urgency, not buildability.** An `agent-bail:` issue can be `critical`, and a trivially agent-ready one `low`.
- **The label names and any sharper definitions are local.** The local file's `<!-- priority-labels: … -->` marker lists the four label names highest first; its _Priority definitions_ section may replace the table above with the repository's own wording. An empty marker turns priority-setting off.

### Needs labels

A `needs:` label names the one interview that would turn an excluded issue into buildable work, so a human can filter the backlog by the next skill to run. Apply it only for the grill-class bail categories:

| `agent-bail:` category | `needs:` label                                                                                                                                            |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `open-decision`        | `needs: product-grill` when the open question is about users, value, scope, or policy toward them; `needs: grill` when it is about how to build it.        |
| `spec-gap`             | `needs: product-grill` when the missing piece is what the user needs from it; `needs: grill` when it is what done looks like technically.                 |
| `epic`                 | Only an epic not yet split into child issues: `needs: grill` to break it into bounded children; `needs: product-grill` instead when its premise is undecided. |

Choose one — the interview that has to happen **first**. A product question outranks a technical one, because its answer can make the technical question moot. Every other bail category is unblocked by something other than an interview (a credential, another repository, a human review, an upstream change, a close) and gets no `needs:` label — except that an open decision keeps its `needs:` label when a permanent category wins precedence (§3, _Choosing the category_).

An epic already split into child issues — listed in its body or a comment, or as GitHub sub-issues — gets no `needs:` label. No interview is owed; the children are what refinement assesses. Name them in the comment.

## §1 — What makes an issue agent-intelligible

An issue is `dev: agent`-ready only if **all** of these hold, plus any criteria the local file adds.

1. **Bounded scope** — one coherent change, touching a small number of packages/modules. Not an epic, not "and also."
2. **Verifiable success** — acceptance is checkable by a deterministic signal the agent can run in the loop: a test, a typecheck, a lint or doc gate, a CI gate, or an observable behavior. "Looks better" is not verifiable.
3. **Self-contained in this repo** — no dependency on an unpublished/private package, an unmerged upstream PR, or a change in a sibling repository.
4. **No open decision** — the issue states _what_ to do, not "should we A or B?". Product, design, and policy forks are human calls.
5. **Current** — the described problem still reproduces against the integration branch HEAD. **This is the most-violated criterion** — see `stale` and verify-against-HEAD.
6. **File-anchored** — the body points at the concrete files/symbols/lines where the work happens (refinement adds these if missing).
7. **Inside the safe envelope** — does not require editing synced-from-upstream files, a credential-gated build, secret/ruleset mutation, or a non-trivial change to a sensitive path the local file names.

## §2 — Make-ready transformations (PREVENTABLE fixes)

When an issue is _shaped_ like agent work but fails §1, refinement transforms it rather than excluding it. The local file may add transformations of its own.

| Transformation                 | Trigger                                     | Action                                                                                                                                                                                                                  |
| ------------------------------ | ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Verify-against-HEAD**        | Always, at tag time                         | Check the described bug/behavior against the freshly fetched integration branch, running every sub-check below. Already fixed → `agent-bail: stale` and the stale action (§3), do not tag. Partially shipped → re-scope the body to the **residual** and re-assess. |
| **Re-verify pre-tagged queue** | A `dev: agent` issue lacks `agent: refined` | It was tagged by something else and never verified against HEAD, yet it is exactly what the loop consumes. Run the full refine over the `candidates.py` **re-verify** bucket before trusting the queue.                 |
| **External-dep availability**  | The issue names a package/service dependency | Confirm it is published and consumable from this repo. If not → `status: blocked` + `agent-bail: cross-repo`, do not tag.                                                                                               |
| **Add acceptance criteria**    | §1.2 fails                                  | Derive a concrete verifiable check: which test file, which command, which observable.                                                                                                                                   |
| **Add file pointers**          | §1.6 fails                                  | Grep the repo; list the files/symbols the agent will touch.                                                                                                                                                             |
| **Add out-of-scope guardrails** | Scope is fuzzy at the edges                | List what is _not_ in scope so the agent does not wander.                                                                                                                                                               |
| **Child-closure check**        | The work is split into children (a task list, "broken out into", sub-issues), before choosing `epic` | Check every child's state. All closed → verify the parent's own acceptance criteria against HEAD: nothing left is `stale`, a residual is re-assessed as the issue. Only open children keep it an `epic`. |
| **Split**                      | §1.1 fails (multi-change)                   | Propose child issues; tag only the bounded ones. The parent gets `agent-bail: epic` and its `needs:` label.                                                                                                             |
| **Split recommendation**       | A residual holds independent parts, and one would be agent-ready alone | Keep the dominant bail on the issue and name the proposed child and its scope in the comment. Create child issues only with a human's go-ahead.                                                                |

### Verify-against-HEAD sub-checks

Run all four on every verify-against-HEAD.

- **(a) Re-run the check yourself.** Counts, version pins, and "X still exists" claims go stale — in the body and in every comment. Run the grep or command rather than trusting either. A close condition stated only in a comment is still the close condition.
- **(b) Cited anchors still exist.** Every named file, symbol, test block, or `file:line` is still present at HEAD.
- **(c) Side-item closure.** Search merged PRs for the issue number, in their bodies and closing references. A merged PR that names the issue without closing it may have shipped the work.
- **(d) Cited foundation merged.** A PR the issue's design stands on actually merged, and was not closed unmerged.

## §3 — Disqualifiers (INHERENT — exclude, never tag `dev: agent`)

Each maps to an `agent-bail:` label. Refinement applies these at prep time; the loop applies them at bail time. The local file adds the repository's own — at minimum, its sensitive paths.

### Bucket A — preventable by prep (a loop bail here means **refinement missed something** → improve §2)

- **`agent-bail: stale`** — work already shipped, or the issue is out of date vs HEAD. Also stale: an index or coordination issue once every piece of work it tracks has landed or moved to its own issue — name any tracked item with no home yet before closing it; and an issue whose residual a recorded deferral moved into another open issue — say where it went, and comment on the receiving issue so it inherits the items. Take the stale action below.
- **`agent-bail: spec-gap`** — under-specified; the agent cannot determine done-ness. _Refinement should have added acceptance criteria and file pointers, or excluded it when the gap needs a human's knowledge._ Grill-class.
- **`agent-bail: loop-mechanics`** — the issue _was_ agent-shaped but the run hit an avoidable mechanical failure (sibling-revert race, env/tooling gap, prompt ambiguity, transient infra). _The fix lives in `agent-loop-instructions.md` or the upstream script, not the rubric._

### Bucket B — inherent (correct exclusions)

- **`agent-bail: cross-repo`** — needs another repository, an unpublished/private dependency, or an unmerged upstream change.
- **`agent-bail: open-decision`** — an unresolved design, product, or policy question. Grill-class.
- **`agent-bail: credential-gate`** — requires a credential-gated build or action (store submission, a physical device, secret/ruleset/key mutation).
- **`agent-bail: synced-surface`** — acceptance requires editing a file synced from upstream; the change belongs upstream.
- **`agent-bail: epic`** — a tracking/coordination issue, not a bounded task. Grill-class.

### Choosing the category

§4 and refinement both label one dominant category. When several apply:

1. **A permanent category beats a grill-class one.** No amount of prep removes the local file's sensitive-path category, `credential-gate`, `cross-repo`, or `synced-surface`, so each wins over `open-decision`, `spec-gap`, or `epic`. Among the permanent ones, prefer them in that order.
2. **An open decision keeps its `needs:` label** even when a permanent category wins, so the question still reaches a grill queue instead of hiding behind the bail.
3. **Between two grill-class reasons,** take the one whose interview has to happen first.
4. Name every other category that applies in the comment.

### Assessment rules

- **`[Decision]` and `[Spike]` titles** are presumed `open-decision` — once you have confirmed the decision was not already made in the repository's decision records, specs, or code at HEAD. A made decision is `stale`. A spike that needs dashboards, billing, vendor accounts, or real third-party data is `credential-gate` on sight, however bounded it looks.
- **Assigned issues** are reserved by a human: apply `agent: refined` and no bail label, and never `dev: agent`. Say in the comment which category the issue _would_ get if unassigned, so an unassignment does not drop a sensitive residual back into the pool unmarked.
- **Orphaned decisions.** An open decision found only in a comment, belonging to no issue, is named in the refinement comment as needing its own `[Decision]` issue.
- **Drift in any body.** When an issue's header contradicts its own history — a retitled priority, a superseded outcome, a scope later comments changed — flag it in the comment.

### The stale action

The local file's `<!-- stale-action: recommend | close -->` marker sets what refinement does with a verified-stale issue. It defaults to `recommend`.

- **`recommend`** — apply `agent: refined` + `agent-bail: stale`, comment with the evidence (commit, PR, or `file:line`), and recommend close. A human closes it.
- **`close`** — post the evidence comment first, apply the same labels, then close the issue as `completed` when the work shipped or `not planned` when it is obsolete or superseded. Remove any state label whose premise the close resolves, such as `status: blocked`. Every other close — a duplicate, a won't-fix — stays a recommendation.

### Workflow-auto-managed issues (skipped entirely — not even labelled)

Issues **opened _and_ closed by a scheduled workflow** — a nightly digest, say — are never engineering tasks, and a refinement comment on one resets its `updatedAt`, delaying the auto-close. `candidates.py` routes any issue carrying a label from the local file's `<!-- auto-managed-labels: … -->` marker to a `skipped` bucket that never enters the queue.

## §4 — Bail-time self-classification (loop side)

When an `<<INVOKE>>agent-loop` iteration exits **without a PR**, the inner agent, before exiting:

1. Picks the **dominant** `agent-bail:` category from §3 or the local file (one label; secondary reasons go in the comment).
2. Requests `agent-bail: <category>` + `agent: refined`, and removal of `dev: agent`.
3. Ends its proposed issue comment with a structured RCA stub the aggregation pass parses:

   ```
   <!-- agent-loop-rca
   category: <agent-bail category>
   bucket: A|B
   preventable: yes|no
   what-could-differ: <one line — the §1/§2 answer, or "nothing: inherent">
   rubric-impact: <which §2 transformation or §3 disqualifier this sharpens>
   -->
   ```

The loop does not set `needs:` or priority labels; the next refinement pass adds them through the **backfill** bucket.

## §5 — Agent-ready issue-body template (rewrite target)

A rewrite quotes only the user-facing wording the original report specifies. It never invents new copy — labels, messages, onboarding text; that stays out of scope as a product call.

```markdown
> **Refined for agent-loop** (core rubric v<N>, local rubric v<M>, <date>). Original report preserved below.

## Goal

<one sentence: the change>

## Acceptance criteria

- [ ] <deterministic, agent-runnable check>
- [ ] <typecheck / lint clean>
- [ ] <relevant tests green>

## Files / entry points

- `path/to/file:LINE` — <what changes>

## Out of scope

- <explicit non-goals so the agent does not wander>

---

> ### Original report
>
> <verbatim original body>
```
