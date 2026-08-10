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

Parse `$ARGUMENTS` for an optional PR number and the word `deep`.

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

- **Rounds 1–2 run adversarially** — the matrix and dispositions below apply as
  written.
- **Round 3 and later run in convergence mode.** Both engines have read the
  change cold twice; the goal moves from challenging it to landing it. Convergence
  mode overrides the lens table and the fix bias, as set out under "Convergence
  rounds" below. It does not change the post-before-editing, reply, or resolve
  contract, and it does not raise the round cap.

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
5. Record the exact PR base and head SHAs. Read every prior review thread,
   including resolved and outdated threads.
6. Skip docs/config-only changesets, per the ledger's changeset classification.
   Finalize a clean v3 result using the ledger's wrapper/standalone ownership
   rule before returning.

Do not begin a reviewer until the PR ledger is available. Do not use a
force-push to establish or update the review branch.

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
and impose a concise output ceiling. Do not ask finders to suppress findings by
confidence. Run selected agents in parallel; the orchestrator verifies them.

Brief each finder per the ledger's diff-delivery rules: resolve the changed-file
list once, name the paths that lens owns, and prefer `git diff <base-sha>..HEAD
-- <path>` over handing every agent one whole-diff artifact. Scope a lens by the
files it reviews, never by the findings it may report.

### Convergence rounds (round 3 and later)

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
diff anchor exists, keep it out of the automated fix loop or track a genuinely
architectural follow-up as the ledger requires.

If no new confirmed finding survives and the enclosing review hook did not move
the head, finalize a `clean` v3 result per the ledger. When deepcritique's earlier
refactorpass committed, preserve the enclosing hook's original before SHA and
finalize `changed` with classification `minor` and an empty finding set; the
committed refactor latch supplies the evidence. Under agent-loop the wrapper
owns the canonical pass attestation; a standalone pass must attest through the
helper before reporting completion.

## Phase 3: Disposition and fixes

In an adversarial round, apply the fix-everything-valid bias:

- Fix a confirmed finding in this PR.
- Dismiss only a false positive or a change that would make the code worse.
  Reply with evidence and resolve the thread without editing.
- Defer only a genuinely architectural change. File an issue, reply with its
  link and rationale, then resolve the thread.

In a convergence round, the bias inverts toward landing the change. Change the PR
only for a **blocking** defect — one that ships wrong behavior, loses or corrupts
data, opens a security or privacy hole, breaks a public contract, or breaks
deploy or rollout:

- Fix a blocking finding with the smallest edit that clears it. No refactor, no
  rename, no new abstraction, no test or comment hardening alongside it.
- Defer every confirmed non-blocking finding. File the issue, reply with
  `outcome=deferred` plus the link, and resolve the thread. Deferral here is the
  expected disposition, not an admission of scope creep.
- Dismiss false positives exactly as above.

The findings a convergence round defers are usually real. Fixing them in this PR
is still the wrong call: each one moves the head, re-stales the other engine's
attestation, and buys another round of the same. Land the change and let the
issue carry the work.

For confirmed fixes:

1. make the smallest safe edit;
2. run focused validation;
3. commit conventionally and push normally;
4. require local HEAD, remote head, and PR head to match;
5. use the deterministic helper's resumable `dispose` transaction with the fix
   SHA, validation result, fingerprint, and occurrence;
6. after the final lane, write the v3 structured result. Under agent-loop the
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

Every engine attestation remains exact to the head it reviewed. A later minor
fix does not rewrite that evidence or claim the other engine reviewed the new
head. The outer round may still converge through an explicit minor transition;
a material transition restarts at Codex.

## Phase 4: Output

Report the PR and reviewed head, the resolved round and stance, mode and lenses,
disposition/thread counts, validation, fix SHAs, and whether material fixes
require another local-engine pass.

A convergence round that found no blocking defect ends the loop: record a clean
result, recommend the ship step, and list the deferred issues.

If this Claude pass made a material fix, restart the bounded round at
`/codex-review <pr-number>` in a fresh session. Otherwise it completes the
Claude half of the current round. Always finalize `clean`, `changed`, or
`blocked` per the ledger's wrapper/standalone ownership rule before returning.

## Boundaries

- Do not force-push or merge.
- Do not invoke hosted reviewers on the local convergence path.
- Do not print raw model logs in the PR.
- Do not mark the PR ready while marked threads are unanswered or unresolved.

## Source of truth

This skill lives upstream at `.claude/skills/critique/` and is synced to consumer
repositories.
