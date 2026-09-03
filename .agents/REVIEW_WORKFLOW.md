# Review Workflow

This file is synced from `gemini-platform` into consumer repos. Consumer-specific
edits will be overwritten on the next sync.

## PR-First Rule

Open a draft pull request before any structured review cleanup such as
`refactorpass`, or adversarial review. Review passes use GitHub review threads
as durable shared context: post each verified finding inline before editing, push
the correction, reply with the fix and validation, then resolve the thread. Every
pass must read resolved as well as unresolved threads before reviewing the current head.

Load [the local review ledger](references/local-review-ledger.md) before running
`refactorpass`, `critique`, `deepcritique`, `pr-critique`, or local review hooks.
That file is the engine-neutral protocol published by the
[`@loomantix/review-ledger`](https://www.npmjs.com/package/@loomantix/review-ledger)
project and vendored verbatim into every engine repository, so all engines read
the same contract. The helper bundle beside it is vendored from that package's
published tarball and pinned by `review-ledger.version` and
`review-ledger.integrity`; CI byte-compares the bundle, not this document, so a
protocol edit must land upstream rather than here. Where the protocol writes
`<ledger-helper>`, this engine's path is:

```text
.agents/skills/critique/scripts/review-ledger.js
```

## Roles, Not Engine Names

The relay is defined over two roles:

- **author** — the engine that wrote the change. Exactly one.
- **reviewer** — an engine that reads the change cold. Zero, one, or more.

Antigravity/Gemini is the author role when it wrote the change and a reviewer
role otherwise. No rule below names a specific engine, so adding a fourth
changes nothing here.

**One non-author reviewer is the recommended floor** and covers the great
majority of changes. A second earns its cost mainly where a defect is expensive
and hard to see: auth, crypto, secret handling, schema and data-shape work,
release and sync tooling, or a change whose blast radius crosses repositories.
Solo review is permitted but must be declared with a recorded reason.

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
   validate, commit, push, reply, and resolve. This engine's lane is
   `deepcritique <pr-number>` when it is the author engine and
   `pr-critique <pr-number>` when it is reviewing another engine's change; other
   engines use their own equivalents. Reviewer order within a round is a
   scheduling choice, not a protocol rule — what matters is which commit each
   one read.
6. Classify committed review fixes as `material` or `minor`. A material fix
   affects behavior, correctness, security/privacy, data safety, compatibility,
   deployment/sync integrity, or another substantive contract. Minor-only fixes
   are validated and kept.

   **The chain gets cheaper as it repeats:**
   - **The refactor pass runs once per engine per PR.** A second cleanup pass over
     an already-simplified diff returns naming and shape churn, which moves the
     head and invalidates the other engines' attestations for nothing that ships.
   - **A fix invalidates by head, not by position.** An attestation is evidence
     for the exact commit it names. A material fix does not restart the round at
     some first engine; it moves the head, which invalidates precisely those
     attestations that named the old head. An engine that already attested the
     post-fix commit stays valid and does not re-run. This is what keeps a
     second reviewer from costing a full extra round every time anything
     changes.
   - **Rounds 1–2 are adversarial; round 3 and later are convergence rounds.**
     A reviewer holding no attestation on this PR runs adversarially on its
     first cold read whatever the round ordinal: the stance tracks how many
     times that reviewer has read the change, not how many rounds elapsed
     before it joined.
     Once every declared reviewer has read the change cold twice, the remaining
     findings are mostly about the review's own artifacts. A convergence round
     runs only the lanes that can find a reason not to deploy, changes the PR
     only for a realistically reachable blocking defect, defers everything else,
     creates an issue only for an urgent high-impact follow-up, and ends the
     loop as soon as it finds no blocker.

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

## Review Tier

Resolve the tier **before the first reviewer runs**, on every path. An
unresolved tier is not a neutral state — it is how the expensive path becomes
the default. **Lean is the default; Deep is the exception you justify.**

State the resolved tier and the trigger that selected it — or `no trigger` — in
the pass output, and post the ledger's `local-review-tier:v1` marker once per
PR. The marker is per-PR and shared across engines: later rounds resolve the
effective marker under the ledger's authenticated, forward-only transition rule
instead of reclassifying the unchanged range. A tier re-derived from scratch
each round, or re-derived against a different list in each engine, drifts back
to Deep.

### What sets the tier

Tier is set by what a missed defect reaches, not by how hard the change is to
review. Difficulty is the wrong input: every subtle diff feels like it deserves
more scrutiny, and that feeling is what pulls a whole repo onto the deep path.

Resolve the changed-file list once with
`git diff --name-only <base-sha>..<head-sha>`, then walk the triggers below.
**Any one selects Deep; no trigger means Lean.**

1. **Sensitive path** — authentication, authorization, cryptography, secret or
   credential handling, PHI/PII, tenant or customer isolation. However small
   the edit.
2. **Irreversible in production data or a published artifact** — migration,
   backfill, a published package's API or version: anything a revert cannot
   undo.
3. **Fans out past this repo** — the synced `.agents/**` surface, the sync
   engine, a published package, a contract other repositories consume. One
   defect lands in every consumer.
4. **Non-obvious behaviour in deployed runtime code** — concurrency, retries or
   idempotency, cache invalidation, money or clinical calculation, state
   machines, partial-failure and rollback paths: correctness that is not
   readable from the diff.
5. **Recurring-incident area** — the touched paths produced a post-merge defect,
   revert, or hotfix in roughly the last 90 days. Evidence is a specific defect,
   revert, or hotfix commit you can name; an active path with ordinary commit
   traffic is not evidence.
6. **Explicitly requested** — a human directly asked for a deep review, or the
   change is a first of its kind the author cannot self-assess. An internal
   `deep` argument passed between tier-aware skills only asserts the recorded
   tier; it is not a new request.

### What does not set the tier

Subtlety does not: a change can be hard to reason about and still be Lean. Nor
does diff size — a large mechanical refactor is Lean unless it also trips
trigger 4. Nor does topic adjacency: code _about_ security that does not itself
enforce a sensitive boundary is not trigger 1.

**The dominant rule: when the worst outcome of a missed defect is a red CI run,
a broken build, or a broken developer workflow, the change is Lean.** CI
scripts, lint rules, build tooling, developer utilities, fixtures, and test
harnesses land here even when they are subtle and even when a defect in them
fails open. That class of defect is caught by the next person the tool touches
and fixed by editing the tool.

Classify enforcement controls by the consequence of failure, not by their CI
location. A secret/privacy scanner, provenance gate, or release guard is Deep
when failing open can expose protected data, grant access, or compromise a
published artifact; that outcome trips trigger 1 or 2 rather than this rule.

**Precedence: walk triggers 1–6 first. The dominant rule only resolves a change
that matched no trigger.** It is dominant over the difficulty instinct, not over
the trigger list. Tooling that also fans out past this repo — the sync engine, a
shared CI action, anything under `.agents/` — is trigger 3 and therefore Deep,
because its blast radius is not confined to the developer who runs it.

### Round budget and stopping rule

A round is one complete pass per available engine at the same head.

- **Lean — cap 2.** Round 1 is adversarial. Round 2 runs only if round 1 made a
  material fix, and runs in convergence mode.
- **Deep — cap 4.** Rounds 1–2 adversarial, rounds 3–4 convergence.

**Stop as soon as a complete round produces no material fix.** That is the
stopping rule for both tiers, and it is a rule rather than a budget to spend: a
Lean change that lands after one clean round has had enough review.

A Lean change that reaches round 3 has either been mis-tiered — escalate it
deliberately, below — or is not converging, which is a signal about the change
rather than a licence for another round. Say which, and stop.

### Escalate and de-escalate on evidence

Both moves require a confirmed finding. A suspicion, an unverified severity
label, or "this feels risky" is not evidence and does not move a tier.

**Lean → Deep.** Escalate when a confirmed finding shows the change reaches a
trigger the classification missed — a real authorization or isolation bypass, a
real data-shape change, a real break in a contract another repository consumes —
or when the human directly requests Deep, which is trigger 6. Name the finding
or request and the trigger, post a replacement tier marker that preserves every
recorded trigger and adds the new one, and adopt the Deep budget. The round
already run counts as Deep round 1; do not restart the count.
**The first round after an escalation is adversarial whatever its ordinal.**

**Deep → Lean.** De-escalate when Deep round 1 completes and _every_ lane owning
a recorded trigger returned no confirmed finding. Finish at Lean: the lean lane
set, one further round at most. Running the full matrix again over a
substantively unchanged diff audits the review rather than the change. Record
the de-escalation and the lanes that came back clean.

Trigger 6 — an explicitly requested deep review — is never de-escalated. The
request is the evidence, and no clean lane overrides it. For the rest, a trigger
de-escalates only through the lane that owns it:

| Trigger                          | Owning lane                               |
| -------------------------------- | ----------------------------------------- |
| 1 sensitive path                 | security reviewer                         |
| 2 irreversible data or artifact  | code reviewer (migration/compat pass)     |
| 3 fans out past this repo        | code reviewer on the consumed contract    |
| 4 non-obvious deployed behaviour | silent failure hunter + code reviewer     |
| 5 recurring-incident area        | code reviewer scoped to the incident path |
| 6 explicitly requested           | not de-escalatable                        |

Tier selection narrows which lanes run and how many rounds are owed. It never
narrows what a lane may report, and it never relaxes the post-before-editing,
reply, or resolve contract.

`deepcritique` runs only on a resolved Deep tier and hands a Lean changeset back
to `critique`; typing the deep skill does not select the deep path. `reviewit`'s
iteration cap matches the tier's round cap numerically — two at Lean, four at
Deep.

## Hosted Reviewers

Hosted AI reviewers — the Gemini Flash and Copilot passes `reviewit` drives on
the PR itself — are a **different style of review**, not a fallback and not a
later phase. Run one whenever it is useful: before the relay, between rounds,
after convergence, or as the only review on a change that does not warrant a
local relay.

**The local relay is the default path here.** Coverage is expected to come from
declared roster engines reading the change cold, and that is what
`verify-coverage` measures. The hosted lane is an extension on top of that.

It stays fully supported because it is the primary path for a consumer whose
developers have no local agent engine — a repository with no local CLI and no
declared roster still gets real review from a hosted pass. That is the case the
lane exists for; it is not the case these defaults are tuned for.

Note that a hosted Gemini Flash pass and this engine's local lane are different
reviewers despite the shared model family: the hosted pass posts under its own
identity and reviews from GitHub, while the local lane runs as the authenticated
actor with the repository and ledger in hand.

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

A repository with no local engine has no roster, so `verify-coverage` does not
apply to it. There, convergence is the hosted lane's own contract: every hosted
finding disposed and resolved, and a final iteration that produced no fix. A
roster-less PR converges on that rule and must not claim relay coverage.

Invocation:

- **Lean** — `reviewit <pr-number>` for the bounded Gemini Flash and Copilot
  loop. It verifies and deduplicates their findings, fixes confirmed issues,
  pushes, replies, and loops within its cap.
- **Deep** — `reviewit <pr-number> deep`, with the larger cap and early-exit
  rules, and a final local `deepcritique` on the same PR number and ledger.

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
