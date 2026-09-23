# 0013 — Proportionate review sizing and triage

- Status: accepted
- Date: 2026-09-23

## Context

Review chains across Claude, Codex, and Gemini provide high-assurance verification,
but can cause excessive review overhead when applied indiscriminately to small or
low-risk changes.

In practice, pull requests that touch only non-runtime developer tooling (such as
isolated test runner scripts, local build configurations, or documentation) can
fail the automated `classify-changeset` gate because files like `package.json` or
test files are classified as review-significant (`app` or `test`) by filename
heuristics. Furthermore, casual user instructions (such as "please run an auto
review chain on PR #X") have been mistakenly interpreted by agents as explicit
requests for Deep review (Trigger 6) or as authorization to run an expensive
multi-engine cyclic relay (`--cycle --until-converged`).

Because `--cycle` requires strictly more than one full cycle to establish
convergence (a minimum of 3 passes: Initiator → Reviewer → Initiator return pass),
running an automatic cycle over a 10-line test script or documentation adjustment
wastes substantial model context, token budgets, and runtime.

## Decision

This record establishes explicit **Review Sizing and Proportionality Triage** across
all harnesses and controllers:

1. **Review Sizing Ladder**:
   - **Human glance (0 AI passes)**: The appropriate outcome for diffs that carry
     no review-significant files, as well as trivial diffs modifying only
     non-runtime developer tooling, test execution scripts, documentation, or
     minor non-behavioral configurations. Even when filename heuristics report
     `reviewSignificant: true`, agents and controllers must evaluate the actual
     delta: if the change has no runtime behavior, no public contract change, and
     a failure blast radius confined to local developer tooling, agents must
     recommend human glance rather than launching an AI review chain.
   - **Single second-model review (1 AI pass)**: The standard Lean default. A
     single independent non-author reviewer reading the diff cold (via
     `pr-critique` or `--chain <single-reviewer>`) catches the vast majority of
     reachable defects in a single pass. Lean review does not require and must not
     default to an open-ended multi-pass convergence cycle.
   - **Finite multi-reviewer chain (`--chain <rev1>,<rev2>`)**: When two distinct
     reviewer perspectives are genuinely beneficial on a Lean change, use an
     exact, finite step sequence without cycle repeats.
   - **Cyclic multi-engine relay (`--cycle --until-converged`)**: Strictly reserved
     for changes where the review tier resolved to **Deep** (Triggers 1–6) or
     complex multi-actor changes where multi-engine consensus is justified.
     Defaulting to `--cycle` on Lean or trivial diffs is prohibited.

2. **Operational Requests vs. Risk Escalation**:
   - Casual user prompts (such as "run review", "review this PR", or "run an auto
     review chain of codex and gemini") are routine task invocations. They do
     **not** constitute an explicit assertion of Deep risk (Trigger 6), do **not**
     override human glance, and do **not** authorize a multi-engine cyclic relay.
   - The agent must triage the diff first, determine proportionate sizing, and
     recommend or execute the appropriate tier and plan. Trigger 6 applies only
     when a human explicitly requests a _deep_ review or insists on an expanded
     chain after being presented with the proportionate sizing recommendation.

3. **Interactive Sizing Guidance**:
   - Interactive review entry points (`critique`, `deepcritique`, `refactorpass`)
     must inspect the diff during pre-flight. If the delta qualifies for human
     glance sizing, the agent must advise the user before spending review tokens.

## Consequences

- Review token consumption and latency for minor tooling and configuration
  pull requests are sharply reduced.
- The distinction between task invocation and risk escalation is clarified in
  `REVIEW_WORKFLOW.md` across all harnesses.
- Automated chain runners and interactive sessions default Lean reviews to
  single-model or finite chains rather than unbounded cycles.
