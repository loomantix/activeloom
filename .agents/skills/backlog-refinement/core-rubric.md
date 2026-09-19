# Agent-readiness core rubric

> **Criteria true of any repository** for deciding whether a GitHub issue is _agent-intelligible_ — completable end-to-end by an autonomous `agent-loop` session without a human in the loop — and for routing everything else. `backlog-refinement` (which prepares the backlog) and `agent-loop` (which consumes it) both read this file **together with** the repository's own `.backlog/refinement.local.md`. Where the two disagree, the local file wins.
>
> This file is synced from upstream and overwritten on every sync. A rule that names this repository's paths, labels, or past incidents belongs in `.backlog/refinement.local.md`; a rule that would hold in any repository belongs here, proposed upstream.
>
> **Core rubric version: 3.** (v3: the rubric split into this synced core plus a repo-owned local file; added priority tiers and `needs:` routing labels. v2: added the §2 _re-verify pre-tagged queue_ transformation.)

## The two questions (the continuous-improvement contract)

When an iteration **bails without a PR** or a PR **fails for a non-code reason**, the RCA asks exactly two questions, in order:

1. **"What could we have done differently to make automation succeed _on this issue_?"**
   If there is an answer — the issue _was_ doable and prep or the loop failed for an avoidable reason — the fix is a **make-ready transformation** (§2) or a **loop fix** (`agent-loop-instructions.md`). The issue can re-enter the queue once fixed. → outcome **PREVENTABLE**.

2. **If the honest answer is "nothing" — the issue is inherently not agent-completable as written —** ask: **"How do we hone the rubric so an issue of this _shape_ is recognized and excluded at refinement time, before it ever costs a loop iteration?"** The fix is a sharpened **disqualifier** (§3). → outcome **INHERENT**.

Every RCA terminates in one concrete edit: a §2 transformation, a §3 disqualifier, or a line in `agent-loop-instructions.md` — made locally, or proposed upstream when it holds for any repository.

## Label model

| Label                    | Meaning                                                                                                                    | Who sets it                                                                  |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `dev: agent`             | **Ready for the loop.** The loop's pickup signal.                                                                          | Refinement after a passing assessment                                        |
| `agent: refined`         | Refinement has assessed this issue, whether it ended ready or excluded. Prevents re-processing; measures backlog coverage. | Refinement on every issue it assesses                                        |
| `agent-bail: <category>` | **Assessed and excluded**, with a reason from §3.                                                                          | Refinement at prep time, **or** the inner loop agent at bail time (§4)       |
| `needs: grill`           | Excluded until a technical interview settles _how_ to build it. Run `grill` on it, then re-refine.                         | Refinement, alongside a grill-class `agent-bail:` label (see _Needs labels_) |
| `needs: product-grill`   | Excluded until a product interview settles _whether_ and _what_ to build. Run `product-grill` on it, then re-refine.       | Refinement, alongside a grill-class `agent-bail:` label (see _Needs labels_) |
| priority (one of four)   | Impact and urgency — independent of agent-readiness. Label names come from the local file's `priority-labels` marker.      | Refinement, only where no priority is already set (see _Priority_)           |

`dev: agent` and `agent-bail: *` are mutually exclusive. A `dev: agent` issue that the loop later bails on loses `dev: agent` and gains the `agent-bail:` reason — _that removal is itself a high-signal RCA trigger_. `needs:` labels only ever accompany an `agent-bail:` label; an issue that re-refines to `dev: agent` loses its `needs:` label in the same edit.

### Priority

Every assessed issue carries exactly one priority. The tiers, highest first:

| Tier     | Use when                                                                                                          |
| -------- | ----------------------------------------------------------------------------------------------------------------- |
| critical | A security vulnerability, data loss or corruption, or production down / a core flow broken for everyone. Do next. |
| high     | A user-facing bug or regression, a compliance or contractual obligation, or work that is blocking other work.     |
| medium   | A worthwhile improvement, or a bug with a reasonable workaround.                                                  |
| low      | Polish, nice-to-have, or speculative work.                                                                        |

- **An existing priority stands.** Refinement sets a priority only where none of the repository's priority labels is present. An issue carrying two is reported as conflicted for a human; refinement leaves both in place.
- **Judge impact and urgency, not buildability.** An `agent-bail:` issue can be `critical`, and a trivially agent-ready one `low`.
- **The label names and any sharper definitions are local.** The local file's `<!-- priority-labels: … -->` marker lists the four label names highest first; its _Priority definitions_ section may replace the table above with the repository's own wording. An empty marker turns priority-setting off.

### Needs labels

A `needs:` label names the one interview that would turn an excluded issue into buildable work, so a human can filter the backlog by the next skill to run. Apply it only for the grill-class bail categories:

| `agent-bail:` category | `needs:` label                                                                                                                                      |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `open-decision`        | `needs: product-grill` when the open question is about users, value, scope, or policy toward them; `needs: grill` when it is about how to build it. |
| `spec-gap`             | `needs: product-grill` when the missing piece is what the user needs from it; `needs: grill` when it is what done looks like technically.           |
| `epic`                 | `needs: grill` to break it into bounded children; `needs: product-grill` instead when the epic's premise itself is undecided.                       |

Choose one — the interview that has to happen **first**. A product question outranks a technical one, because its answer can make the technical question moot. Every other bail category is unblocked by something other than an interview (a credential, another repository, a human review, an upstream change, a close) and gets no `needs:` label.

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

| Transformation                  | Trigger                                      | Action                                                                                                                                                                                                                      |
| ------------------------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Verify-against-HEAD**         | Always, at tag time                          | Check the described bug/behavior against the freshly fetched integration branch. Already fixed → `agent-bail: stale`, recommend close, do not tag. Partially shipped → re-scope the body to the **residual** and re-assess. |
| **Re-verify pre-tagged queue**  | A `dev: agent` issue lacks `agent: refined`  | It was tagged by something else and never verified against HEAD, yet it is exactly what the loop consumes. Run the full refine over the `candidates.py` **re-verify** bucket before trusting the queue.                     |
| **External-dep availability**   | The issue names a package/service dependency | Confirm it is published and consumable from this repo. If not → `status: blocked` + `agent-bail: cross-repo`, do not tag.                                                                                                   |
| **Add acceptance criteria**     | §1.2 fails                                   | Derive a concrete verifiable check: which test file, which command, which observable.                                                                                                                                       |
| **Add file pointers**           | §1.6 fails                                   | Grep the repo; list the files/symbols the agent will touch.                                                                                                                                                                 |
| **Add out-of-scope guardrails** | Scope is fuzzy at the edges                  | List what is _not_ in scope so the agent does not wander.                                                                                                                                                                   |
| **Split**                       | §1.1 fails (multi-change)                    | Propose child issues; tag only the bounded ones. The parent gets `agent-bail: epic` and its `needs:` label.                                                                                                                 |

## §3 — Disqualifiers (INHERENT — exclude, never tag `dev: agent`)

Each maps to an `agent-bail:` label. Refinement applies these at prep time; the loop applies them at bail time. The local file adds the repository's own — at minimum, its sensitive paths.

### Bucket A — preventable by prep (a loop bail here means **refinement missed something** → improve §2)

- **`agent-bail: stale`** — work already shipped, or the issue is out of date vs HEAD. Recommend close.
- **`agent-bail: spec-gap`** — under-specified; the agent cannot determine done-ness. _Refinement should have added acceptance criteria and file pointers, or excluded it when the gap needs a human's knowledge._ Grill-class.
- **`agent-bail: loop-mechanics`** — the issue _was_ agent-shaped but the run hit an avoidable mechanical failure (sibling-revert race, env/tooling gap, prompt ambiguity, transient infra). _The fix lives in `agent-loop-instructions.md` or the upstream script, not the rubric._

### Bucket B — inherent (correct exclusions)

- **`agent-bail: cross-repo`** — needs another repository, an unpublished/private dependency, or an unmerged upstream change.
- **`agent-bail: open-decision`** — an unresolved design, product, or policy question. Grill-class.
- **`agent-bail: credential-gate`** — requires a credential-gated build or action (store submission, a physical device, secret/ruleset/key mutation).
- **`agent-bail: synced-surface`** — acceptance requires editing a file synced from upstream; the change belongs upstream.
- **`agent-bail: epic`** — a tracking/coordination issue, not a bounded task. Grill-class.

### Workflow-auto-managed issues (skipped entirely — not even labelled)

Issues **opened _and_ closed by a scheduled workflow** — a nightly digest, say — are never engineering tasks, and a refinement comment on one resets its `updatedAt`, delaying the auto-close. `candidates.py` routes any issue carrying a label from the local file's `<!-- auto-managed-labels: … -->` marker to a `skipped` bucket that never enters the queue.

## §4 — Bail-time self-classification (loop side)

When an `agent-loop` iteration exits **without a PR**, the inner agent, before exiting:

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
