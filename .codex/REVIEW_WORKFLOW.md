# Review Workflow

This file is synced from `codex-platform` into consumer repos. Consumer-specific
edits will be overwritten on the next sync.

## PR-First Rule

Open a draft pull request before any structured review cleanup such as
`refactorpass`, or adversarial review. Local Codex and Claude passes use GitHub
review threads as durable shared context:
post each verified finding inline before editing, push the correction, reply
with the fix and validation, then resolve the thread. Every pass must read
resolved as well as unresolved threads before reviewing the current head.

Load [the local review ledger](references/local-review-ledger.md) before running
`refactorpass`, `critique`, `deepcritique`, `pr-critique`, or local review hooks.
That file is the engine-neutral protocol, vendored verbatim from
[`@loomantix/review-ledger`](https://www.npmjs.com/package/@loomantix/review-ledger)
so every engine reads the same contract. Where it writes `<ledger-helper>`, this
engine's path is:

```text
.codex/skills/critique/scripts/review-ledger.js
```

## Roles, Not Engine Names

The relay is defined over two roles:

- **author** — the engine that wrote the change. Exactly one.
- **reviewer** — an engine that reads the change cold. Zero, one, or two.

Codex is the author role when Codex wrote the change and a reviewer role
otherwise. No rule below names a specific engine, so adding a fourth changes
nothing here.

**One non-author reviewer is the recommended floor** and covers the great
majority of changes. A second earns its cost mainly where a defect is expensive
and hard to see: auth, crypto, secret handling, schema and data-shape work,
release and sync tooling, or a change whose blast radius crosses repositories.
Solo review is permitted but must be declared with a recorded reason.

The local relay and the hosted reviewers are **not competing paths** and no
longer need to be chosen between. See "Hosted Reviewers" below.

## Review Relay

1. Make the change, run focused validation, and create a clean local commit.
2. Push the feature branch and open or reuse its draft PR. Record the PR number,
   head SHA, and all existing review threads before any reviewer runs.
3. Declare the roster with the ledger helper's `post-roster`, naming the author
   engine and this PR's reviewer engines. Participation is declared, never
   inferred: an engine that has not attested is otherwise indistinguishable from
   one that was never going to run, so nothing downstream can tell an incomplete
   round from a finished one. A solo relay is `--reviewers none` with the reason
   in the content file.
4. Fetch the target base, record its immutable commit SHA, and give that exact
   SHA to every reviewer for the round. No reviewer may re-resolve a mutable
   remote-tracking ref independently.
5. Run each declared reviewer in a fresh session against the current head. Read
   the PR ledger, post every confirmed finding inline before editing, fix,
   validate, commit, push, reply, and resolve. Codex's lane is
   `deepcritique <pr-number>` when Codex is the author engine and
   `pr-critique <pr-number>` when it is reviewing another engine's change; other
   engines use their own equivalents. Reviewer order within a round is a
   scheduling choice, not a protocol rule — what matters is which commit each
   one read.
6. Classify committed review fixes as `material` or `minor`. A material fix
   affects behavior, correctness, security/privacy, data safety, compatibility,
   deployment/sync integrity, or another substantive contract. Minor-only fixes
   are validated and kept.

   **The chain gets cheaper as it repeats.** Three rules make that happen, and
   all are derived from the ledger so a fresh session reaches the same answer:
   - **The refactor pass runs once per engine per PR.** A second cleanup pass over
     an already-simplified diff returns naming and shape churn, which moves the
     head and invalidates the other engines' attestations for nothing that ships.
     Each engine's cleanup lane latches on a `local-review-refactor:v1` marker;
     a docs/config-only skip does not consume it.
   - **A fix invalidates by head, not by position.** An attestation is evidence
     for the exact commit it names. A material fix does not restart the round at
     some first engine; it moves the head, which invalidates precisely those
     attestations that named the old head. An engine that already attested the
     post-fix commit stays valid and does not re-run. This is what keeps a
     second reviewer from costing a full extra round every time anything
     changes.
   - **Rounds 1–2 are adversarial; round 3 and later are convergence rounds.**
     Once every declared reviewer has read the change cold twice, the remaining findings
     are mostly about the review's own artifacts. A convergence round runs only
     the lanes that can find a reason not to deploy, changes the PR only for a
     realistically reachable blocking defect, defers everything else, creates
     an issue only for an urgent high-impact follow-up, and ends the loop as soon
     as it finds no blocker. Lanes still report everything they find —
     the narrowing is a disposition rule applied when consolidating lane output,
     never an instruction to a lane to withhold what it found.

7. Cap the loop at four rounds unless the consumer explicitly configures a
   different positive bound. At cap exhaustion, stop, preserve the branch,
   worktree, and draft PR, and report non-convergence. Do not mark it ready.
8. Converge when `verify-coverage` passes at the exact current head — a roster
   is declared and every declared reviewer holds an attestation naming that
   head — the round that produced those attestations had no material fix, and
   every local-review thread contains a disposition reply and is resolved.
   Revalidate the exact PR head, then mark the PR ready.

The author engine's own adversarial pass never counts toward coverage. It
re-reads the change while still holding the rationale that produced it, which is
the opposite of the cold read the relay exists to obtain. `coverage` reports it
as `authorAttested` so the fact stays visible, but the tier counts distinct
non-author engines only.

The `agent-loop` skill automates this path with a required non-mutating
validation hook plus `review_max_rounds`, `codex_review_hook`, and
`claude_review_hook`. Under contract v3 every hook writes a structured clean,
changed, or blocked result to `$AGENT_LOOP_REVIEW_RESULT_FILE`. The wrapper
validates that result against observed Git state and the v3 ledger, then posts
the canonical pass/completion attestation itself. It opens a draft PR before
review, exports the pinned PR identity to both hooks, checkpoints private atomic
run state, and verifies that each hook leaves local, remote, and PR heads
aligned. Consumer hooks own semantic finding verification, deterministic inline
posting and disposition, and classification; they must fail or return blocked
if a valid finding or undisposed local-review thread remains.

## Hosted Reviewers

Hosted AI reviewers — the Gemini Flash and Copilot passes `reviewit` drives on
the PR itself — are a **different style of review**, not a fallback and not a
later phase. Run one whenever it is useful: before the relay, between rounds,
after convergence, or as the only review on a change that does not warrant a
local relay.

A hosted pass **invalidates nothing on its own.** Only a commit invalidates, and
only by the head rule in step 6, which treats a hosted-review fix exactly like
any other:

- a minor fix leaves attestations at the old head stale for the ordinary reason,
  and the affected engines re-run when the relay next needs them;
- a material fix means the round had a material transition and does not
  converge, the same as if a local reviewer had made it.

Classify a hosted-review fix by its effect on the code, with the same
material/minor rule as everything else. "A hosted reviewer touched this" is not
a category.

Hosted reviewers are not roster participants. They post under their own
identities, so their comments are context rather than actor-owned ledger
evidence, and they do not attest. Coverage counts local engines only — a hosted
pass does not turn a solo relay into a cross-model one.

### Lean

1. Make the local change, create a clean commit, and open a draft PR.
2. Run `refactorpass <pr-number>` for source changes.
3. Run `critique <pr-number>`. Lean mode executes the code-reviewer and silent
   failure-hunter lanes, using independent subagents when available.
4. Run `reviewit <pr-number>`. It triggers Gemini Flash and Copilot, verifies and
   deduplicates their findings, fixes confirmed issues, pushes, replies, and
   loops within its configured cap.

### Deep

1. Open a draft PR, then run `deepcritique <pr-number>`. It executes `critique deep`'s
   six core lanes and the conditional tenant-coupling lane, preceded by
   `refactorpass` on this engine's first pass over the PR.
2. Run `reviewit <pr-number> deep`. Deep mode uses the same hosted reviewers
   with its larger cap, early-exit rules, and final fresh Codex `deepcritique`.
   That tail `deepcritique` skips the refactor pass — step 1 already spent this
   engine's cleanup latch on the PR.

Choose deep when the change touches auth, crypto, secret handling, schema/data
shape, GitHub Actions, sync tooling, `.codex/skills/**`, a large refactor, an
area with recurring incidents, or customer/tenant-variable behavior.

## Skip Path

For docs/config-only changes, skip expensive review automation unless the user
explicitly wants it. Source-code changes include common implementation
extensions such as `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`,
`.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, and `.bash`.

## Reviewing Another Engine's Change

When Codex holds the reviewer role, run `pr-critique <pr-number>` from an
isolated worktree. It reads the existing ledger, runs the deep matrix, posts
confirmed findings inline, applies fixes, and completes those same threads.

If that pass commits a fix, it moves the head and invalidates the attestations
that named the old commit — including the author engine's. Those engines re-run
against the new head; engines that had not yet attested are unaffected. The
hand-back is the head rule, not a separate obligation.

## Review Principles

- Treat every generated finding as a hypothesis. Verify it against code, tests,
  and documented constraints before posting or changing anything.
- Fix a confirmed finding only when the likelihood and impact of real user harm,
  or a credible path to security exploitation, justify the fix's churn and
  regression risk.
- Create a follow-up issue only for a concrete, high-impact defect that should
  be scheduled within roughly two weeks. Record ordinary deferrals without an
  issue; do not turn speculative hardening, cleanup, or low-likelihood edge cases
  into backlog.
- A review fix without a preceding inline finding, a finding without a reply,
  or a resolved thread without a visible disposition is a failed review pass.
- Never copy sensitive source, credentials, private data, or model logs into PR
  metadata. Only the concise verified finding and its disposition belong there.
- Stop at the configured cap and preserve recovery state when reviewers do not
  converge.
