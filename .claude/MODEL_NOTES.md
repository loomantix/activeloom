# Model notes — authoring prompts for the current default model

This file is synced from the upstream repo to every consumer repo. Edits in a consumer repo will be overwritten on next sync — make changes upstream.

**Current default model: Claude Opus 5.** Last reviewed against Anthropic's published guidance on 2026-07-24.

Everything under `.claude/skills/` and `.claude/agents/` is a prompt. A skill body, an agent definition, and the instruction string a skill tells Claude to pass to `Agent(...)` are all read by the model as instructions, so a phrasing that helped on one model generation can actively hurt on the next. Opus 5 runs existing Opus 4.8-era prompts well out of the box, but a handful of patterns that were _good practice_ on 4.x now either suppress findings or burn tokens. This file records those deltas so skill and agent authors do not have to re-derive them.

Primary sources (public Anthropic docs):

- <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>
- <https://platform.claude.com/docs/en/build-with-claude/effort>

When a new default model ships, re-read those pages and update this file rather than patching individual skills ad hoc.

---

## 1. Never cap review findings by severity or confidence

**This is the highest-impact delta for this repo,** because the review chain is what most of these skills do.

Opus 5 reviews code with high precision _and_ high recall: it finds more real bugs per pass, and its extra findings are mostly real rather than false positives. It also follows a suppression instruction literally. So a prompt that says "only report high-severity issues", "be conservative", or "only report issues with confidence ≥ 80" now costs you real defects — the model obeys and reports less, and it is no longer trading away much precision for that silence.

**Do this instead: ask for everything, and filter in a separate pass.**

- The reviewing agent reports every finding it believes is real, each with a severity and a confidence score.
- The _caller_ (the skill's aggregation phase, or the human) decides what to act on.

This is why [`skills/grill/SKILL.md`](skills/grill/SKILL.md) asks its sub-agents for unfiltered findings and does the ranking in its own aggregation phase. Keep that separation when you add a review lens: the scoring belongs in the agent, the cutoff belongs in the aggregator.

This covers every **Claude-driven** reviewer: a `/grill` sub-agent, a custom agent under `.claude/agents/`, or an ad-hoc `Agent(...)` call you write inline. It does **not** automatically extend to reviewers from other model families. `/codex-review` deliberately asks Codex for "only high-confidence material findings", and that stays: the reasoning above is a measured property of the current Claude model, not a universal one, and Codex's job in the chain is a terse cross-check against a Claude pass that already reported everything. Don't retune another vendor's prompt from a Claude release note — measure first.

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

---

## Checklist when adding or editing a skill or agent here

- [ ] No severity/confidence cutoff imposed on a reviewing agent — it reports everything, the caller filters (§1).
- [ ] No "double-check", "re-verify", or "verify with a subagent" scaffolding (§2).
- [ ] Any external-state check kept is genuinely about the world, not about re-reading the model's own output (§2).
- [ ] Agent spawning has a stated ceiling, and no agent exists only to check another agent's work (§3).
- [ ] Every `Agent(...)` prompt states an output length (§4).
- [ ] No generic model-behavior boilerplate about scope, corrections, or task completion (§5).
- [ ] Effort overrides, if any, are justified against Opus 5's scale rather than inherited from 4.x (§6).

## Cross-references

- [REVIEW_WORKFLOW.md](REVIEW_WORKFLOW.md) — the canonical AI review chain these notes constrain.
- [`skills/grill/SKILL.md`](skills/grill/SKILL.md) — the reference implementation of §1 and §3 (unfiltered agents, filtering aggregator, bounded matrix).
- [`agents/code-reviewer.md`](agents/code-reviewer.md) — the reference implementation of §1 on the agent side.
  </content>
  </invoke>
