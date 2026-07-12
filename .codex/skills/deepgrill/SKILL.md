---
name: deepgrill
description: High-fidelity pre-push Codex review chain. Use for complex or high-risk changes such as auth, crypto, secrets, data migrations, GitHub Actions, sync tooling, .codex/skills, large refactors, or when the user asks for a deep review.
---

# Deep Grill

## Context Window Check

Run this check before anything else. `deepgrill` is the most cache-hungry skill in the chain — it runs `refactorpass` (cleanup matrix) and then `grill deep` (six core independent review lanes, plus a conditional tenant-coupling lane). When subagents/delegation are available, the lanes run in parallel, each inheriting cache state from this session; when subagents are not available they run as serial local passes against the same context. Either way, if the current Codex session has already been heavily used for feature implementation, the lanes start with sharply reduced working windows and the whole chain runs slower and more expensively.

Assess honestly:

- Has this session been writing/editing the feature about to be reviewed? Long conversation, many file edits, dense planning?
- Is the conversation about to brush against compaction territory?

If either is yes, stop and tell the user:

> Your context is heavy from the implementation work. Start a new Codex session and run `deepgrill` there. `deepgrill` spawns up to seven review lanes and is the chain that benefits most from cache headroom. A fresh session makes the chain materially cheaper.

Do not proceed in the current session unless the user explicitly overrides.

## Chain

Run the full high-fidelity pre-push chain:

1. Execute the `refactorpass` workflow.
2. Execute the `grill` workflow in deep mode, including all six core independent review lanes from the `grill` skill and the conditional tenant-coupling lane when customer-variable behavior is present.
3. Stop before pushing unless the user explicitly asked you to push.

Deep grill is not a single generalized review. If the active Codex runtime permits subagents/delegation, use independent reviewers for every applicable lane. If subagents are unavailable or not permitted, run a separate local pass for every applicable lane and disclose the downgrade in the final output.

Invoking `deepgrill` is an explicit request to use independent subagents for the
six core review lanes, plus the conditional tenant-coupling lane when signaled,
whenever the active runtime exposes subagent/delegation tools. Do not require the
user to separately say "use subagents" before spawning those lane reviewers.

Every deep lane must use an adversarial stance: assume the diff contains
defects, search for the highest-impact failure modes first, and require code,
tests, or documented constraints to disprove each risk. Do not report guesses;
report only evidence-backed, actionable findings.

## Deep Triggers

Use this path when the change touches:

- `.codex/skills/**`, `scripts/sync*`, or `.github/workflows/**`
- authentication, authorization, crypto, secret handling, or sensitive data
- database schema, data shape, migrations, or serialization contracts
- more than roughly 20 files or 500 net lines
- an area with recurring incidents
- customer/tenant-variable behavior such as vendor integrations, per-tenant configuration, prompt/output generation, or data normalization

## Handoff

Read `.codex/REVIEW_WORKFLOW.md` and the consumer's instructions to determine
which cross-model path the developer selected.

When invoked as the final sub-skill inside `reviewit`, return the deepgrill
result directly to that orchestrator. Do not start local convergence, recommend
another `reviewit`, push, or emit a terminal workflow summary; `reviewit` owns
the hosted-path summary.

When `AGENT_LOOP_REVIEW_ENGINE=codex` or the local convergence path is selected,
return control without pushing:

If `$AGENT_LOOP_REVIEW_OUTCOME_FILE` is set and this pass commits fixes, write
exactly `material` when any fix is substantive or `minor` when every fix is
low-risk and non-behavioral. Do not create the file when no fix commit is made.
Missing classification for a commit defaults to material.

```text
Codex deepgrill pass complete.
Review depth: <deep with independent subagents | deep local multi-pass fallback>
Next:
  Run a fresh local Claude review on this HEAD.
  Classify committed fixes as material or minor.
  Restart at Codex only when either reviewer commits a material fix.
  Push after one full Codex-then-Claude round produces no material fixes.
```

Do not recommend or invoke `reviewit` on the local convergence path. The current
process or outer agent-loop wrapper owns the next pass and final summary.

When the hosted fallback path is selected, tell the user:

```text
Deep pre-push review complete.
Review depth: <deep with independent subagents | deep local multi-pass fallback>
Next:
  git push
  gh pr create --title "..." --body "..."
  reviewit <pr-number> deep

Run `reviewit <pr-number> deep` in a FRESH Codex session.
The current session has absorbed refactorpass output, all deep-grill review
lanes, and any fix commits — cache pressure is high. `reviewit deep` runs
up to four review iterations and a final deepgrill against the PR; each
step needs cache headroom. A fresh session for `reviewit deep` makes the
full chain materially cheaper.
```

If no path is declared, present both choices and ask the developer to select
based on whether local Claude Code is available. Do not assume a license.
