# Skill authoring — structuring a document the model will follow

This file is synced from the upstream repo to every consumer repo. Edits in a consumer repo will be overwritten on next sync — make changes upstream.

[MODEL_NOTES.md](MODEL_NOTES.md) covers **what the current model does differently** — the phrasings that suppress findings or burn tokens on this generation. This file covers **how to structure the document** — where material sits, when a step is done, what to cut. The two are independent: a skill can be perfectly calibrated to Opus 5 and still be an unfollowable wall of text.

Read both before writing or editing anything under `.claude/skills/` or `.claude/agents/`.

Derived from the `writing-for-agents` skill in <https://github.com/mattpocock/skills> (MIT) — see [NOTICE](../NOTICE). The vocabulary below is theirs; the examples are ours.

---

## 1. The two loads, and the sync multiplier

Every document and every pointer to one spends one of two budgets:

- **Context load** — always-loaded material: a skill's `description`, a line in `CLAUDE.md`, anything in the window every turn. It costs tokens and attention whether or not it ever fires.
- **Cognitive load** — the cost on the human: knowing which skills exist and when to reach for each. Not a cost to minimize. It is the price of human agency — spend it where human judgment matters, remove it where it does not.

**This repo has a third consideration the general rule does not: everything on the sync surface is multiplied by every consumer repo.** A sloppy 40-word description is not 40 wasted tokens, it is 40 wasted tokens per turn per session per repo, indefinitely, and the cost is paid by developers who never chose the skill. A skill nobody in a given repo invokes still bills that repo every turn.

That multiplier is the strongest argument in this file. When a pruning decision feels marginal in isolation, price it at fleet scale and it usually stops being marginal. It also cuts the other way: material genuinely needed everywhere earns its place more easily here than in a single-repo skill.

## 2. Invocation — who can reach the skill

Two settings, trading the two loads against each other:

| Frontmatter                      | Who can invoke      | Description in context? |
| -------------------------------- | ------------------- | ----------------------- |
| _(default — omit both fields)_   | model and user      | Always                  |
| `disable-model-invocation: true` | user only (`/name`) | No                      |
| `user-invocable: false`          | model only          | Always                  |

`disable-model-invocation: true` is the one that buys context back: the description is dropped from the window entirely and the body loads only when the human types the name. Typing `/name` keeps working — the field removes the model's reach, never the human's.

**The test: could the model usefully start this on its own, or must a human decide to?** A skill that only ever runs because a person decided to run it — a deploy runbook, a state migration, a hardware workflow — is paying permanent context rent for a trigger that never fires.

**One caveat before converting anything.** Whether a user-invoked skill can still be reached by _another_ skill's body ("then run `/refactorpass`") is not documented. Treat it as unsafe: if any other skill invokes it by name, leave it model-invoked. Grep the skill bodies before flipping the field — a silently broken chain step is far more expensive than the tokens saved.

For a skill that stays model-invoked, the `description` is a **context pointer**, and its wording — not its target — decides how reliably the skill fires. A pointer does two jobs: say what the material is, and name the distinct branches that should trigger it. Front-load the words that do the triggering, keep one trigger per branch (synonyms renaming a single branch are one branch written twice), and cut identity the body already carries.

## 3. The information hierarchy

A skill is built from **steps** (ordered actions) and **reference** (rules and facts consulted on demand). They mix freely — an all-steps runbook, an all-reference rulebook, or both. The decision is where each piece sits on a ladder ranked by how immediately the model needs it:

1. **In-file step** — the primary tier: what the model does, in order.
2. **In-file reference** — consulted on demand. Often a legitimately flat peer set (every rule of a review on one rung). That is fine, not a smell.
3. **Disclosed reference** — a separate file behind a pointer, loaded only when the pointer fires. [`references/local-review-ledger.md`](references/local-review-ledger.md) is this repo's worked example: several skills need the diff-delivery rules, none of them need those rules inline.

Push too little down and the top bloats; push too much and you hide what the model actually needs. **Branching is the cleanest test: inline what every path needs, disclose what only some paths reach.**

The failure mode this prevents is specific. When a document has steps, in-file reference that should have been disclosed _buries_ them, and attending to any given step becomes a coin flip. This is a reliability lever, not just a tidiness one.

**Co-location** is the within-file companion. The ladder decides how far down a piece sits; co-location decides what sits beside it. Keep a concept's definition, rules, and caveats under one heading rather than scattered, so reading one part brings its neighbors with it. (Distinct from duplication: duplication repeats one meaning in two places, scattering fragments one meaning across many.)

**Sprawl** is a document simply too long, even when every line is live and unique. Attention thins across the excess and every extra line is one more to keep true. The cure is the ladder: disclose reference, split by branch or sequence so each path carries only what it needs.

## 4. Completion criteria

Every step ends on a condition that tells the model the work is done. Two properties make it a lever.

**Clarity** — can the model tell done from not-done? A vague bound ("understanding reached", "the code is clean") invites **premature completion**: ending the step early, attention already sliding to the steps visible after it. Sharpen the bound first; that is local and cheap. Only if it is irreducibly fuzzy _and_ you observe the rush should you hide the later steps by splitting the sequence — and hiding only works across a real context boundary (a handoff, a subagent dispatch). An inline call leaves the later steps in context and clears nothing.

**Demand** — how much the criterion requires. "Every modified file accounted for" forces work that "produce a change list" does not. Demand drives the digging the model does _within_ a step rather than as its own step, and it is not step-bound: "every rule applied" binds a body of flat reference exactly as "every step done" binds a sequence. That is how an all-reference document still carries an exhaustiveness bar.

The strongest criteria are both checkable and exhaustive. `Under 300 words` and `every finding posted inline before any fix is pushed` are checkable; `be thorough` is neither.

## 5. Leading words, and prompting the positive

A **leading word** is a compact concept the model already holds from pretraining, repeated as a token rather than restated as a sentence. Used consistently it accumulates a distributed definition and anchors a whole region of behavior in very few tokens. This repo already runs on several: a review lens, the frontier, convergence mode, the ledger.

It anchors twice — in the body the model reaches for the same behavior every time the word appears; in a pointer, shared vocabulary across your prompts, docs, and code makes the model reach the material more reliably. Coining your own works if you define it, but an invented word recruits no priors: you pay in definition tokens what a pretrained word gives free. Reach for an existing word first.

Hunt for passages that collapse into one. "Fast, deterministic, low-overhead" is a _tight_ loop. "A test that can actually catch this bug" goes _red_ — a fuzzy gate becomes a binary observable state.

**Negation is the failure mode beside this lever.** Steering by prohibition drags the forbidden behavior into context and makes it _more_ available, not less. The ban half-reads as an instruction to do the thing. State the target behavior instead, so the banned one is never spoken. A prohibition earns its place only as a hard guardrail you cannot phrase positively — and even then, pair it with the positive target so attention lands on what to do.

## 6. Pruning

- **Single source of truth.** One authoritative place per meaning, so changing the behavior is a one-place edit. Duplication costs maintenance and tokens, and inflates a meaning's apparent rank on the ladder. (The accidental inverse of a leading word, which repeats a token on purpose and never the meaning.)
- **The environment is a source of truth too** — `package.json` scripts, the sync manifest, `--help` output, the directory layout. A document restating it is a **cache**, earning its load only when the lookup is expensive. Cache what the model cannot find by looking: the unwritten convention, the reason behind a choice, the gotcha no config confesses. Leave one-command lookups to the environment, where they cannot go stale.
- **Relevance.** Does each line still bear on what the document does? Lines lose relevance by never bearing on the task, or by going stale as the world moves. Without a pruning discipline the default fate is **sediment** — stale layers that settle because adding feels safe and removing feels risky, until you have to core down through them to find what is live.
- **No-ops.** An instruction the model already obeys by default pays load to say nothing. The test — does this change behavior versus the default? — is model-relative, not reader-relative, and it is settled by running the document rather than by debate. When a sentence fails, delete the sentence rather than trimming words from it. The test grades leading words too: a word too weak to beat the default is a no-op, and the fix is a stronger word, not a different technique.

MODEL_NOTES §5 is the standing example of a whole class of no-op: scope discipline, correction narration, and finish-the-task are already in the harness prompt. Restating them in a skill adds noise and invites drift.

## 7. When to split

Splitting one document into two spends one of the two loads, so the cut has to earn it.

- **By sequence** — split a run of steps when the later ones tempt the model to rush the one in front of it. The reverse holds as a warning: merging two sequences exposes each step to what follows, inviting premature completion.
- **By invocation** — split off a model-invoked skill when it has a distinct trigger word you actually use, or when another skill must reach it. You pay context load for a new always-loaded description, so that independent reach has to be worth it.

## 8. Before you open the PR

Two gates run in CI on anything under `.claude/skills/` or `.claude/agents/`:

- `.claude/lint-skill-content.py` — blocks pipe-to-shell, credential reads, env exfiltration, defanged URLs, and any URL whose host is not on its allowlist. Adding a host is a deliberate review decision, not a formality.
- `npx prettier --check .` — markdown included. Run it before pushing; a formatting-only CI failure wastes a full cycle.

## Checklist when adding or editing a skill

- [ ] Invocation is deliberate: the skill is model-invoked because the model can usefully start it, or `disable-model-invocation: true` because only a human can (§2).
- [ ] Before flipping a skill to user-invoked, no other skill's body invokes it by name (§2).
- [ ] The `description` front-loads its triggering words and carries one trigger per branch (§2).
- [ ] Reference only some branches need is disclosed behind a pointer, not inlined among the steps (§3).
- [ ] Each concept's rules and caveats sit under one heading rather than scattered (§3).
- [ ] Every step ends on a criterion the model can check, and the demanding ones say what "all" means (§4).
- [ ] Guardrails are phrased as the target behavior; a bare prohibition is a last resort (§5).
- [ ] No line restates what the environment already answers in one lookup (§6).
- [ ] No line restates what the harness prompt already enforces (§6, MODEL_NOTES §5).
- [ ] Skill-content lint and prettier both pass locally (§8).

## Cross-references

- [MODEL_NOTES.md](MODEL_NOTES.md) — model-generation deltas; read alongside this file.
- [REVIEW_WORKFLOW.md](REVIEW_WORKFLOW.md) — the review chain most of these skills implement.
- [`references/local-review-ledger.md`](references/local-review-ledger.md) — the worked example of disclosed reference (§3).
