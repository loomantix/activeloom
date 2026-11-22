---
name: refactorpass
description: PR-first cleanup pass for Antigravity or Gemini CLI. Use when the user asks for refactoring, cleanup, simplification, or the platform review chain on an open draft PR. Posts verified cleanup suggestions inline before editing, runs a structured cleanup matrix, and pushes, replies, and resolves when appropriate. Runs at most once per PR for this engine.
---

# Refactor Pass

## Reviewer environment preflight — hard stop

When hreview supplies a binding, run the controller-supplied absolute `reviewer-entry.py enter`
command before following this skill and follow its bound skill from the candidate cwd, unless you are
already following that skill's entered text. Each nested lane gets its own `enter` with `--skill` set
to that lane; after a pushed fix, set `--head` to the new Git-derived HEAD and keep the base, run,
round and receipt. A failed entry ends the pass before any evidence operation.

This preflight precedes all other entry gates, including instructions below to
classify or check context first. Before profile setup, GitHub reads, telemetry,
classification or any ledger write,
run these commands separately in the supplied review worktree:

```bash
git rev-parse HEAD
node .agents/skills/critique/scripts/review-ledger.js --version
node .agents/skills/critique/scripts/review-ledger.js silent-status --help
```

Require the last command to print the command-specific `usage: review-ledger silent-status --repo` synopsis; generic help or an unknown command blocks. Compare the reported helper version with
`.agents/skills/critique/scripts/review-ledger.version`. On any command's
nonzero exit, a missing pin or a version mismatch, stop and report the failing
command, exit status and exact stdout/stderr (or both versions for a mismatch).
Post nothing. Do not simulate a ledger write, hand-write markers, create a
worktree, re-clone, change settings or start a launcher to escape the failure.
Agy's sandbox can hide the primary checkout's Git metadata and user configuration
(including gh authentication and activeloom profiles); missing paths inside it
do not prove the host worktree or configuration is broken. Report this possible
cause to the controller or user, who owns relaunching with the approved settings.

Pass full SHAs directly from Git output; never retype or expand an abbreviated SHA.
After publishing, record the full SHA captured after this pass's successful push;
a pass that publishes nothing records its Git-derived reviewed head instead.
Before each `dispose`, capture a complete pre-call ledger snapshot and verify the
local HEAD, the pushed remote branch and the live PR head all equal that recorded SHA.

The missing-argument exception applies after this preflight: when the pinned ledger CLI returns a
missing-required-argument diagnostic followed by that subcommand's `usage:` synopsis,
and its required-argument guard rejected the invocation before any write, the
reviewer may make one corrected invocation. Read that subcommand's `--help`;
use only its documented flags and fill missing values from the pinned review
context, preserving all supplied identity arguments. Confirm that the
local and live PR heads are unchanged since the failed invocation, that the
live PR head still equals any `--head` it named, and that
previously captured ledger evidence is unchanged. Continue in the
same run, round and budget; do not relaunch a worker or rewrite a result.
A second failure stops the pass. This exception never covers a controller
refusal, stale-head, authorization, lifecycle or integrity refusal, a
partial or uncertain write, or an error whose pre-write status is unproven.

A separate head-transcription exception applies only to `dispose` rejected with
`PR head mismatch`. The supplied `--head` must differ from that recorded SHA,
and must not be a previously valid pass head.
Require the local HEAD, pushed remote branch and live PR head to still match that
recorded SHA, and require the complete pre-call ledger snapshot to be
unchanged after the refusal. Also verify from the pinned implementation that the
failed call stopped before any write; the error text alone is insufficient,
because a post-write head check can report the same mismatch. Then change only
`--head` to the Git-derived recorded SHA and retry once, preserving every other
argument and the content file. Do not replace a previously valid head with a newer head.
Missing evidence, a real head move or a partial/uncertain write stops the pass.
Both exceptions share one correction attempt per pass, within the same run,
round and budget; they never revive an already-blocked result or relaunch a worker.

At any other later ledger or controller refusal, including `transition-run`, stop and
report the exact command and error to the controller or user. Never retry the
same transition, invent flags or scripts, simulate its write, or use another
tool, worktree or launcher to obtain the refused effect. This overrides generic
retry or recovery instructions for this reviewer pass. It does not cancel the
protocol's own steps after a refusal: still finalize a `blocked` result through
the helper, preserve any recovery sidecar, and re-run an identical idempotent
helper command (such as `dispose`) where the ledger prescribes it.

## Step 0: Human-glance gate

After the reviewer environment preflight, when present, classify the range
before every other step in this skill — before the
context-window check, the PR boundary, round and stance, the telemetry
snapshot, and any marker. Follow `.agents/REVIEW_WORKFLOW.md` "Human glance".
Apply that section's **Verified zero-file comparison** first: when
verified, print its no-content line and return without a chain or auto-glance
and with no draft PR, ledger result, attestation, tier or refactor marker, or
telemetry record. Its PR-wide lifecycle check
still applies; an existing run is not bypassed by an empty diff. Failed or
unavailable reads block this route. An empty range that fails verification
with no existing run reports the block and stops the same way.

On `skip: true` with at least one classified file, print that section's one-line
message and stop with the same absence of review artifacts.
On `smallChange: true`, print that section's small-change
recommendation and stop the same way.

On `dependencyUpdate: true`, print the workflow's dependency-update line, stop
the same way, and run `dep-analysis` instead: no review chain runs on a
dependency update, whatever the tier marker or skill name, unless the
workflow's dependency review override applies — a direct human request, a
current or previously applied `review: full` label, or a matching always-Deep
repository tier extension. Resolve it using the workflow's complete label-event
history rule; label removal alone does not cancel it. Then classify again with
`--force-review` and continue as for a mixed range. When
`dependencyPaths` is non-empty and `dependencyUpdate` is false, the range is
mixed: continue for the code, and follow the workflow's "Dependency updates"
rule for the dependency files.

Continue when the range carries a review-significant file and is neither a
small change nor a dependency update, when a human
explicitly asked for this change to be reviewed anyway or
overrode a small-change recommendation, when a small change's open PR already
carries a tier marker, or when
`$AGENT_LOOP_REVIEW_RESULT_FILE` is set and the controller that scheduled this
pass owns the gate.

## Silent mode

A PR is silent when `$ARGUMENTS` holds the word `silent`, or when the ledger
helper's `silent-status --repo <owner/repo> --pr <number>` reports
`"silent": true`. A failed or malformed status read blocks every evidence write. For an argument-requested PR, run `silent-pin` before the
first ledger read. A refused pin ends the pass: report the refusal to the user
and never continue the review loud. A silent PR keeps its ledger local: follow
[Silent mode](../../references/local-review-ledger.md#silent-mode) for every
ledger read and for the end-of-loop summary. Every posting step below that
uses the ledger helper still runs exactly as written, and the helper keeps the
post local. A step that posts with raw `gh` is not intercepted: write that text
into the scratchpad that `silent-status` names instead. Where a step below
replies to a hosted reviewer, write that reply into the scratchpad, then
resolve the thread with `gh`. The helper's `local-gh` subcommand is internal
to the run controller; never call it yourself, and on a PR that is not silent it
refuses every write.

## Findings before telemetry emission

Before every telemetry emission attempt, including an early `blocked` or
spent-latch return, follow [Count the findings](../../REVIEW_WORKFLOW.md#count-the-findings):
write the complete measured findings file and supply `--findings-file`.
Preserve findings posted before an interruption; unknown counts are not zeros.
If counts cannot be established, report `telemetry not emitted: findings measurement unavailable`
and follow the existing nonfatal telemetry path.

Run a structured, behavior-preserving cleanup pass on an open draft PR before
adversarial review. This is not a broad refactor, and it is not a step that
repeats each review round: it is the active engine's **one** cleanup pass on that
PR. Resolve the active runtime as `gemini` or `antigravity`; use `codex` only
when Codex is actually running the pass.

## Pass measurement

Follow "Pass Telemetry" in `.agents/REVIEW_WORKFLOW.md`. After resolving the
mandatory pass identity and before diff classification, run the usage helper's
`snapshot`. On every terminal path, including blocked, finalize the
review result first, then run `delta` and attempt emission only when `emit` is
true. Report publication failures and unavailable usage explicitly. A failure
before identity resolution reports `telemetry not emitted: boundary unresolved`.

## Context Window Check

Run this check before anything else. `refactorpass` (and the `critique` that typically follows) does diff-reading, multi-lane reviewing, and edit application — all cache-hungry. If the current Antigravity or Gemini session has already been heavily used for feature implementation, the cache is largely spent on context the cleanup pass does not need, and the downstream `critique` (especially `critique deep`'s six independent lanes) will be measurably slower and more expensive.

Assess honestly:

- Has this session been writing/editing the feature about to be cleaned up? Long conversation, many file edits, dense planning?
- Is the conversation about to brush against compaction territory?

If either is yes, stop and tell the user:

> Your context is heavy from the implementation work. Start a new Antigravity or Gemini session and run `refactorpass` (and `critique` / `deepcritique`) there. The downstream lanes need cache headroom and a fresh session makes the chain materially cheaper.

Do not proceed in the current session unless the user explicitly overrides.

## Fix Bias

Apply every valid cleanup `refactorpass` surfaces in this pass. Skip suggestions only when they are wrong: would change behavior, would make the code worse, would introduce speculative abstraction, or are based on a misread of the diff. Do not defer valid cleanups to a "follow-up PR" — the only legitimate defer is a major architectural rework (roughly 300+ lines or a cross-cutting redesign), and in that case file a GitHub issue at deferral time rather than leaving the suggestion as an undocumented todo. Reason: every valid cleanup that ships becomes the floor for the next PR in this area, and letting them accrue as "deferred" turns the backlog into review noise and makes future cleanups more expensive.

## Cleanup Matrix

Refactorpass must cover three lanes:

1. **Simplicity/DRY lane** — remove fresh duplication, collapse awkward control flow, inline one-use abstractions, delete dead code, and simplify names when the diff makes intent clearer.
2. **Correctness-preserving lane** — look for cleanup that reduces bug risk without changing behavior: narrower conditions, safer defaults, clearer error paths, less state mutation, and tighter async/resource cleanup.
3. **Convention/API lane** — align fresh code with local patterns, package boundaries, exports, dependency placement, and documented repo conventions.

Run these lanes as independently as the active runtime permits:

- If subagents/delegation are available and permitted by the active instructions, spawn independent cleanup reviewers for the three lanes using the ledger's immutable review packet and scoped diff-delivery contract. Keep the packet prefix byte-identical, append only the cleanup lens and exact file scope, use no inherited conversation history when supported, and impose a concise output ceiling. Tell each reviewer to suggest behavior-preserving cleanup and avoid broad rewrites.
- In an Agy print-mode pass, do not spawn subagents or background cleanup lanes. Run the cleanup lanes sequentially in series within the primary session. In each lane, verify suggestions, apply behavior-preserving cleanup, and validate before proceeding to the next lane so subsequent lanes review the updated code. Run all commands and tests synchronously in the foreground; never leave background tasks running. Wait for all lanes to complete before writing the canonical result.
- If subagents are unavailable or not permitted, perform three separate local passes using the lane prompts above. Do not present that as equivalent to independent subagents.
- If refactorpass could not use independent subagents, explicitly say so in the output under `cleanup depth`.

## Process

1. Load `.agents/references/local-review-ledger.md`.
2. Verify the branch is not `main`, `master`, or `staging`. Resolve or create
   its draft PR before running cleanup lanes, and require local, remote, and PR
   heads to match. Read all prior review threads.
3. Use the exact base SHA supplied by an invoking `deepcritique` or caller. Only
   when run standalone without a supplied base, resolve `@{u}` when available,
   otherwise the default branch, once. Resolve the reviewed head, changed-file
   list, and diff stat once and build the ledger's immutable review packet. Pass
   the literal `<base-sha>..<head-sha>` range to every cleanup lane; never let
   lanes re-resolve a mutable ref or rebuild the packet independently.
4. Check the once-per-engine latch. Search the PR's comments for
   `local-review-refactor:v1 engine=<active-engine>`, authored by the actor
   running this review. If it is present, this PR has already had that engine's cleanup pass:
   report the skip with the head the earlier pass ran on and stop without running
   a lane. Continue only when the marker is absent or the caller explicitly asked
   to force a re-run, and say which of the two applied.

   The rule exists because the second pass over an already-simplified diff
   returns naming and shape churn, not cleanups. That churn moves the head and
   re-stales the other engine's attestation for no shipped benefit.

5. Assign each lane the exact changed source paths its lens needs and execute
   every lane in the Cleanup Matrix. Follow the ledger's scoped-read contract;
   do not hand every lane a whole-diff artifact.
6. Consolidate lane suggestions, verify them, and deduplicate them against the
   complete PR ledger.
7. Post each confirmed cleanup inline before editing, then apply only cleanup
   that is behavior-preserving and clearly improves the fresh diff.
8. Keep scope tight: touch only code changed by the current branch unless a tiny adjacent edit is required to finish the cleanup safely.
9. Do not introduce feature behavior, broad rewrites, unrelated style churn, formatting-only commits, or speculative abstraction.
10. Run the smallest relevant formatter/test command if the repo documents one.
11. If changes were made, commit them as `refactor: <active-engine> cleanup pass - <summary>` (e.g. `refactor: gemini cleanup pass - <summary>`).
    When `$AGENT_LOOP_REVIEW_PUSH_HELPER` is set, leave the commit local for the
    enclosing critique pass so that wrapper invocation publishes exactly once;
    otherwise push without force. Reply to each cleanup thread with the commit and
    validation, then resolve it.
12. Whether or not the lanes produced changes, post one informational PR comment
    closing the latch for this engine, carrying the ledger's marker:

    ```text
    <!-- local-review-refactor:v1 engine=<active-engine> head=<reviewed-sha> outcome=<committed|no-op> -->
    ```

    Post it only for a pass that actually ran the cleanup lanes. A docs/config-only
    skip leaves the latch open, so a later round whose changeset contains source
    can still spend the one pass.

## Output

Report:

- cleanup depth: independent subagents, local three-pass fallback, or no source changes
- latch state: first pass for this engine, already spent at `<sha>`, or forced re-run
- whether changes were made
- commit SHA if created
- validation run
- PR number plus comments, replies, and resolved-thread counts
- recommended next step from the selected path: if invoked by `deepcritique`,
  return so it can run `critique deep`; if run standalone, run `deepcritique`
  next and hand to the next declared reviewer only after that full chain; add
  `reviewit <pr-number>` only when the user asks for a hosted pass
