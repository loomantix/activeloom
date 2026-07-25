# Model notes — authoring prompts for the current default model

This file is synced from the upstream repo to every consumer repo. Edits in a consumer repo will be overwritten on next sync — make changes upstream.

**Current default model: Claude Opus 5.** Last reviewed against Anthropic's published guidance on 2026-07-24.

Everything under `.claude/skills/` and `.claude/agents/` is a prompt. A skill body, an agent definition, and the instruction string a skill tells Claude to pass to `Agent(...)` are all read by the model as instructions, so a phrasing that helped on one model generation can actively hurt on the next. Opus 5 runs existing Opus 4.8-era prompts well out of the box, but a handful of patterns that were _good practice_ on 4.x now either suppress findings or burn tokens. This file records those deltas so skill and agent authors do not have to re-derive them.

Primary sources (public Anthropic docs):

- <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>
- <https://platform.claude.com/docs/en/build-with-claude/effort>

When a new default model ships, re-read those pages and update this file rather than patching individual skills ad hoc.

---

## 1. A finder must not be its own filter

**This is the highest-impact item for this repo,** because the review chain is what most of these skills do. It is also **not primarily a model-version rule** — it is an architecture rule that holds on any model, with a model-specific reason stacked on top. Both are stated below, deliberately separated, because conflating them is how this ended up wrong once already.

**The architectural reason (model-independent).** A finding suppressed inside the finder's own prompt is unrecoverable — the caller never learns it existed, so it cannot be reviewed, dismissed, or logged. A finding that arrives with a low score costs one line and can be cut in a moment. The asymmetry is total, so the three jobs belong in three places:

1. **Find** — report every issue you believe is real, with no cutoff of your own.
2. **Score** — attach a confidence to each, ideally judged independently of whoever found it.
3. **Cut** — apply the threshold at the orchestrator, where every lens is visible at once and each claim can be checked against the diff.

**A cutoff value is fine; a cutoff _inside the finder_ is not.** Anthropic's own official `code-review` plugin is the reference implementation and still filters at 80 — but it gets there by running finder agents that return everything, then a **separate** scorer agent per issue, and only then applying the ≥ 80 cut in the orchestrator. Same number, opposite placement. That is the distinction to preserve when you add a review lens.

**The model-specific reason (Opus 5, stacked on the above).** Opus 5 follows a suppression instruction literally, while reviewing with high precision _and_ high recall — its additional findings are mostly real rather than false positives. So on Opus 5 a self-suppression instruction is close to a pure loss: the model obeys, reports less, and gives up little false-positive noise in exchange. This makes the architecture rule urgent rather than merely tidy, but the architecture rule is what to cite when refactoring a prompt.

**Scope carefully — the model-specific reason travels less far than the architecture.** The architecture applies to every reviewer in the chain. The Opus 5 empirical claim applies only where Opus 5 actually runs, which is not everywhere:

- Agents pinned to another model (this repo's three `.claude/agents/` definitions pin `model: sonnet`, inherited verbatim from the official `feature-dev` plugin) are governed by the architecture, not by the Opus 5 measurement. Don't cite an Opus 5 release note as evidence about a Sonnet-pinned agent.
- Other model families are governed by neither. `/codex-review` deliberately asks Codex for "only high-confidence material findings", and that stays: Codex's job is a terse cross-check against a Claude pass that already reported everything. Don't retune another vendor's prompt from a Claude release note — measure first.

## 2. Do not add verification scaffolding

Opus 5 verifies its own work without being asked. Instructions like these now cause **over-verification** — extra tool calls and tokens with no quality gain:

- "Include a final verification step for any non-trivial task."
- "Use a subagent to verify the result."
- "Double-check your answer before responding."
- "Re-verify before reporting."

Remove them from skills and agent definitions, and do not add them to new ones. If an existing skill's phase exists only to re-check the previous phase, that phase is now dead weight.

**The exception that matters:** this is about _generic_ self-review scaffolding, not about domain facts that must be checked against reality. "Confirm the row count before designing a migration", "`git rev-parse --abbrev-ref HEAD` before trusting a test result", "assert the installed definition, not the migration you wrote" — those are checks against external state the model cannot know, and they stay. The distinction is whether the instruction tells the model to re-read its own output (drop it) or to go look at something outside itself (keep it).

## 3. Cap subagent delegation explicitly

Opus 5 delegates to subagents more readily than prior models. That pays off on genuinely independent, sizeable tracks of work and wastes money on everything else. Skills in this repo that spawn agents should state their ceiling:

- Delegate only for large, genuinely independent, parallelizable work.
- Do not delegate what the session can finish in a handful of tool calls.
- **Never** spawn a subagent to verify or double-check the session's own work (see §2).
- If one agent can do it, use one. Keep spawn counts low.

`/grill`'s agent matrix is a **ceiling, not a floor** — pick the lenses whose signals actually appear in the diff. The "two to five agents is typical in deep mode" line in that skill is a real budget, not a suggestion. If a change feels big enough to want more lenses than the matrix offers, that is a signal to escalate to `/deepgrill`, not to invent extra agents.

## 4. Prompt for length — effort will not do it for you

Two separate behaviors, both longer on Opus 5 than on prior models:

- **Conversational output.** Per-message output during agentic work runs longer, and the model narrates what it is about to do more readily.
- **Written deliverables.** Files it writes to disk — reports, handoff notes, PR bodies, summaries — are longer too.

The `effort` parameter controls how much the model _thinks_, not how much it _says_. Lowering effort does not reliably shorten a response. If a skill's output has a length that matters (a PR body, a findings table, a status line), state the length in the prompt. The existing "Under 300 words" ceilings in the `Agent(...)` prompts in `/grill` are exactly the right pattern — keep them, and add them to new agent prompts.

For documents Claude authors, calibrate rather than truncate:

> Match the length of written documents to what the task needs: cover the substance, but do not pad with filler sections, redundant summaries, or boilerplate.

## 5. Scope discipline is already handled — do not re-add it

Opus 5 can widen a task's scope on its own judgment, and Claude Code's own system prompt already instructs against that (deliver the requested scope, make routine judgment calls, flag concerns in a sentence and continue). The same is true of correction narration and of finishing the whole task.

Do not restate those rules in skills or agent definitions. Duplicating them adds prompt noise and, worse, invites drift when the harness wording changes. Skills should carry _their own_ scope boundaries — what this skill does and does not do — not generic model-behavior instructions.

## 6. Effort levels

`high` is the API and Claude Code default, and it is the right starting point on Opus 5. Adjust from there against real results:

| Level    | Use for                                                                                                        |
| -------- | -------------------------------------------------------------------------------------------------------------- |
| `low`    | Cheap mechanical stages, simple lookups, high-volume subagents. Quality holds far better than on prior models. |
| `medium` | Balanced default for routine agentic work where you have checked that quality holds.                           |
| `high`   | Default. Complex reasoning, difficult coding, agentic tasks.                                                   |
| `xhigh`  | Demanding coding and long-horizon agentic work. Set a large `max_tokens` (start ~64k) so it has room to think. |
| `max`    | Reserve for genuinely frontier problems where a task justifies unconstrained spend.                            |

**If you carried an effort default over from Opus 4.7 or 4.8, it is stale.** Those models' guidance was "start at `xhigh` for coding and agentic work"; Opus 5's is "start at `high` and use `low`/`medium` liberally as the primary cost and latency control". Re-check rather than reusing the old setting.

Two practical notes: review accuracy holds up at lower effort on Opus 5, which makes a cheap fast pass genuinely useful ahead of a thorough one; and effort shapes the rendered prompt, so changing it mid-conversation invalidates prompt caching — pick a level per workload, not per turn.

## 7. Keep thinking enabled

Thinking is on by default and cannot be disabled at `xhigh` or `max` effort. Prefer **low effort with thinking on** over disabling thinking — it performs better at comparable cost. With thinking disabled, two artifacts can leak into visible output: a tool call written as prose instead of a structured call (which then never runs, and poisons later turns in an agentic loop), and stray internal XML tags.

Never write a rule telling the model not to think or not to reason. That phrasing measurably increases tag leakage.

## 8. A bigger context window is not a reason to review in the authoring session

Opus 5 carries a 1M-token context window as both default and maximum, and holds its instruction-following and reasoning quality across it. It is tempting to read that as retiring the pre-flight gates that send `/grill` and `/deepgrill` to a fresh session. **It does not, and this is the one delta in this file that runs the opposite way to "the new model needs less scaffolding".**

Two separate reasons, and the second is the load-bearing one:

- **Cost.** Reviewing in the authoring session drags the whole implementation history through every pass. In practice that reached 700–800k tokens, re-read on each turn of a multi-pass chain, and `/deepgrill` fans that inheritance out across up to six sub-agents.
- **Review quality.** That history was almost never useful to the review, and sometimes actively unhelpful. A session that just wrote the code re-reads its own diff already holding the rationale that produced it — anchored on why the code is right rather than looking for why it is wrong. That is the opposite of the fresh-eyes stance the adversarial pass exists to provide. No context window fixes it, because the problem is what the context contains, not whether it fits.

**The general lesson for this file: capacity to hold context is not evidence the context is worth holding.** When a new model relaxes a limit, check whether the guardrail was actually about the limit before removing it. Some guardrails were about relevance, and those get _stronger_ as the limit rises — a bigger window means more irrelevant history survives to pollute the pass.

---

## Checklist when adding or editing a skill or agent here

- [ ] No cutoff inside a finder's own prompt — find, score, and cut are three separate places (§1).
- [ ] Any model-specific claim used to justify an edit is scoped to agents that actually run that model — check the `model:` pin (§1).
- [ ] No "double-check", "re-verify", or "verify with a subagent" scaffolding (§2).
- [ ] Any external-state check kept is genuinely about the world, not about re-reading the model's own output (§2).
- [ ] Agent spawning has a stated ceiling, and no agent exists only to check another agent's work (§3).
- [ ] Every `Agent(...)` prompt states an output length (§4).
- [ ] No generic model-behavior boilerplate about scope, corrections, or task completion (§5).
- [ ] Effort overrides, if any, are justified against Opus 5's scale rather than inherited from 4.x (§6).
- [ ] No pre-flight fresh-session gate weakened on the grounds that the context window grew (§8).

## Cross-references

- [REVIEW_WORKFLOW.md](REVIEW_WORKFLOW.md) — the canonical AI review chain these notes constrain.
- [`skills/grill/SKILL.md`](skills/grill/SKILL.md) — the reference implementation of §1 and §3 (unfiltered agents, filtering aggregator, bounded matrix).
- [`agents/code-reviewer.md`](agents/code-reviewer.md) — the reference implementation of §1 on the agent side.
