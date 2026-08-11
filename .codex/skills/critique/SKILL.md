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

## Fix Bias

Fix every valid finding in the current PR, including small nits and cleanup
items. Do not defer valid findings just because they are inconvenient or
"out of scope." Only dismiss invalid findings, false positives, or suggestions
that would make the code worse.

Defer only when the fix is a major architectural rework — roughly 300+ lines
or a cross-cutting redesign — and in that case file a GitHub issue at
deferral time rather than leaving the suggestion as an undocumented todo. A
"deferred" finding without a tracked issue is not allowed.

Reason: every valid finding that ships becomes the floor for the next PR in
this area. Letting them accrue as "deferred" turns the backlog into review
noise and makes future critiques more expensive.

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

The fix bias inverts. Change the PR only for a **blocking** defect — one that
ships wrong behavior, loses or corrupts data, opens a security or privacy hole,
breaks a public contract, or breaks deploy or rollout:

- Fix a blocking finding with the smallest edit that clears it. No refactor, no
  rename, no new abstraction, no test or comment hardening alongside it.
- Defer every confirmed non-blocking finding. File the GitHub issue, reply with
  `outcome=deferred` plus the link, and resolve the thread. Deferral is the
  expected disposition here, not an admission of scope creep, and the 300+ line
  threshold in the Fix Bias section does not apply to a convergence round.
- Dismiss invalid findings with evidence, exactly as in an adversarial round.

The findings a convergence round defers are usually real. Fixing them in this PR
is still the wrong call: each one moves the head, re-stales the other engine's
attestation, and buys another round of the same. Land the change and let the
issue carry the work.

A convergence round that finds no blocking defect ends the loop: post the
clean-pass attestation, recommend the repository's ship step, and list the
deferred issues.

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
10. Fix it unless it is invalid or a valid major architectural rework. Dismiss
    invalid findings with evidence. Defer only 300+ line or cross-cutting
    refactors, and track each deferral in a GitHub issue.
11. Run targeted validation, commit, and push with no force.
12. Use the ledger helper's resumable `dispose` transaction for every posted
    finding. Stop on any posting, push, disposition, or resolution failure; on
    an uncertain helper response, retry only the identical command.
13. Always write the v3 structured result to
    `$AGENT_LOOP_REVIEW_RESULT_FILE` when set. The outer wrapper validates it
    and owns the pass/completion attestation. Outside agent-loop, create the
    same result, complete review-thread export, and ordered forward-only
    before-to-after head list as private temporary files, then use the helper's
    `attest --threads-file <path> --allowed-heads-file <path>` command. `attest`
    verifies the ledger and requires `--expected-result-sha256` from
    `validate-result` before publishing, so manual and automated passes share
    one protocol.

## Output

End with:

- round and stance: `<n>` plus adversarial or convergence
- review depth: lean with independent subagents, lean local two-pass fallback, deep with independent subagents, or deep local multi-pass fallback
- findings fixed
- findings deferred (with linked GitHub issue) or dismissed (with one-line evidence)
- validation run
- PR number, reviewed head, comments posted, replies posted, and threads resolved
- the next step for the path selected in `.codex/REVIEW_WORKFLOW.md`: return to
  the local Codex/Claude convergence loop after completing the PR ledger, or use
  `reviewit <pr>` / `reviewit <pr> deep` on the hosted
  fallback path. When recommending `reviewit`, recommend a fresh Codex session;
  the current session has absorbed critique findings, fix commits, and (in deep
  mode) the full review matrix.
