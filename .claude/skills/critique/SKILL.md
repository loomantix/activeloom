---
name: critique
description: PR-first adversarial review. Lean mode runs the highest-signal local Claude lenses; `deep` selects the full relevant matrix. Verified findings are posted inline before fixes, then replied to and resolved after a pushed fix.
argument-hint: (optional PR number and/or "deep")
---

# /critique — PR-first adversarial review

Run a bounded local Claude review against an open draft PR. GitHub review
threads are the durable context ledger: later reviewers must see what earlier
reviewers found, how it was fixed, and why a thread was resolved.

## Mode resolution

Mode follows the resolved review tier, not the caller's habit. Parse
`$ARGUMENTS` for an optional PR number and the word `deep`; `deep` asserts that
the tier resolved to Deep, and Phase 0 checks it against the PR's tier marker.

- Lean mode runs `code-reviewer` and, when the diff contains error/async
  signals, `silent-failure-hunter`.
- Deep mode selects only the relevant lenses from `code-reviewer`,
  `silent-failure-hunter`, `type-design-analyzer`, `comment-analyzer`,
  `pr-test-analyzer`, `security-review`, and the tenant-coupling pass.
- Five agents is the maximum for one pass. The matrix is a ceiling, not a
  target.

## Stance resolution

Resolve this engine's round number per the ledger before selecting lenses: use
`$AGENT_LOOP_REVIEW_ROUND` when the runner set it, take it from an invoking
`/deepcritique`, or count the `local-review-pass:v3` and
`local-review-complete:v3`
markers on the PR naming `engine=claude` and add one.

The stance follows the tier's schedule, not the round ordinal alone:

- **At Deep, rounds 1–2 run adversarially and round 3 and later run in
  convergence mode.**
- **At Lean the cap is two rounds: round 1 is adversarial and round 2 is the
  convergence round.** A Lean PR arriving at round 3 is mis-tiered or not
  converging; report which and stop rather than opening it.
- **The first round after an escalation is adversarial whatever its ordinal**,
  per the workflow doc's escalation rule.

In an adversarial round the matrix and dispositions below apply as written. In a
convergence round both engines have already read the change cold; the goal moves
from challenging it to landing it. Convergence mode overrides the lens table and
the fix bias, as set out under "Convergence rounds" below. It does not change the
post-before-editing, reply, or resolve contract, and it does not raise the round
cap.

State the resolved round and stance in the output.

## Phase 0: Fresh context and PR boundary

### Context-window check

If this session authored the change or carries dense implementation context,
stop and recommend a fresh Claude session. Continue only after an explicit
override. Fresh eyes and prompt-cache headroom are part of the review quality
contract; see [`../../MODEL_NOTES.md`](../../MODEL_NOTES.md) §8.

### PR-first pre-flight

1. Load [`../../references/local-review-ledger.md`](../../references/local-review-ledger.md).
2. Require a clean, committed feature branch, not `main`, `master`, or
   `staging`.
3. Reuse the branch's open PR. If none exists, push normally and open a draft
   PR before starting a reviewer.
4. Require local HEAD, remote head, and PR head to match.
5. Record the exact PR base and head SHAs. Read the actor-owned issue comments
   needed to resolve the tier, round, and stance, excluding every comment whose
   marker begins `local-review-telemetry:`.
6. Resolve the round and stance now, before the telemetry boundary or any branch
   that can emit. Read the effective tier marker already on the PR — reading it
   only, since a pass that exits on the docs/config classification posts no
   marker — and fall back to `adversarial` when none exists, which is correct
   because a PR carrying no tier marker has had no prior round and both schedules
   make round 1 adversarial. `emit-telemetry` requires `--stance` and offers no
   way to omit it.
7. Take the pass telemetry snapshot now that its mandatory repository, PR,
   base, head, round, and stance identity exists, per
   [`../../REVIEW_WORKFLOW.md`](../../REVIEW_WORKFLOW.md) "Pass Telemetry". The
   helper is a no-op when extraction is not enabled for this repository, and it
   reports the separate emission gate that decides whether this pass may publish
   a record at all. The skill and identity-resolution setup above is outside the
   measurement boundary.
8. Read every prior review thread, including resolved and outdated threads.
   Telemetry markers are not review context: exclude them by marker prefix and
   never carry one into a finder prompt or packet. Where any remaining thread
   carries a v3 finding or disposition record, write its comment ID to an
   owner-only file before running a lane: that snapshot is the ledger's
   `--historical-comment-ids-file`, and it is the one attestation input that
   cannot be reconstructed once this pass has posted its own findings.
9. Skip docs/config-only changesets, per the ledger's changeset classification.
   Finalize a clean v3 result using the ledger's wrapper/standalone ownership
   rule, then emit a `skipped` telemetry record, before returning. A skip still
   spends tokens reading and classifying the PR, and that overhead is worth
   seeing.
10. If a failure terminates this pass after the snapshot boundary, emit
    `status=blocked` before returning. This applies from here on, not only at
    Phase 4 — a pass that dies in Phase 1 through 3 never reaches the output
    phase, and that is exactly the pass the `blocked` record describes.
    A failure in steps 1–6 reports `telemetry not emitted: boundary unresolved`
    instead; it does not yet have the mandatory identity needed to emit.

Do not begin a reviewer until the PR ledger is available. Do not use a
force-push to establish or update the review branch.

### Tier resolution

Resolve the effective `local-review-tier:v1` marker under the ledger's
authenticated, forward-only transition rule. If none exists, classify against
the tier triggers in [`../../REVIEW_WORKFLOW.md`](../../REVIEW_WORKFLOW.md) and
post the marker before starting a lane; Lean is the tier when no trigger
matches. Run the lens set for the recorded tier. A `deep` argument from an
internal `/deepcritique` handoff only asserts that tier; a direct human `deep`
request is trigger 6 and posts a Deep replacement that preserves recorded
triggers and adds 6 before lanes start. Escalate mid-pass only on a confirmed
finding that reaches a trigger, per the workflow doc's evidence rule, and post
the replacement marker naming it. State the resolved tier and trigger alongside
the round and stance.

## Phase 1: Select the review lenses

Always include `pr-review-toolkit:code-reviewer` when source code changed.
Add only lenses whose signal exists:

| Diff signal                                                    | Lens                                           |
| -------------------------------------------------------------- | ---------------------------------------------- |
| try/catch, fallback, async, error propagation                  | `silent-failure-hunter`                        |
| new or changed public types/generics                           | `type-design-analyzer`                         |
| substantial comments or docstrings                             | `comment-analyzer`                             |
| new/changed tests                                              | `pr-test-analyzer`                             |
| auth, crypto, secrets, sensitive data, input or SQL boundaries | `security-review`                              |
| tenant/customer-variable behaviour or data normalization       | dedicated `code-reviewer` tenant-coupling pass |

Every finder prompt must identify the exact head and diff, ask the agent to
read the source, request every plausible finding with severity and `file:line`,
and impose a concise output ceiling. Name the four-rung ladder from the ledger
reference in the prompt — `blocking`, `major`, `minor`, `nit`, rated on blast
radius — so lenses do not each invent their own scale. Do not ask finders to
suppress findings by confidence. Run selected agents in parallel; the
orchestrator verifies them.

Brief each finder per the ledger's diff-delivery rules: resolve the changed-file
list once, name the paths that lens owns, and prefer `git diff <base-sha>..HEAD
-- <path>` over handing every agent one whole-diff artifact. Scope a lens by the
files it reviews, never by the findings it may report.

### Convergence rounds (any round whose stance is convergence)

Run only `code-reviewer`, `silent-failure-hunter`, and `security-review` when its
signal is present. Drop `type-design-analyzer`, `comment-analyzer`,
`pr-test-analyzer`, and the tenant-coupling pass: they found what they were going
to find in rounds 1–2, and they audit a surface that regenerates every time it is
hardened, so they are guaranteed to return work and guaranteed not to change what
ships.

Brief those finders exactly as an adversarial round does. They still report
everything they find with severity attached — a reviewer instructed to withhold
by severity or confidence drops real defects, and this narrowing is a disposition
rule enforced one level up, not a reporting rule pushed into the agent. See
[`../../MODEL_NOTES.md`](../../MODEL_NOTES.md) §1.

## Phase 2: Verify and deduplicate

Combine the outputs, inspect the code, and reject false positives,
pre-existing issues outside the diff, and unsupported style preferences.

Before presenting or editing a surviving finding:

1. derive its stable fingerprint;
2. search all prior local-review threads for that fingerprint or defect;
3. reuse the existing thread when present;
4. otherwise post one inline comment on the right-side diff line using the
   marker and content contract from the ledger.

The inline comment must exist before the corresponding edit. If no defensible
diff anchor exists, keep it out of the automated fix loop. Track it separately
only when it clears the urgent-follow-up bar in Phase 3.

If no new confirmed finding survives and the enclosing review hook did not move
the head, finalize a `clean` v3 result per the ledger. When deepcritique's earlier
refactorpass committed, preserve the enclosing hook's original before SHA and
finalize `changed` with classification `minor` and an empty finding set; the
committed refactor latch supplies the evidence. Under agent-loop the wrapper
owns the canonical pass attestation; a standalone pass must attest through the
helper before reporting completion. The helper's snapshot flags are optional
inputs — omit them and it reads the threads live — so a pass that did not seal
one still attests. Never report a pass complete on a write-up that only names
the marker in prose; if the helper refuses, finalize `blocked` with its
diagnostic instead.

## Phase 3: Disposition and fixes

Treat validity and actionability as separate decisions. A technically real
concern is not automatically worth changing the PR or growing the backlog.

Fix a confirmed finding only when the expected harm avoided clearly outweighs
the churn and regression risk of the fix. Judge that from how likely a user is
to reach the path, the impact and breadth when they do, recoverability,
confidence in the root cause and correction, and the change's size, complexity,
compatibility cost, and regression risk.

For security findings, require a credible exploit path: identify the reachable
boundary, attacker capability and preconditions, missing or bypassable control,
and resulting impact. A theoretical weakness, generic hardening opportunity, or
severity label without a plausible path to discovery and exploitation does not
by itself justify churn.

Create a GitHub issue only for an urgent follow-up: a concrete, high-impact
defect that is important enough to schedule within roughly the next two weeks,
but whose safe fix should not land in this PR. Ordinary deferred backlog,
speculative hardening, cleanup, and low-likelihood edge cases get no issue. If
already posted, reply with `outcome=deferred` and the no-issue rationale; keep a
concern that does not clear the actionable finding bar out of the PR ledger.

In a convergence round, the bar tightens further toward landing the change.
Change the PR only for a realistically reachable `blocking` defect, as the
ledger's severity ladder defines it, that also clears the bar above. A finding a
comment or test edit could clear was never `blocking`:

- Fix a blocking finding with the smallest edit that clears it. No refactor, no
  rename, no new abstraction, no test or comment hardening alongside it.
- Defer every confirmed non-blocking finding and resolve its thread. Create and
  link an issue only when it clears the urgent-follow-up bar above; otherwise
  reply with `outcome=deferred` and a concise no-issue rationale. Deferral here
  is the expected disposition, not an admission of scope creep.
- Dismiss false positives or suggestions that would make the code worse with
  concrete evidence and resolve the thread without editing.

The findings a convergence round defers may still be real. Fixing them in this
PR is the wrong call when the expected benefit does not justify moving the head
and re-staling the other engine's attestation. Land the change; let only urgent
follow-ups grow the backlog.

For confirmed fixes:

1. make the smallest safe edit;
2. run focused validation;
3. commit conventionally and push normally;
4. require local HEAD, remote head, and PR head to match;
5. use the deterministic helper's resumable `dispose` transaction with the fix
   SHA, validation result, fingerprint, and occurrence;
6. before the attestation, run the repository's gating suite unfiltered, per the
   ledger's "Validate before attesting". The focused run in step 2 dispositions
   the finding and is not evidence for the pass. Name the command, config, and
   SHA in the attestation. A red gating run is itself a blocking finding, even
   when it predates this round, and applies to a `clean` pass just as much as a
   changed one;
7. after the final lane, write the v3 structured result. Under agent-loop the
   wrapper owns the committed-pass marker.

Never resolve a finding merely because code changed. A marked thread requires
both a response and an explicit resolution. Never batch unrelated findings
into one ambiguous thread.

Material fixes invalidate prior clean-pass attestations and require the next
local engine to review the new exact head. Minor-only fixes do not by
themselves start an unbounded new round.

Classify by effect, not path or finding severity. `material` includes any
substantive correctness, security/privacy, data-safety, compatibility,
deployment/sync, or review-integrity change, including a test or workflow
change needed to prevent a false green. `minor` is low-risk non-behavioral
cleanup, clarity, or test/docs polish.

Severity is a property of the finding, classification a property of this pass's
diff, and neither implies the other. A `major` finding whose fix touched only
comments, only docs, or only tests is a `minor` pass. Never restate a severity
to reach a classification: the thread's severity is fixed evidence, and the
classification is read off the diff. See "Severity is not classification" in the
ledger reference.

Every engine attestation remains exact to the head it reviewed. A later minor
fix does not rewrite that evidence or claim the other engine reviewed the new
head. The outer round may still converge through an explicit minor transition;
a material transition moves the head, which invalidates precisely those
attestations that named the superseded commit.

## Phase 4: Output

Once the v3 result is finalized and any fix commits are pushed, take the
prompt-stack digests and emit this pass's telemetry record per
[`../../REVIEW_WORKFLOW.md`](../../REVIEW_WORKFLOW.md) "Pass Telemetry", with
`--pass-type review` and the status this pass reached. A record that cannot name
the prompt generation it ran on cannot be compared against the next one, so the
two digests are part of emitting, not an optional extra.
Emission runs last because it must describe the finished pass, and it exits zero
whether or not it succeeded: a telemetry failure is reported and never retried
into the review, never changes the v3 result, and never delays marking the PR
ready.

Report the PR and reviewed head, the resolved tier and its trigger, the round
and stance, mode and lenses, disposition/thread counts, validation, fix SHAs,
and whether material fixes require another local-engine pass. Reporting this
pass's own measured spend here is permitted; reading any earlier pass's record
is not.

A convergence round that found no blocking defect ends the loop: record a clean
result, recommend the ship step, and list any urgent deferred issues.

If this Claude pass made a material fix, it moved the head: every declared
reviewer whose attestation named the superseded commit re-runs against the new
head, and a reviewer that already attested this head does not. In session mode
that re-run is a fresh terminal; in auto mode it is that engine's checked-in
launcher — `.claude/skills/critique/scripts/run-agy-review.sh` for `gemini` — followed by a
`verify-coverage` check at the exact reviewed head, per
[`../../REVIEW_WORKFLOW.md`](../../REVIEW_WORKFLOW.md). Otherwise it completes Claude's part of the current round. Always finalize
`clean`, `changed`, or `blocked` per the ledger's wrapper/standalone ownership
rule before returning. Use `write-result` for `clean` or `changed`, and use
`write-blocked-result` with an owner-only blocker file for `blocked`.

## Boundaries

- Do not force-push or merge.
- Do not invoke hosted reviewers; they are a separate lane the caller runs, not a side effect of this skill.
- Do not print raw model logs in the PR.
- Do not mark the PR ready while marked threads are unanswered or unresolved.

## Source of truth

This skill lives upstream at `.claude/skills/critique/` and is synced to consumer
repositories.
