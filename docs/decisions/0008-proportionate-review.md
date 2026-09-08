# 0008 — Proportionate Codex review

- Status: accepted
- Date: 2026-09-08

## Decision

Codex selects review lenses from the changed behavior and the resolved tier.
It may cover a cohesive scope directly at either tier. It delegates substantial
independent tracks when a fresh reader adds value, with a default ceiling of
five workers. Explicit user requests for independent review still govern.
The canonical Codex selection and execution policy is in
`.codex/skills/critique/SKILL.md`; its wrappers reference that policy.

Deep means investigating the risk that selected it, including the surrounding
boundary and realistic failure paths. It does not imply six workers or six
separate reads. A direct pass can be complete without claiming independence.
The declared cross-engine roster, exact-head attestations, finding dispositions,
and validation requirements are unchanged.

Claude already selects relevant lenses instead of treating its matrix as a
minimum. Gemini's own lens-execution policy remains unchanged. Reviewers and
controllers assess a pass against its engine's selected scope and actual
execution, rather than importing another engine's agent-count expectations.
This is a supported execution difference, not evidence of a failed review.

All three workflow tier definitions allow a verified bounded repair to use
existing controls without automatically selecting the sensitive-path trigger.
Other Deep triggers remain independent. Initial tier selection happens before a run starts, so a tentative Deep guess
does not require a Deep pass to justify Lean. Existing runs retain their
supported transition/recovery flow, consumed rounds, and unresolved findings. Explicit human
Deep requests remain until the human changes them.

Cross-engine review reads the resolved tier rather than selecting Deep from its
skill name. Cleanup weighs benefit against churn and does not create work merely
to populate a matrix or preserve speculative suggestions in a backlog.

## Consequences

Completion reports name the tier, selected lenses, and direct/delegated/mixed
execution. They do not infer work from labels or tool availability. This change
does not retroactively attest earlier passes or resolve operational findings.

Behavioral validation should exercise bounded repairs, small trust-boundary
changes, concurrent runtime fixes, explicit Deep requests, cross-engine Lean
reviews, corrected classifications, and no-op cleanup. String presence alone
cannot prove these prompts lead to proportionate decisions.
