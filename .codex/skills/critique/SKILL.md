---
name: critique
description: PR-first adversarial code review for Codex. Use after implementation or refactorpass on an open draft PR, especially when the user asks to critique, review hard, find bugs, or run the platform review chain. Posts verified findings inline before fixing, then replies and resolves. Select relevant lenses and proportionate delegation; Lean is the default and Deep follows the recorded risk.
---

# Critique

Review an open draft PR adversarially. The goal is to catch bugs, missing tests,
security issues, and convention violations while preserving every verified
finding and disposition in the PR.

## Context Window Check

Choose review scope before allocating agents. A bounded diff can be reviewed directly; delegate substantial independent tracks when a fresh reader adds value. Carrying implementation history into each reviewer can add cost and anchoring without improving coverage.

Assess honestly:

- Has this session been writing/editing the feature about to be critiqued? Long conversation, many file edits, dense planning?
- Is the conversation about to brush against compaction territory?

If either is yes, briefly tell the user:

> This session already contains substantial implementation context. A fresh Codex session may make the review lanes cheaper, but I can continue here if that is the authorized task.

This is cost and quality advice, not a workflow gate. Do not stop, defer the
authorized task, or require a new session solely because context is heavy or
compaction is approaching. Continue in the current session when the user has
already authorized the review or asked you to proceed. Pause only when the user
requested a fresh-session boundary, the runtime cannot continue safely, or a
separate-session protocol transition requires another reviewer.

## Stance Resolution

Before selecting lanes, use the controller-authorized `$AGENT_LOOP_REVIEW_ROUND`
or the round supplied by `deepcritique`. Otherwise select one past Codex's
highest completed round within the latest authenticated `local-review-run:v1`
(1 when it has none), using only pass/complete comments after that run marker.
Validate it with `local-review-handoff.py authorize-pass`. An ended run,
including an aborted one, requires fresh restart authorization: this controller
has no budget-preserving resume command. PR-wide history is a fallback only when no run
marker exists; it never consumes a restarted run's budget.

- **Adversarial:** Lean round 1 and Deep rounds 1–2. The stance, matrices,
  and fix bias below apply as written.
- **Convergence:** Lean round 2 and Deep rounds 3–4. At Deep both engines have read the
  change cold twice; the goal moves from challenging it to landing it. See
  "Convergence Rounds" below — it overrides the lane selection and the fix bias,
  and nothing else. The post-before-editing, reply, and resolve contract is
  unchanged, and the round cap does not move.

The first pass after escalation and a reviewer's first cold read remain
adversarial regardless of ordinal. State the resolved round and stance in the output.

## Adversarial Stance

Assume there are problems to find. Treat the diff as guilty until each risk is
disproved by code, tests, or documented constraints. Actively look for the
highest-impact failure modes first: data loss, security exposure, silent
failure, broken public contracts, rollout breakage, and missing validation.
Do not soften the search into a general quality pass.

Explicitly audit claims about un-diffed plumbing: when a PR description or comment
claims existing background plumbing already handles a new event, field, or state
transition ("already re-polls", "already listened to", "existing pipe handles this"),
treat the claim as an unverified hypothesis. Open and inspect the referenced producer
or consumer directly to verify its dirty-checking, filtering, and propagation logic.

Still keep the reporting bar high: only report specific, actionable findings
with file/line evidence. If a suspected issue cannot be supported, dismiss it
privately or list it as dismissed with the evidence that disproved it.

## Disposition Bar

Before posting, settle the finding's severity and intended disposition using
[Recover a blocked pass](../../REVIEW_WORKFLOW.md#recover-a-blocked-pass).
A planned follow-up must not be published as a blocking deferral.

Treat validity and actionability as separate decisions. A technically real
concern is not automatically worth changing the PR or growing the backlog.

Fix a confirmed finding only when the expected harm avoided clearly outweighs
the churn and regression risk of the fix. Judge that from concrete evidence:

- how likely a user is to reach the failing path in normal or reasonably
  foreseeable use;
- the impact when they do, the number of users or systems exposed, and whether
  recovery is possible;
- confidence in the root cause and in the proposed correction; and
- the size, complexity, compatibility cost, and regression risk of the change.

For security findings, require a credible exploit path: identify the reachable
boundary, attacker capability and preconditions, missing or bypassable control,
and resulting impact. A theoretical weakness, generic hardening opportunity, or
severity label without a plausible path to discovery and exploitation does not
by itself justify churn.

Create a GitHub issue only for an urgent follow-up: a concrete, high-impact
defect that is important enough to schedule within roughly the next two weeks,
but whose safe fix should not land in this PR. Do not create issues for ordinary
deferred backlog, speculative hardening, cleanup, or low-likelihood edge cases;
record those as `outcome=deferred` with the no-issue rationale if already posted,
or keep them out of the PR ledger when they do not clear the actionable finding
bar.

## Convergence Rounds

When the resolved stance is convergence, run only the lanes that can find a reason not to deploy:
the code reviewer, the silent failure hunter, and the security reviewer when its
signal is present. Drop the type/API design, comment/docs, PR test, and
tenant-coupling lanes. The preceding adversarial passes covered them, and
they audit a surface that regenerates every time it is hardened — guaranteed to
return work, guaranteed not to change what ships.

Brief those lanes exactly as an adversarial round does. They still report every
evidence-backed finding with severity attached; the narrowing is a disposition
rule applied when consolidating lane output, not an instruction to a lane to
withhold what it found.

The actionability bar tightens further. Change the PR only for a **blocking**
defect that also clears the Disposition Bar above — one that is realistically
reachable and ships materially wrong behavior, loses or corrupts data, exposes
a credible security or privacy exploit, breaks a public contract, or breaks
deploy or rollout:

- Fix a blocking finding with the smallest edit that clears it. No refactor, no
  rename, no new abstraction, no test or comment hardening alongside it.
- Defer every confirmed non-blocking finding and resolve its thread. Create and
  link an issue only when it clears the urgent-follow-up bar above; otherwise
  reply with `outcome=deferred` and a concise no-issue rationale. Deferral is the
  expected disposition here, not an admission of scope creep.
- Dismiss invalid findings with evidence, exactly as in an adversarial round.

The findings a convergence round defers may still be real. Fixing them in this
PR is the wrong call when the expected benefit does not justify moving the head
and re-staling the other engine's attestation. Land the change; let only urgent
follow-ups grow the backlog.

A convergence pass with no blocking defect posts its clean-pass attestation
and returns to the controller. Recommend the ship step only after the controller
verifies the remaining exact-head coverage and ledger; unused rounds are not owed.

## Mode

Mode follows the resolved review tier, not the caller's habit. The tier triggers
and the evidence rules live in
[`../../REVIEW_WORKFLOW.md`](../../REVIEW_WORKFLOW.md) under "Review Tier" and
are the only definition; this skill does not carry its own list. **Lean is the
default; Deep is the exception you justify.**

Resolve the effective `local-review-tier:v1` marker under the ledger's
authenticated, forward-only transition rule. If none exists, classify against
the workflow doc's triggers and post the marker before starting a lane. Lean is
the tier when no trigger matches.

- **Lean**: the default. Review correctness and add lenses only for signals in the diff, including security and tests for a bounded sensitive-path repair.
- **Deep**: examine the risks that selected Deep using the relevant lenses below. A `deep` argument handed down from `deepcritique` asserts that recorded tier; a direct human `deep` request is trigger 6 and posts a Deep replacement marker that preserves the recorded triggers and adds 6 before lanes start. Trace those risks beyond the edited lines; choose execution by the scope of that work.

Escalate mid-pass only on a confirmed finding that reaches a trigger, per the
workflow doc's evidence rule, and post the replacement marker naming it. A
suspicion is not evidence. State the resolved tier and the trigger that selected
it in the output.

The tenant-coupling lane catches one customer's values hardcoded into shared
logic and is selected when that signal is present. A diff that
materially changes customer/tenant-variable application behavior — vendor
integrations, branching or transformation driven by per-tenant configuration,
prompt/output generation, or data normalization — trips trigger 1's isolation
clause and is classified there rather than by a separate recommendation here. A
deployment-only value or reference to an existing runtime secret is not enough
unless the diff changes how that value is selected, authorized, transformed,
exposed, or stored.

## Lane Execution Ownership

Review lanes are read-only analysis workers. Every spawned lane prompt must say
that the lane may inspect source, diffs, existing tests, and existing CI results,
but must not run test suites, linters, formatters, builds, coverage, package
installation, or CI polling. If dynamic evidence is necessary, the lane returns
the smallest proposed probe to the orchestrator instead of executing it.

Mutation probes belong in an orchestrator-owned disposable copy, never the
shared review worktree. Lane agents must not edit source even temporarily.

The orchestrator owns command execution. After all lanes finish, deduplicate and
verify their hypotheses, apply any fixes, then run one consolidated validation
pass against the final head. Do not multiply the same validation across parallel
lanes.

Read the repo-local review addendum first. Check for
`.review/addendum.local.md` in the repository under review; if it exists, read it
before selecting lenses and fold each of its sections into the brief of the lens
it names. It is consumer-owned and never synced, which is what makes it safe to
append to — the role prompts and this skill are overwritten by the next sync, so
a lens learned from a review cycle survives only if it lands there. If a lesson
would be true of any codebase, it belongs in this skill or a role prompt
upstream instead; if it names this repo's flags, paths, or past incidents, it
belongs in the addendum.

## Review lenses and execution

Both tiers select lenses from the diff; the matrix is a menu, not an agent quota.
Always examine correctness. Add the following lenses when their signal exists:

| Signal                                                   | Lens                     |
| -------------------------------------------------------- | ------------------------ |
| Errors, async work, retries, fallbacks, partial failure  | Silent failure hunter    |
| Public types, API contracts, generics, compatibility     | Type/API design analyzer |
| Substantial comments, documentation, operational claims  | Comment/docs analyzer    |
| Changed tests or a repair that needs a regression guard  | PR test analyzer         |
| Secrets, auth, privacy, injection, trust boundaries      | Security reviewer        |
| Tenant-variable behavior, configuration or normalization | Tenant-coupling reviewer |

Deep requires tracing the affected boundary and realistic failure paths beyond
the edited lines, with evidence for every risk that selected the tier. It does
not require unrelated lenses or a minimum number of agents. Convergence rounds
narrow the selection as described above.

Use one direct pass when the selected scope is cohesive and manageable. Delegate
substantial independent tracks when they benefit from a fresh reader; combine
overlapping lenses, and use at most five subagents per pass unless the user
explicitly requests a larger roster. An explicit request for independent lanes
still applies. The skill name and tool availability alone do not require fan-out.

State the selected lenses, a short reason for the selection, and whether they
ran directly, delegated, or both. A direct pass is supported, not a failed or
degraded review; report it honestly. This execution choice does not change the
declared cross-engine roster, exact-head evidence, or round budget. Complete the
selected scope and required validation before attesting.

For delegated work, use the ledger's immutable packet, no inherited conversation
when supported, exact file scopes, and concise output limits. Keep findings
separate until the orchestrator verifies and deduplicates them. Load only the
role references needed for the selected lenses.

## Process

1. Load `.codex/references/local-review-ledger.md`.
   1a. Take the pass telemetry snapshot before reading or classifying anything, per
   `.codex/REVIEW_WORKFLOW.md` "Pass Telemetry". The reading and classification
   the steps below do is part of what the pass costs, so a snapshot taken later
   would quietly under-report it. The helper is a no-op when extraction is not
   enabled for this repository, and it reports the separate emission gate that
   decides whether this pass may publish a record at all.
2. Resolve the PR number, verify it is open and its head is the current branch,
   and require local HEAD, remote head, and PR head to match. If the branch has
   no PR, push it and open a draft PR before reviewing.
3. Read every prior review thread, including resolved and outdated threads,
   once at the orchestrator level before inspecting the current PR diff.
   Telemetry markers are not review context: exclude them by marker prefix and
   never carry them into a lane prompt or packet. When
   the caller supplies a pinned base SHA, resolve the reviewed head, changed-file
   list, and stat once, then build the ledger's immutable packet using the same
   literal `<base-sha>..<head-sha>` range for every lane. Do not make each lane
   reload the PR ledger.
4. Skip docs/config-only changes unless the user explicitly wants review. A
   skip finalizes its v3 result and then emits a `skipped` telemetry record: it
   still spent tokens reading and classifying the PR, and that overhead is worth
   seeing.
5. Read `AGENTS.md` and relevant path-specific instructions. Assign every lane
   the exact changed paths its lens needs, and have it pull path-scoped diffs per
   the ledger instead of receiving one pasted or stored whole diff.
6. Resolve the round and stance per "Stance Resolution". In a convergence round,
   the lane selection in "Convergence Rounds" narrows steps 7 and 8, and its
   fix bias replaces step 10. Every other step, including step 9, is unchanged.
7. Select lenses using "Review lenses and execution" and the resolved stance.
   Load only their matching files under `.codex/references/roles/`.
8. Review the selected scope directly or with the chosen independent workers.
   Keep worker findings separate until they finish, then deduplicate by root cause.

9. Verify and deduplicate lane findings against the source and complete PR
   ledger. For each confirmed root cause, use the deterministic ledger helper
   required by `.codex/references/local-review-ledger.md` to post one inline
   comment on an exact GitHub diff anchor before editing. Do not hand-compose
   review-comment API requests.
10. Apply the Disposition Bar. Fix only findings whose expected harm reduction
    justifies the churn. Defer the rest, and create an issue only for an urgent
    follow-up that should be scheduled within roughly two weeks.
11. Run targeted validation and commit. When
    `$AGENT_LOOP_REVIEW_PUSH_HELPER` is set, accumulate every same-pass fix in
    local commits and invoke the helper exactly once after the final fix;
    otherwise push normally with no force.
12. Use the ledger helper's resumable `dispose` transaction for every posted
    finding. Reconcile failures through the workflow's bounded recovery. On an
    uncertain helper response, retry only the identical command; correct a
    preflight rejection only when it is known to have performed no mutation.
    12a. Before the attestation, run the repository's gating suite unfiltered, per
    the ledger's "Validate before attesting". The targeted run in step 11
    dispositions findings and is not evidence for the pass. Name the command,
    config, and SHA in the attestation. A red gating run is itself a blocking
    finding, even when it predates this round, and applies to a `clean` pass
    just as much as a changed one.
13. Always use the ledger helper's `write-result` command to create the v3
    structured result at `$AGENT_LOOP_REVIEW_RESULT_FILE` when set. The outer
    wrapper validates it and owns the pass/completion attestation. Inside
    agent-loop, omit thread and transition files so the helper fetches and
    derives them. For a blocked pass, call `write-blocked-result` with an
    owner-only blocker file. Outside agent-loop, create the complete
    review-thread export and ordered
    forward-only before-to-after head list as private temporary files, use
    `write-result`, then use `attest --threads-file <path>
--allowed-heads-file <path>`. `attest` verifies the ledger and requires
    `--expected-result-sha256` from `validate-result` before publishing, so
    manual and automated passes share one protocol.

14. Once the v3 result is finalized and any fix commits are pushed, take the
    prompt-stack digests and emit this pass's telemetry record per
    `.codex/REVIEW_WORKFLOW.md` "Pass Telemetry", with `--pass-type review` and
    the status this pass reached. A record that cannot name the prompt
    generation it ran on cannot be compared against the next one, so the two
    digests are part of emitting, not an optional extra. Emission runs
    last because it must describe the finished pass, and it exits zero whether
    or not it succeeded: unlike every other step above, a telemetry failure is
    reported and never stops the pass, never retried into the review, and never
    changes the v3 result.

## Output

Reporting this pass's own measured spend is permitted; reading any earlier
pass's telemetry record is not.

End with:

- round and stance: `<n>` plus adversarial or convergence
- review depth: tier, selected lenses, and direct/delegated/mixed execution
- findings fixed
- findings deferred (with an issue link only for urgent follow-ups) or dismissed
  (with one-line evidence)
- validation run
- PR number, reviewed head, comments posted, replies posted, and threads resolved
- the next step under `.codex/REVIEW_WORKFLOW.md`: hand back to the outer relay
  controller for the declared reviewers that have not attested this head. This
  pass must not launch another reviewer or continue the relay itself. In auto
  mode the outer controller runs only the tested launcher matching the next
  missing declared reviewer (defaulting to Agy only when declaring a new
  relay); in handoff mode post
  `local-review-handoff:v1` and stop — and add `reviewit <pr>` /
  `reviewit <pr> deep` whenever a hosted pass is wanted. When recommending
  `reviewit`, recommend a fresh session; the current one has absorbed critique
  findings, fix commits, and coverage of the selected review scope.
