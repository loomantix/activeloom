---
name: deepcritique
description: High-fidelity PR-first Codex review chain that opens or reuses a draft PR, records verified findings inline before fixes, and runs critique deep — preceded by refactorpass on this engine's first pass only. Rounds 3+ run in convergence mode. Runs only when the review tier resolved to Deep; hands a Lean changeset back to critique.
---

# Deep Critique

## Mandatory Claude Launcher Boundary

Read and apply this boundary before any preflight, lane, or cross-engine
transition. Whenever this skill or its caller starts the independent Claude
reviewer, invoke only:

```text
.codex/skills/critique/scripts/run-claude-review.sh
```

Never invoke the raw `claude` CLI directly, through a hand-composed shell
command, or through a replacement wrapper. Never supply or override Claude's
model, effort, permission, persistence, or output options; the tested launcher
owns those settings and pins literal `--effort low`. Do not set
`CLAUDE_REVIEW_CLI` outside launcher tests. If the launcher is missing, rejects
the exact-head preflight, or fails, stop and report the blocker. Do not fall
back to a direct Claude invocation.

## Context Window Check

Run this check before anything else. `deepcritique` is the most cache-hungry skill in the chain — it runs `refactorpass` (cleanup matrix) and then `critique deep` (six core independent review lanes, plus a conditional tenant-coupling lane). When subagents/delegation are available, the lanes run in parallel, each inheriting cache state from this session; when subagents are not available they run as serial local passes against the same context. Either way, if the current Codex session has already been heavily used for feature implementation, the lanes start with sharply reduced working windows and the whole chain runs slower and more expensively.

Assess honestly:

- Has this session been writing/editing the feature about to be reviewed? Long conversation, many file edits, dense planning?
- Is the conversation about to brush against compaction territory?

If either is yes, briefly tell the user:

> This session already contains substantial implementation context. A fresh Codex session may make `deepcritique`'s review lanes cheaper, but I can continue here if that is the authorized task.

This is cost and quality advice, not a workflow gate. Do not stop, defer the
authorized task, or require a new session solely because context is heavy or
compaction is approaching. Continue in the current session when the user has
already authorized the review or asked you to proceed. Pause only when the user
requested a fresh-session boundary, the runtime cannot continue safely, or a
separate-session protocol transition below requires another reviewer.

## PR-First Preflight

Load `.codex/references/local-review-ledger.md` and follow it throughout this
pass. Take an optional PR number from the invocation.

Require a clean, committed feature branch. Reuse the open PR whose head is the
current branch. If none exists, push the branch with a normal push and open a
draft PR before invoking any review lane. Verify the local HEAD, remote branch,
and PR head SHA match. Record the PR number and load all prior review threads,
including resolved and outdated threads. Telemetry markers are not review
context: exclude them by marker prefix and never carry them into a lane prompt
or packet.

This chain emits no telemetry record of its own. Each lane it invokes snapshots
and emits for itself, so a deep round produces a `review` record plus, when
cleanup actually ran, a `refactor` record — rather than a third that
double-counts both. A convergence round or a spent latch skips cleanup, so
those rounds produce only the `review` record.

The chain's own overhead is therefore **not** in either record: pre-flight runs
before the first lane snapshots, and the packet rebuild between lanes and the
relay after the last one fall outside both windows. A deep round's recorded
cost is a lower bound, and the gap is one-directional — it understates Deep and
never Lean, which is the comparison these records exist to support. Reading a
long thread ledger is not a rounding error, so treat the difference as real
when reading a Deep-versus-Lean series. See `.codex/REVIEW_WORKFLOW.md`
"Pass Telemetry".

Resolve the selected local session mode from repository instructions or the
user. When the consumer declares no default, ask before running a lane rather
than assuming one.

Then read the prior pass, per
`.codex/REVIEW_WORKFLOW.md` "Read the prior pass before every
review". This read is unconditional — it does not depend on the
session mode or on the user having said "continue" or "resume". Read every
`local-review-pass:v3` and `local-review-complete:v3` attestation on the PR,
their bodies, and the inline v3 threads their fingerprints name; that record,
not a handoff comment, is the authority on what another engine already found
here. A `local-review-handoff:v1` comment addressed to Codex is a richer read
when one exists, and its absence is normal rather than a reason to refuse.

Stop and hand back to the user only when a handoff comment exists and targets
another engine — that pass is owed and belongs in its own fresh session. Another
declared reviewer holding no attestation is not a stop: reviewer order within a
round is a scheduling choice, not a protocol rule.

Resolve this engine's round number per the ledger: `$AGENT_LOOP_REVIEW_ROUND`
when the runner set it, otherwise one past the count of `local-review-pass:v3`
and `local-review-complete:v3` markers on the PR naming `engine=codex`. Rounds
1–2 are adversarial; round 3 and later are convergence rounds. State which
applies before invoking a lane.

## Chain

The chain gets cheaper as it repeats, deliberately. Cleanup runs once; the
adversarial stance holds for two rounds and then gives way to landing the change.

1. Search the PR for `local-review-refactor:v1 engine=codex`. If it is absent and
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

Classify the behavior the diff changes, not merely the domain or value flowing
through it. An ordinary version or image pin, deployment-only configuration
update, environment-variable wiring, or reference to an existing secret does
not trip trigger 1 solely because a sensitive runtime value will be supplied
during deployment. That exception applies only when the diff does not embed,
print, transform, authorize, persist, or otherwise change the handling of that
value and no other trigger applies.

## Handoff

Read `.codex/REVIEW_WORKFLOW.md` and the consumer's instructions to determine
which cross-model path the developer selected.

When invoked as the final sub-skill inside `reviewit`, return the deepcritique
result directly to that orchestrator. Do not start the local relay, recommend
another `reviewit`, push, or emit a terminal workflow summary; `reviewit` owns
the hosted-path summary.

When `$AGENT_LOOP_REVIEW_RESULT_FILE` is set, finish the ledger, write the
wrapper-owned Codex result as described below, and return to agent-loop. Never
launch another engine from inside a wrapper-owned Codex hook; agent-loop owns
the next engine and its separate result boundary.

Outside agent-loop, when `$AGENT_LOOP_REVIEW_ENGINE` is set or this pass is part
of a review relay, follow the selected session mode. This skill performs exactly
one review pass and returns its result. It never launches another reviewer,
retries itself, or recursively continues the relay. In auto mode, the outer
controller reads the effective roster and exact-head coverage, authorizes the
next bounded pass, and invokes the matching tested launcher: use
`run-agy-review.sh` for `gemini` and `run-claude-review.sh` for `claude`. Never replace a declared reviewer silently;
changing an existing roster requires an explicit superseding declaration. When
no roster exists yet, declare it before launching; Gemini is the default
reviewer unless repository instructions or the user selected another roster.
Never hand-compose either command or override its model, effort, permission,
output, or timeout options. Both launchers require the exact reviewed head and
fail closed unless the current worktree is that self-authored,
same-repository PR head. The Agy launcher additionally resolves and validates
the complete Agy relay surface before review. In handoff mode, post the
next-session handoff with `local-review-handoff.py post-handoff` and return
control to the user.

```bash
.codex/skills/critique/scripts/run-agy-review.sh \
  --repo <owner/repo> --pr <pr-number> --base <review-base-sha> \
  --head <reviewed-head-sha> --round <round>
```

Explicit Claude fallback:

```bash
.codex/skills/critique/scripts/run-claude-review.sh \
  --repo <owner/repo> --pr <pr-number> --base <review-base-sha> \
  --head <reviewed-head-sha> --round <round>
```

After a launcher returns to the outer controller, it verifies local, upstream,
and PR heads plus the new ledger evidence before deciding whether the round converged. A fix invalidates
only the attestations naming the superseded head. A launcher failure stops the
chain; never retry with a hand-composed command.

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
  <review-base-sha>.
  Auto mode: run the tested launcher for each missing declared reviewer and
  continue the chain; use Gemini only as the default for a newly declared relay.
  Handoff mode: a local-review-handoff:v1 comment was posted; the user starts a
  fresh terminal for the next reviewer and says "Continue review on PR
  #<pr-number>."
  Read all prior local-review threads before reviewing.
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

If no session mode is declared, present auto and handoff and ask the developer
to select. In auto mode follow any existing roster; for a newly declared relay
Agy is the default local reviewer unless repository instructions or the user
selected another roster.
