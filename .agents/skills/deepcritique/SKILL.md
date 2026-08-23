---
name: deepcritique
description: High-fidelity PR-first review chain that opens or reuses a draft PR, records verified findings inline before fixes, and runs critique deep — preceded by refactorpass on this engine's first pass only. Rounds 3+ run in convergence mode. Runs only when the review tier resolved to Deep; hands a Lean changeset back to critique.
---

# Deep Critique

## Context Window Check

Run this check before anything else. `deepcritique` is the most cache-hungry skill in the chain — it runs `refactorpass` (cleanup matrix) and then `critique deep` (six core independent review lanes, plus a conditional tenant-coupling lane). When subagents/delegation are available, the lanes run in parallel, each inheriting cache state from this session; when subagents are not available they run as serial local passes against the same context. Either way, if the current Codex session has already been heavily used for feature implementation, the lanes start with sharply reduced working windows and the whole chain runs slower and more expensively.

Assess honestly:

- Has this session been writing/editing the feature about to be reviewed? Long conversation, many file edits, dense planning?
- Is the conversation about to brush against compaction territory?

If either is yes, stop and tell the user:

> Your context is heavy from the implementation work. Start a new Codex session and run `deepcritique` there. `deepcritique` spawns up to seven review lanes and is the chain that benefits most from cache headroom. A fresh session makes the chain materially cheaper.

Do not proceed in the current session unless the user explicitly overrides.

## PR-First Preflight

Load `.agents/references/local-review-ledger.md` and follow it throughout this
pass. Take an optional PR number from the invocation.

Require a clean, committed feature branch. Reuse the open PR whose head is the
current branch. If none exists, push the branch with a normal push and open a
draft PR before invoking any review lane. Verify the local HEAD, remote branch,
and PR head SHA match. Record the PR number and load all prior review threads,
including resolved and outdated threads.

Resolve this engine's round number per the ledger: `$AGENT_LOOP_REVIEW_ROUND`
when the runner set it, otherwise resolve the active runtime as `gemini` or
`antigravity` and use one past the count of `local-review-pass:v3` and
`local-review-complete:v3` markers naming that engine. Use `codex` only when
Codex is actually running the pass. Rounds
1–2 are adversarial; round 3 and later are convergence rounds. State which
applies before invoking a lane.

## Chain

The chain gets cheaper as it repeats, deliberately. Cleanup runs once; the
adversarial stance holds for two rounds and then gives way to landing the change.

1. Search the PR for `local-review-refactor:v1 engine=<active-engine>`. If it is absent and
   this is an adversarial round, execute `refactorpass <pr-number>` against the
   draft PR. If it is present, or this is a convergence round, skip cleanup
   entirely and report `refactor pass: already spent at <sha>` or
   `refactor pass: skipped (convergence round)`.
2. Reload the PR head and review ledger.
3. Execute `critique <pr-number> deep`, passing the resolved round so the lane picks
   the matching stance. An adversarial round runs all six core independent review
   lanes and the conditional tenant-coupling lane when customer-variable behavior
   is present. A convergence round runs only the code reviewer, silent failure
   hunter, and security reviewer when its signal is present, and changes the PR
   only for a blocking defect. `critique` owns those rules; do not restate or relax
   them here.
4. Require every confirmed finding to have been posted inline before its fix,
   replied to after push, and resolved.

## Pinned Review Base

Resolve the review base exactly once before `refactorpass`. When
`$AGENT_LOOP_REVIEW_BASE_SHA` is set, use that value. Otherwise use the exact
base SHA supplied by the caller, or resolve the requested base ref once after
any explicitly requested fetch. Verify the value with
`git rev-parse --verify '<sha>^{commit}'` and retain the resulting full object
ID for the entire pass.

Resolve the reviewed head SHA, changed-file list, and diff stat once for the
initial `refactorpass` packet. Reuse that packet unchanged while its head is
current. If `refactorpass` moves the PR head, end that packet and build a new
immutable review packet from the same pinned base and the resulting full head SHA,
including a fresh changed-file list and diff stat; reuse the new packet
unchanged for `critique` and every lane. Give each packet the literal
`<review-base-sha>..<reviewed-head-sha>` range; do not use a mutable `HEAD`
token inside it. No lane may re-resolve `@{u}`, a default branch, or a
remote-tracking ref independently. Report the pinned base plus each reviewed
head in the handoff so the Claude reviewer can reconstruct the ranges.

Keep the packet as the byte-identical prefix of every spawned review prompt and
append only the lane lens and exact file scope. Spawn with no inherited
conversation history (`fork_turns="none"`) when the runtime supports that
choice. The ledger governs scoped diff reads, bounded output, and PR-ledger
deduplication; do not paste the whole diff or the implementation conversation
into lane prompts.

Deep critique is not a single generalized review. If the active Codex runtime permits subagents/delegation, use independent reviewers for every applicable lane. If subagents are unavailable or not permitted, run a separate local pass for every applicable lane and disclose the downgrade in the final output.

Invoking `deepcritique` is an explicit request to use independent subagents for the
six core review lanes, plus the conditional tenant-coupling lane when signaled,
whenever the active runtime exposes subagent/delegation tools. Do not require the
user to separately say "use subagents" before spawning those lane reviewers.

Every deep lane must use an adversarial stance: assume the diff contains
defects, search for the highest-impact failure modes first, and require code,
tests, or documented constraints to disprove each risk. Do not report guesses;
report only evidence-backed, actionable findings.

## Tier Gate

This lane runs only when the review tier resolved to Deep. The triggers and the
evidence rules are defined once, in
[`../../REVIEW_WORKFLOW.md`](../../REVIEW_WORKFLOW.md) under "Review Tier" —
this skill does not carry its own list, and a repo-local list never overrides
that one. **Lean is the default; Deep is the exception you justify.**

Resolve the effective `local-review-tier:v1` marker under the ledger's
authenticated, forward-only transition rule. If no accepted marker exists,
classify against the workflow doc's triggers and post the marker before starting
a lane. A pass that exits on the docs/config-only skip never needs a tier.

**If the tier is Lean, do not run this chain.** Report the resolved tier and
hand the changeset to `critique <pr-number>`, which owns the Lean lane set.
Continue here only on a resolved Deep tier or an explicit human request, which
is trigger 6 and is recorded as one. Typing this skill's name does not select
the deep path.

## Handoff

Read `.agents/REVIEW_WORKFLOW.md` and the consumer's instructions to determine
which cross-model path the developer selected.

When invoked as the final sub-skill inside `reviewit`, return the deepcritique
result directly to that orchestrator. Do not start local convergence, recommend
another `reviewit`, push, or emit a terminal workflow summary; `reviewit` owns
the hosted-path summary.

When `$AGENT_LOOP_REVIEW_ENGINE` is set, or this pass is part of a review relay,
return control after pushing reviewed fixes and completing their PR threads:

If `$AGENT_LOOP_REVIEW_RESULT_FILE` is set, always create the v3 structured
result after the final lane. For `clean` or `changed`, call the ledger helper's
`write-result`; inside agent-loop omit thread and transition files so the helper
fetches and derives them. Use `minor` or `material` classification when the head
moved. For `blocked`, put the safe blocker in an owner-only regular file and
call `write-blocked-result`.
The outer wrapper validates the observed transition and posts the canonical
attestation; this skill must not post a pass/completion marker itself.

```text
Codex deepcritique pass complete.
PR: #<pr-number>
Reviewed head: <sha>
Round: <n> (<adversarial | convergence>)
Refactor pass: <ran | already spent at <sha> | skipped (convergence round) | docs-config skip>
Review depth: <deep with independent subagents | deep local multi-pass fallback>
Next:
  Run each declared reviewer that has not attested this head, against
  <review-base-sha>. Read all prior local-review threads before reviewing.
  Classify committed fixes as material or minor.
  A fix invalidates only the attestations naming the superseded head; an engine
  that already attested this head does not re-run.
  Mark ready only after verify-coverage passes at the exact head, that round
  produced no material fix, and every local-review thread is resolved.
```

A convergence round that found no blocking defect ends the loop. Say so and name
the repository's ship step; do not report the remaining rounds as owed.

Do not invoke `reviewit` from inside this skill; hosted review is a separate
lane the caller runs when it is useful, not a side effect of this pass. The
current process or outer agent-loop wrapper owns the next pass and final
summary.

When the caller asked for the hosted lane as the next step, tell the user:

```text
Deep PR review complete.
PR: #<pr-number>
Review depth: <deep with independent subagents | deep local multi-pass fallback>
Next:
  reviewit <pr-number> deep

Run `reviewit <pr-number> deep` in a FRESH Codex session.
The current session has absorbed refactorpass output, all deep-critique review
lanes, and any fix commits — cache pressure is high. `reviewit deep` runs
up to four review iterations and a final deepcritique against the PR; each
step needs cache headroom. A fresh session for `reviewit deep` makes the
full chain materially cheaper.
```

If no path is declared, present both choices and ask the developer to select
based on whether local Claude Code is available. Do not assume a license.
