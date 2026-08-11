---
name: grill
description: A relentless interview that stress-tests a plan or design until nothing is silently assumed.
disable-model-invocation: true
argument-hint: (optional) the idea, plan, or decision to stress-test
---

# /grill — interview until nothing is assumed

Interview the user until you reach a shared understanding of what is being built and why. **This skill does not write code and does not implement anything.** It ends when the questions run out, and the user decides what happens next.

To grill is to interview a person, not to review a diff. The adversarial code review that once held this name is now `/critique` and `/deepcritique`; this skill is the opposite end of the work — sharpening an idea _before_ there is a diff.

## The design tree

Map the problem as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled — the questions you can ask _now_ without guessing at answers you have not heard yet.

Ask the whole frontier in one round. Number each question and give your recommended answer:

```
❓ **Q1** — **<short question title>**: <the question, including any options worth choosing between>

➡️ <your recommended answer, and why>
```

Then wait. Each round of answers reshapes the tree — settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round.

**A question whose answer depends on another question still open in this round belongs to a later round.** Asking it now forces the user to guess at their own unmade decision.

Always recommend an answer. A bare question makes the user do all the work; a recommendation gives them something to push against, and disagreement is faster than composition.

## Facts are your job, decisions are theirs

Never ask the user for something you could look up. If a frontier question needs a fact from the repo, the git history, a live config, an issue thread, or a package registry, **go and get it**.

Do not block the round on a lookup. A running exploration is just an unsettled prerequisite: questions downstream of it wait, the rest of the frontier goes out now.

Delegate a lookup only when it is genuinely independent and too large for a handful of tool calls — a sweep across many files or repos. If one agent can do it, use one; state a word ceiling on what it returns. Most lookups here are two greps and a read, and should stay inline.

The _decisions_ are always the user's. Put each one to them and wait.

## Ubiquitous language

When the user uses a term that is vague, overloaded, or in tension with how the code already uses it, stop and pin it down before building on top of it. "You said _claim_ — do you mean the submission batch or the individual service line? Those diverge later."

When the user states how something currently works, check whether the code agrees. A contradiction surfaced now is worth more than the same contradiction found in review. Say so plainly: "Your code cancels the whole batch, but you just described partial cancellation — which is right?"

Where a repo already carries a glossary or ADRs for the area, read them first and use their words.

## Done

The session is done when **the frontier is empty** — every branch of the tree visited, nothing left silently assumed.

Then summarize: the decisions made, the alternatives rejected and why, and anything the user explicitly ruled out of scope. Keep it under 400 words — it is a record of decisions, not a spec.

**Do not act on it until the user confirms the understanding is shared.** When they do, the natural next steps are `/task-packet` for a single bounded change, `/issues` to file it, or `/backlog-refinement` if it needs breaking into an agent-ready queue.

## Scope

- No code, no branches, no commits, no PRs.
- No implementation planning past the point where the decision is settled — the aim is that nothing is assumed, not that everything is specified.
- If the frontier empties after two or three questions, say so. The idea was already clear, and there is nothing here to earn a session.

---

Adapted from the `grilling` skill in [mattpocock/skills](https://github.com/mattpocock/skills) (MIT) — see [NOTICE](../../../NOTICE).
