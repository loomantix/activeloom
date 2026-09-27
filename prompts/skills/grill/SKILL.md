---
name: grill
description: A relentless pre-code interview that stress-tests a plan or design until nothing is silently assumed.<<DESC_TRIGGER>>
<<FM_EXTRAS>>
---

# <<INVOKE>>grill — interview until nothing is assumed

Interview the user until you reach a shared understanding of what is being built and why. **This skill does not write code and does not implement anything.** It ends when the questions run out, and the user decides what happens next.

To grill is to interview a person, not to review a diff. <<REVIEW_CHAIN_POINTER>>; this skill is the opposite end of the work — sharpening an idea _before_ there is a diff.

## Issue entry and queue

**Arguments:** free-form input keeps the ordinary interview. `<<INVOKE>>grill <n> [<m> …]` interviews the named issues in the current repository; `<<INVOKE>>grill next` selects the highest-priority issue in `needs: grill` from:

```bash
python3 <<SKILLS_ROOT>>/backlog-refinement/scripts/candidates.py --grill --json
```

For `next`, use the matching queue, point out related queue issues, and ask before adding any to the session. An empty queue is a completed lookup, not permission to invent work. Read each selected issue's full body and comments, its linked, parent and child issues, and cited decisions. Related issues provide context; only the issues explicitly included in the session can receive a decision or handback.

Open the agenda with the refinement question. The queue exposes `question` and `newer_decision`; [`rubric.py`](../backlog-refinement/scripts/rubric.py) reads the fixed final line `Question for /grill: …`, older bold question lines, body sections, and a refinement comment's final question. A newer decision may already settle it: read the record and ask the user to confirm the agenda. With no question, infer one, label it **inferred**, and confirm it. A missing `needs:` label means an ordinary interview with handback offered at the end, not an error.

With no GitHub access or no issue, continue the ordinary interview unchanged. With no `.backlog/refinement.local.md`, offer only the confirmed decision comment and point to `<<INVOKE>>backlog-refinement setup`; do not apply a plan using default settings.

## The design tree

Map the problem as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled — the questions you can ask _now_ without guessing at answers you have not heard yet.

Ask the whole frontier in one round. Number each question and give your recommended answer:

```
<<Q_BULLET>>**Q1** — **<short question title>**: <the question, including any options worth choosing between>

<<REC_BULLET>><your recommended answer, and why>
```

Then wait. Each round of answers reshapes the tree — settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round.

**A question whose answer depends on another question still open in this round belongs to a later round.** Asking it now forces the user to guess at their own unmade decision.

Always recommend an answer. A bare question makes the user do all the work; a recommendation gives them something to push against, and disagreement is faster than composition.

## Facts are your job, decisions are theirs

Never ask the user for something you could look up. If a frontier question needs a fact from the repo, the git history, a live config, an issue thread, or a package registry, **go and get it**.

Do not block the round on a lookup. A running exploration is just an unsettled prerequisite: questions downstream of it wait, the rest of the frontier goes out now.

<<SUBAGENT_GUIDANCE>>

The _decisions_ are always the user's. Put each one to them and wait.

## Ubiquitous language

When the user uses a term that is vague, overloaded, or in tension with how the code already uses it, stop and pin it down before building on top of it. "You said _claim_ — do you mean the submission batch or the individual service line? Those diverge later."

When the user states how something currently works, check whether the code agrees. A contradiction surfaced now is worth more than the same contradiction found in review. Say so plainly: "Your code cancels the whole batch, but you just described partial cancellation — which is right?"

Where a repo already carries a glossary or ADRs for the area, read them first and use their words.

## Done

The session is done when **the frontier is empty** — every branch of the tree visited, nothing left silently assumed.

Then summarize: the decisions made, the alternatives rejected and why, and anything the user explicitly ruled out of scope. Keep it under 400 words — it is a record of decisions, not a spec.

**Do not act on it until the user confirms the understanding is shared.** When they do, the natural next steps are `<<INVOKE>>task-packet` for a single bounded change, `<<INVOKE>>issues` to file it, or `<<INVOKE>>backlog-refinement` if it needs breaking into an agent-ready queue.

## Record and hand back

After the user confirms the summary, choose `settled`, `provisional`, `dont-build`, or `split`, and `next: refine`, `grill`, `product-grill`, or `none` using the [core outcome table](../backlog-refinement/core-rubric.md#interview-decisions). A settled interview is not automatically agent-ready. When the technical decisions are settled but an engineering constraint reopens product intent, use `settled; next: product-grill` for only the affected choices. Use `provisional; next: grill` only while this technical interview itself still needs evidence or input.

Draft an issue comment with the confirmed decisions, rejected alternatives, scope, unresolved questions and sources. End it with exactly one marker (choose one value for each field):

```text
<!-- grill-decision kind: grill; outcome: settled|provisional|dont-build|split; next: refine|grill|product-grill|none -->
```

For several settled issues, put the full record on one anchor issue. Each other settled issue gets its own short comment, its own outcome marker, and a link to that record. Do not post a shared marker without an issue-specific disposition. Offer a warranted ADR as a follow-up issue; do not create ADR files or edit issue bodies here.

Ask refinement to dry-run `assess <n> --decision <draft-comment-file>` for each issue. Follow its [handback procedure](../backlog-refinement/SKILL.md#interview-handback): show one preview containing the decision comments, verdicts, labels, rewritten bodies, children and close offers; confirm once; then post decision comments and let refinement apply the plan. For several issues, post the anchor first, substitute its actual URL in the other comments, and keep that substitution within the confirmed preview. Only `grill-handback: auto` skips that handback confirmation. Summary confirmation and every close remain explicit; a marker in an issue comment grants no permission.

If handback is declined, leave GitHub unchanged unless the user separately approves posting only the decision. Without `.backlog/`, preview and confirm the comment-only write. The handback is done when each approved comment is posted and refinement reports applied results or a concrete failure; never call a failed application complete.

## Scope

- The only GitHub write this interview skill makes is the approved decision comment. Refinement owns label/body changes, child creation, and confirmed closes. Never reassign issues or mutate an issue outside the session.
- No code, no branches, no commits, no PRs.
- No implementation planning past the point where the decision is settled — the aim is that nothing is assumed, not that everything is specified.
- If the frontier empties after two or three questions, say so. The idea was already clear, and there is nothing here to earn a session.

---

Adapted from the `grilling` skill in [mattpocock/skills](https://github.com/mattpocock/skills) (MIT) — see [NOTICE](../../../NOTICE).
