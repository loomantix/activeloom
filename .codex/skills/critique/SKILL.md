---
name: critique
description: PR-first adversarial code review for Codex. Use after implementation or refactorpass on an open draft PR, especially when the user asks to critique, review hard, find bugs, or run the platform review chain. Posts verified findings inline before fixing, then replies and resolves. Supports lean and deep modes.
---

# Critique

Review an open draft PR adversarially. The goal is to catch bugs, missing tests,
security issues, and convention violations while preserving every verified
finding and disposition in the PR.

## Context Window Check

Run this check before anything else. `critique` runs adversarial review lanes—two in lean mode, six core lanes in deep mode, plus a conditional tenant-coupling lane—each of which reads the diff, reads changed files, and produces structured findings. When subagents/delegation are available the lanes run in parallel, and each subagent inherits cache state from this session; when subagents are not available the lanes run as serial local passes that compete for the same context. Either way, if the current Codex session has already been heavily used for feature implementation, the lanes start with sharply reduced working windows and `critique` (especially `critique deep`) runs slower and more expensively.

Assess honestly:

- Has this session been writing/editing the feature about to be critiqued? Long conversation, many file edits, dense planning?
- Is the conversation about to brush against compaction territory?

If either is yes, stop and tell the user:

> Your context is heavy from the implementation work. Start a new Codex session and run `critique` (or `deepcritique`) there. `critique deep`'s full matrix especially needs cache headroom and a fresh session makes the chain materially cheaper.

Do not proceed in the current session unless the user explicitly overrides.

## Stance Resolution

Resolve this engine's round number per `.codex/references/local-review-ledger.md`
before selecting lanes: use `$AGENT_LOOP_REVIEW_ROUND` when the runner set it,
take it from an invoking `deepcritique`, or count the `local-review-pass:v3` and
`local-review-complete:v3` markers on the PR naming `engine=codex` and add one.

- **Rounds 1–2 run adversarially.** The stance, matrices, and fix bias below
  apply as written.
- **Round 3 and later run in convergence mode.** Both engines have read the
  change cold twice; the goal moves from challenging it to landing it. See
  "Convergence Rounds" below — it overrides the lane selection and the fix bias,
  and nothing else. The post-before-editing, reply, and resolve contract is
  unchanged, and the round cap does not move.

State the resolved round and stance in the output.

## Adversarial Stance

Assume there are problems to find. Treat the diff as guilty until each risk is
disproved by code, tests, or documented constraints. Actively look for the
highest-impact failure modes first: data loss, security exposure, silent
failure, broken public contracts, rollout breakage, and missing validation.
Do not soften the search into a general quality pass.

Still keep the reporting bar high: only report specific, actionable findings
with file/line evidence. If a suspected issue cannot be supported, dismiss it
privately or list it as dismissed with the evidence that disproved it.

## Disposition Bar

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

In round 3 and later, run only the lanes that can find a reason not to deploy:
the code reviewer, the silent failure hunter, and the security reviewer when its
signal is present. Drop the type/API design, comment/docs, PR test, and
tenant-coupling lanes. They found what they were going to find in rounds 1–2, and
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

A convergence round that finds no blocking defect ends the loop: post the
clean-pass attestation, recommend the repository's ship step, and list any
urgent deferred issues.

## Mode

- **Lean**: default. Run the lean two-lane review: code reviewer plus silent failure hunter. This is still an adversarial PR review, not a casual skim.
- **Deep**: if the user passes `deep` or the change is high-risk. Run the full independent review matrix below. Deep mode is intentionally much heavier than lean mode; do not collapse it into one general review pass.

If the diff touches customer/tenant-variable behavior—vendor integrations, per-tenant configuration, prompt/output generation, or data normalization—recommend deep mode. The tenant-coupling lens that catches one customer's values hardcoded into shared logic is intentionally not part of the lean two-lane set.

## Lane Execution Ownership

Review lanes are read-only analysis workers. Every spawned lane prompt must say
that the lane may inspect source, diffs, existing tests, and existing CI results,
but must not run test suites, linters, formatters, builds, coverage, package
installation, or CI polling. If dynamic evidence is necessary, the lane returns
the smallest proposed probe to the orchestrator instead of executing it.

The orchestrator owns command execution. After all lanes finish, deduplicate and
verify their hypotheses, apply any fixes, then run one consolidated validation
pass against the final head. Do not multiply the same validation across parallel
lanes.

## Lean Review Matrix

Lean mode must cover two independent lanes:

1. **Code reviewer** — correctness bugs, regressions, edge cases, broken contracts, project conventions, and meaningful test gaps.
2. **Silent failure hunter** — swallowed errors, partial failures, async races, retries, timeouts, idempotency, and missing observability for critical paths.

Run these lanes as independently as the active runtime permits:

- If subagents/delegation are available and permitted by the active Codex instructions, spawn independent reviewers for both lanes using the ledger's immutable review packet and scoped diff-delivery contract. Keep the packet prefix byte-identical, append only the lens and exact file scope, use no inherited conversation history when supported, and impose a concise output ceiling. Tell each reviewer to return only actionable findings with file/line evidence and avoid relying on conclusions from the other lane.
- If subagents are unavailable or not permitted, perform two separate local passes using the lane prompts above. Do not present that as equivalent to independent subagents.
- If lean mode was requested but independent subagents could not be used, explicitly say so in the output under `review depth`.

## Deep Review Matrix

Deep mode must cover six core independent lanes, plus the conditional tenant-coupling lane when its signal is present:

1. **Code reviewer** — correctness bugs, regressions, edge cases, and broken contracts.
2. **Silent failure hunter** — swallowed errors, partial failures, async races, retries, timeouts, idempotency, and observability gaps.
3. **Type/API design analyzer** — public API shape, type soundness, compatibility, dependency boundaries, and versioning drift.
4. **Comment/docs analyzer** — misleading comments, stale docs, migration instructions, public/private information leaks, and docs that overpromise behavior.
5. **PR test analyzer** — missing tests, weak assertions, CI gaps, fixture realism, and whether validation actually exercises the risk.
6. **Security reviewer** — auth, secrets, injection, supply-chain, workflow permissions, sensitive-data exposure, and fail-closed behavior.
7. **Tenant-coupling reviewer (conditional)** — literals or branches that encode one customer's data, configuration, or vocabulary into shared logic. For every suspicious value ask: _would this still be correct for a second customer with different values?_ If not, move the value to configuration/data with a safe default. Ignore genuinely universal protocol constants, standard enums, and framework keys.

Run these lanes as independently as the active runtime permits:

- Invoking `critique deep` is an explicit request to use independent subagents for
  every applicable lane whenever the active runtime exposes subagent/delegation tools.
  Do not require the user to separately say "use subagents" before spawning
  those lane reviewers.
- If subagents/delegation are available and permitted by the active Codex instructions, spawn independent reviewers using the ledger's immutable review packet and scoped diff-delivery contract. Keep the packet prefix byte-identical, append only the disjoint lens and exact file scope, use no inherited conversation history when supported, and impose a concise output ceiling. Tell each reviewer to return only actionable findings with file/line evidence and avoid relying on conclusions from other lanes.
- If subagents are unavailable or not permitted, perform a separate local pass for every applicable lane using the prompts above. Do not present that as equivalent to independent subagents.
- If deep mode was requested but independent subagents could not be used, explicitly say so in the output under `review depth`.
- Run the tenant-coupling lane as a separate use of the code-reviewer role with the narrow prompt above; do not dilute it into the general correctness lane.

## Process

1. Load `.codex/references/local-review-ledger.md`.
2. Resolve the PR number, verify it is open and its head is the current branch,
   and require local HEAD, remote head, and PR head to match. If the branch has
   no PR, push it and open a draft PR before reviewing.
3. Read every prior review thread, including resolved and outdated threads,
   once at the orchestrator level before inspecting the current PR diff. When
   the caller supplies a pinned base SHA, resolve the reviewed head, changed-file
   list, and stat once, then build the ledger's immutable packet using the same
   literal `<base-sha>..<head-sha>` range for every lane. Do not make each lane
   reload the PR ledger.
4. Skip docs/config-only changes unless the user explicitly wants review.
5. Read `AGENTS.md` and relevant path-specific instructions. Assign every lane
   the exact changed paths its lens needs, and have it pull path-scoped diffs per
   the ledger instead of receiving one pasted or stored whole diff.
6. Resolve the round and stance per "Stance Resolution". In a convergence round,
   the lane list in "Convergence Rounds" replaces steps 7 and 8, and its inverted
   fix bias replaces step 10. Every other step, including step 9, is unchanged.
7. In lean mode, execute every lane in the Lean Review Matrix. Load these role references for lane prompts:
   - `.codex/references/roles/code-reviewer.md`
   - `.codex/references/roles/silent-failure-hunter.md`
     Keep lane findings separated until both lanes complete, then deduplicate by root cause.
8. In deep mode, execute every lane in the Deep Review Matrix. Load these role references for lane prompts:
   - `.codex/references/roles/code-reviewer.md`
   - `.codex/references/roles/silent-failure-hunter.md`
   - `.codex/references/roles/type-design-analyzer.md`
   - `.codex/references/roles/comment-analyzer.md`
   - `.codex/references/roles/pr-test-analyzer.md`
   - `.codex/references/roles/security-reviewer.md`
     When the tenant-coupling signal is present, load `.codex/references/roles/code-reviewer.md` again for the dedicated conditional pass. Keep lane findings separated until all lanes complete, then deduplicate by root cause.
9. Verify and deduplicate lane findings against the source and complete PR
   ledger. For each confirmed root cause, use the deterministic ledger helper
   required by `.codex/references/local-review-ledger.md` to post one inline
   comment on an exact GitHub diff anchor before editing. Do not hand-compose
   review-comment API requests.
10. Apply the Disposition Bar. Fix only findings whose expected harm reduction
    justifies the churn. Defer the rest, and create an issue only for an urgent
    follow-up that should be scheduled within roughly two weeks.
11. Run targeted validation and commit. Publish through
    `$AGENT_LOOP_REVIEW_PUSH_HELPER` when it is set; otherwise push normally
    with no force.
12. Use the ledger helper's resumable `dispose` transaction for every posted
    finding. Stop on any posting, push, disposition, or resolution failure; on
    an uncertain helper response, retry only the identical command.
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

## Output

End with:

- round and stance: `<n>` plus adversarial or convergence
- review depth: lean with independent subagents, lean local two-pass fallback, deep with independent subagents, or deep local multi-pass fallback
- findings fixed
- findings deferred (with an issue link only for urgent follow-ups) or dismissed
  (with one-line evidence)
- validation run
- PR number, reviewed head, comments posted, replies posted, and threads resolved
- the next step under `.codex/REVIEW_WORKFLOW.md`: hand back to the relay for
  the declared reviewers that have not attested this head — in auto mode run
  only the tested launcher matching each missing declared reviewer (defaulting
  to Agy only when declaring a new relay), in handoff mode post
  `local-review-handoff:v1` and stop — and add `reviewit <pr>` /
  `reviewit <pr> deep` whenever a hosted pass is wanted. When recommending
  `reviewit`, recommend a fresh session; the current one has absorbed critique
  findings, fix commits, and (in deep mode) the full review matrix.
