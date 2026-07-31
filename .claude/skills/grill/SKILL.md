---
name: grill
description: PR-first adversarial review. Lean mode runs the highest-signal local Claude lenses; `deep` selects the full relevant matrix. Verified findings are posted inline before fixes, then replied to and resolved after a pushed fix.
argument-hint: (optional PR number and/or "deep")
---

# /grill — PR-first adversarial review

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
6. Skip docs/config-only changesets, per the ledger's changeset
   classification.

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

If no new confirmed finding survives and the pass makes no commit, post a
clean-pass PR review attestation with `engine=claude` and the exact reviewed
head SHA. A fix pass attests through its thread replies instead.

## Phase 3: Disposition and fixes

Apply the fix-everything-valid bias:

- Fix a confirmed finding in this PR.
- Dismiss only a false positive or a change that would make the code worse.
  Reply with evidence and resolve the thread without editing.
- Defer only a genuinely architectural change. File an issue, reply with its
  link and rationale, then resolve the thread.

For confirmed fixes:

1. make the smallest safe edit;
2. run focused validation;
3. commit conventionally and push normally;
4. require local HEAD, remote head, and PR head to match;
5. reply to each thread with the fix SHA, validation result, and structured
   `outcome=fixed` marker;
6. post the committed-pass completion marker after the final adversarial lane;
7. resolve each replied thread.

Never resolve a finding merely because code changed. A marked thread requires
both a response and an explicit resolution. Never batch unrelated findings
into one ambiguous thread.

Material fixes invalidate prior clean-pass attestations and require the next
local engine to review the new exact head. Minor-only fixes do not by
themselves start an unbounded new round.

Classify by what the fix **changes**, not by how severe the finding sounded: a
fix is material only when it changes product code. A pass whose commits touch
only tests, fixtures, comments, or docs is minor-only, and the other engine's
attestation still covers the head — confirm by diffing the attested SHA against
the current head over product paths.

If this pass changed no product code, stop the loop and recommend the ship step —
whatever this repo uses to merge the PR. A round that finds only test and comment
work means the product converged and the review is now auditing its own
artifacts; that surface regenerates every time you harden it, so another round is
guaranteed to find more and equally guaranteed not to improve what ships.

## Phase 4: Output

Report the PR and reviewed head, mode and lenses, disposition/thread counts,
validation, fix SHAs, and whether material fixes require another local-engine
pass.

If this Claude pass made a material fix, restart the bounded round at
`/codex-review <pr-number>` in a fresh session. Otherwise it completes the
Claude half of the current round — and if it changed no product code at all,
report the PR as converged and recommend the ship step rather than implying
another round is owed.

## Boundaries

- Do not force-push or merge.
- Do not invoke hosted reviewers on the local convergence path.
- Do not print raw model logs in the PR.
- Do not mark the PR ready while marked threads are unanswered or unresolved.

## Source of truth

This skill lives upstream at `.claude/skills/grill/` and is synced to consumer
repositories.
