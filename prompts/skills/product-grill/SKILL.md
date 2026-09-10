---
name: product-grill
description: A relentless product interview that stress-tests a plan or design from the user's point of view until nothing about the user is silently assumed.<<DESC_TRIGGER>>
<<FM_EXTRAS>>
---

# <<INVOKE>>product-grill — interview until the user is understood

Interview the user until you reach a shared understanding of who this is for, what problem it solves for them, and what changes in their experience. The person you are interviewing is usually a product manager, designer, or someone else who works with engineers but does not write the code. **This skill does not write code and does not implement anything.** It ends when the questions run out, and the user decides what happens next.

This is the product-side sibling of `<<INVOKE>>grill`. `<<INVOKE>>grill` settles how a thing gets built; this settles what it must do for the people who use it, and why it is worth building at all.

## The design tree

Map the problem as a **design tree**: every decision branches into the decisions that hang off it. Grow it from the people outward. Every tree has at least these branches:

- **Who** — the users it is for, and the people it touches indirectly: support, admins, users who never opt in.
- **Problem** — what they cannot do, or do badly, today — and the evidence it matters: requests, support volume, usage data, or a hunch named as a hunch.
- **Outcome** — what success looks like and how anyone would know: the change in behavior or the number that moves.
- **Experience** — the path through it, including first use, empty states, errors, undo, and what a user who ignores the feature sees.
- **Boundaries** — what is deliberately left out, and what existing users lose or have to relearn.
- **Rollout** — who gets it first, how they hear about it, and what would make you pull it back.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled — the questions you can ask _now_ without guessing at answers you have not heard yet.

Ask the whole frontier in one round. Number each question and give your recommended answer:

```
<<Q_BULLET>>**Q1** — **<short question title>**: <the question, including any options worth choosing between>

<<REC_BULLET>><your recommended answer, and why>
```

Then wait. Each round of answers reshapes the tree — settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round.

**A question whose answer depends on another question still open in this round belongs to a later round.** Asking it now forces the user to guess at their own unmade decision.

Always recommend an answer, argued from what the end user would experience. A bare question makes the user do all the work; a recommendation gives them something to push against, and disagreement is faster than composition.

## Plain language

Name things the way the interviewee names them. Every question, recommendation, and reported fact is written for someone who has never opened the codebase: describe what a user would see or be able to do, and leave file names, function names, and engineering acronyms out unless the interviewee used them first.

"Today, cancelling an order cancels every item in it" — not the name of the handler that does it.

## Facts are your job, decisions are theirs

Never ask the user for something you could look up. If a frontier question needs a fact — how the product behaves today, what already exists, what an issue thread decided — **go and get it**, then report it in plain language.

Do not block the round on a lookup. A running exploration is just an unsettled prerequisite: questions downstream of it wait, the rest of the frontier goes out now.

<<SUBAGENT_GUIDANCE>>

The _product decisions_ are always the user's. Put each one to them and wait.

**Technical decisions go to engineering.** When a branch reaches a choice the interviewee is not placed to make — data model, architecture, performance budget — add it to a running list of **questions for engineering** and keep working the product branches. Where the technical answer would change the product, ask the product half in plain terms: "Keeping deleted items restorable is extra work — is recovering a mistake worth a later launch?"

## Ubiquitous language

When the user uses a term that is vague or overloaded, stop and pin it down before building on top of it. "You said _customer_ — the company that pays, or each person who logs in? They get different emails."

When the user describes how the product works today, check whether the product agrees. A contradiction surfaced now is worth more than the same contradiction found after launch. Say so plainly: "You described partial refunds, but today a refund always covers the whole order — is changing that part of the plan?"

Where a repo already carries a glossary, product docs, or ADRs for the area, read them first and use their words.

## Done

The session is done when **the frontier is empty** — every branch of the tree visited, nothing about the user left silently assumed.

Then summarize for a reader who was not in the room: who it is for, the problem and its evidence, how success will be measured, the decisions made, the alternatives rejected and why, what is out of scope, and the questions for engineering. Keep it under 500 words — it is a record of decisions, not a spec.

**Do not act on it until the user confirms the understanding is shared.** When they do, the natural next steps are `<<INVOKE>>grill` for an engineer to work the technical branches from this summary, or `<<INVOKE>>issues` to file it.

## Scope

- No code, no branches, no commits, no PRs.
- No technical design — those questions are listed for engineering, not answered here.
- If the frontier empties after two or three questions, say so. The idea was already clear, and there is nothing here to earn a session.

---

Adapted from the `grilling` skill in [mattpocock/skills](https://github.com/mattpocock/skills) (MIT) — see [NOTICE](../../../NOTICE).
