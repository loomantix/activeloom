# Review Workflow

This file is synced from `claude-platform` into consumer repos. Consumer edits
will be overwritten on the next sync.

## PR-First Rule

Open a draft pull request before any local cleanup or adversarial review. Every
local pass, whichever engine runs it, uses GitHub review threads as durable
shared context: post each verified finding inline before editing, push the
correction, reply with the fix and validation, then resolve the thread. Every
pass reads resolved as well as unresolved threads before reviewing the current
head.

Load [the local review ledger](references/local-review-ledger.md) before running
`refactorpass`, `critique`, `deepcritique`, `codex-review`, or local review hooks.
That file is the engine-neutral protocol published by the
[`@loomantix/review-ledger`](https://www.npmjs.com/package/@loomantix/review-ledger)
project and vendored verbatim into every engine repository, so all engines read
the same contract. The helper bundle beside it is vendored from that package's
published tarball and pinned by `review-ledger.version` and
`review-ledger.integrity`; CI byte-compares the bundle, not this document, so a
protocol edit must land upstream rather than here. Where the protocol writes
`<ledger-helper>`, this engine's path is:

```text
.claude/skills/critique/scripts/review-ledger.js
```

## Review Tier

Resolve the tier **before the first reviewer runs**, on every path. An
unresolved tier is not a neutral state — it is how the expensive path becomes
the default. **Lean is the default; Deep is the exception you justify.**

State the resolved tier and the trigger that selected it — or `no trigger` — in
the pass output, and post the ledger's `local-review-tier:v1` marker once per
PR. Later rounds resolve the effective marker under the ledger's authenticated,
forward-only transition rule instead of reclassifying the unchanged range; a
tier re-derived from scratch each round drifts back to Deep.

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
3. **Fans out past this repo** — the synced `.claude/**` surface, the sync
   engine, a published package, a contract other repositories consume. One
   defect lands in every consumer.
4. **Non-obvious behaviour in deployed runtime code** — concurrency, retries or
   idempotency, cache invalidation, money or clinical calculation, state
   machines, partial-failure and rollback paths: correctness that is not
   readable from the diff.
5. **Recurring-incident area** — the touched paths produced a post-merge defect,
   revert, or hotfix in roughly the last 90 days
   (`git log --oneline --since=90.days -- <paths>`).
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
shared CI action, anything under `.claude/` — is trigger 3 and therefore Deep,
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
**The first round after an escalation is adversarial whatever its ordinal.** An
escalated change would otherwise inherit a convergence stance and receive the
Deep budget without one adversarial Deep pass — and escalation fires precisely
when a confirmed finding showed the change reaches further than classified.

**Deep → Lean.** De-escalate when Deep round 1 completes and _every_ lens owning
a recorded trigger returned no confirmed finding. Finish at Lean: the lean lens
set, one further round at most. Running the full matrix again over a
substantively unchanged diff audits the review rather than the change. Record
the de-escalation and the lenses that came back clean.

Trigger 6 — an explicitly requested deep review — is never de-escalated. The
request is the evidence, and no clean lens overrides it. For the rest, a trigger
de-escalates only through the lens that owns it:

| Trigger                          | Owning lens                                 |
| -------------------------------- | ------------------------------------------- |
| 1 sensitive path                 | `security-review`                           |
| 2 irreversible data or artifact  | `code-reviewer` (migration/compat pass)     |
| 3 fans out past this repo        | `code-reviewer` on the consumed contract    |
| 4 non-obvious deployed behaviour | `silent-failure-hunter` + `code-reviewer`   |
| 5 recurring-incident area        | `code-reviewer` scoped to the incident path |
| 6 explicitly requested           | not de-escalatable                          |

Tier selection narrows which lenses run and how many rounds are owed. It never
narrows what a lens may report, and it never relaxes the post-before-editing,
reply, or resolve contract.

## Review Relay

The relay is defined over **roles**, not engine names:

- **author** — the engine that wrote the change. Exactly one.
- **reviewer** — an engine that reads the change cold. Zero, one, or more.

Claude is the author role when Claude wrote the change and a reviewer role
otherwise. The rules below never name an engine, so adding a fourth changes
nothing here.

**One non-author reviewer is the recommended floor**, and covers the great
majority of changes. A second reviewer earns its cost mainly where a defect is
expensive and hard to see — the Deep triggers above are the same signals. Solo
review is permitted but must be declared with a reason; see step 2.

1. Make the change, validate it, create a clean commit, and open a draft PR.
2. Declare the roster with the ledger helper's `post-roster`, naming the author
   engine and the reviewer engines for this PR. Participation is declared, never
   inferred: an engine that has not attested is otherwise indistinguishable from
   one that was never going to run, so nothing downstream can tell an incomplete
   round from a finished one. A solo relay is `--reviewers none` with the reason
   in the content file, which puts the choice on the PR rather than in a
   session's memory.
3. Pin the exact base SHA for the round, resolve the tier, and give both to
   every reviewer. Do not start a reviewer with the tier unresolved.
4. Run each declared reviewer in a fresh session against the current head, under
   the ledger's comment/fix/reply/resolve contract. Claude's lane is
   `critique <pr-number>` at Lean and `deepcritique <pr-number>` at Deep; other
   engines use their own equivalents. Reviewer order within a round is a
   scheduling choice, not a protocol rule — what matters is which commit each
   one read.
5. Classify fixes by effect, not path or finding severity. `material` includes
   substantive correctness, security/privacy, data-safety, compatibility,
   deployment/sync, or review-integrity changes, including tests or workflows
   needed to prevent a false green. `minor` is low-risk non-behavioral cleanup
   or polish.

   Severity and classification are separate axes and neither implies the other.
   A fixed `major` whose fix edited only comments, only docs, or only tests is
   `minor` — no executing line moved, so the round can complete through that
   transition rather than owing every declared reviewer a fresh cold read.
   Findings are rated on the single ladder in
   [`references/local-review-ledger.md`](references/local-review-ledger.md);
   that section is the only definition of `blocking`, `major`, `minor`, and
   `nit`, and every lens and engine uses it.

   **The chain gets cheaper as it repeats.** Three rules make that happen, and
   all are enforced from the ledger rather than from session memory:
   - **The refactor pass runs once per engine per PR.** A second `/simplify` over
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
   - **At Deep, rounds 1–2 are adversarial and round 3 and later are
     convergence rounds; at Lean the cap is 2 and round 2 is the convergence
     round.** The stance follows the tier's schedule above, not the ordinal
     alone. A reviewer holding no attestation on this PR runs adversarially on
     its first cold read whatever the round ordinal: the stance tracks how many
     times that reviewer has read the change, not how many rounds elapsed
     before it joined.
     Once every declared reviewer has read the change cold twice, the remaining
     findings are mostly about the review's own artifacts. A convergence round
     runs only the lenses that can find a reason not to deploy, changes the PR
     only for a realistically reachable blocking defect, defers everything else,
     creates an issue only for an urgent high-impact follow-up, and ends the
     loop as soon as it finds no blocker. Lenses still report everything they
     find — the narrowing is a disposition rule applied by the orchestrator,
     never an instruction to a review agent to withhold by severity or
     confidence.

6. Converge when `verify-coverage` passes at the exact current head — a roster
   is declared and every declared reviewer holds an attestation naming that
   head — the round that produced those attestations had no material
   transition, and every local-review thread has a disposition reply and is
   resolved. A minor A-to-B transition can complete the round without pretending
   an earlier reviewer read B; its exact-head attestation remains historical
   evidence, and `verify-coverage` is the authority on what has been read at the
   head that will actually merge.
7. The wrapper, not review hooks, posts canonical pass/completion attestations
   after validating structured results and the GitHub ledger.
8. Stop at the tier's round cap — two at Lean, four at Deep — or earlier under
   the stopping rule above. Leave the PR draft and report non-convergence
   instead of continuing an unbounded cycle.

The author engine's own adversarial pass never counts toward coverage. It
re-reads the change while still holding the rationale that produced it, which is
the opposite of the cold read the relay exists to obtain. `coverage` reports it
as `authorAttested` so the fact stays visible, but the tier counts distinct
non-author engines only.

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

A repository with no local engine has no roster, so `verify-coverage` does not
apply to it. There, convergence is the hosted lane's own contract: every hosted
finding disposed and resolved, and a final iteration that produced no fix. A
roster-less PR converges on that rule and must not claim relay coverage.

A hosted pass **invalidates nothing on its own.** Only a commit invalidates, and
only by the head rule in step 5, which treats a hosted-review fix exactly like
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

Invocation:

- **Lean** — `reviewit <pr-number>` for the bounded Gemini Flash and Copilot
  loop, after `refactorpass <pr-number>` and `critique <pr-number>`.
- **Deep** — `reviewit <pr-number> deep`; its final local `deepcritique`
  receives the same PR number and ledger, and skips the refactor pass when this
  engine's cleanup latch is already spent on the PR.

`reviewit`'s iteration cap is the tier's round cap: two at Lean, four at Deep.

## Review Principles

- Treat generated findings as hypotheses; verify against source before posting.
- **A pass that cannot name its tier and the trigger that selected it has not
  started correctly.** Tier is resolved before the first reviewer, not inferred
  from which skill someone happened to type.
- **No Claude finder lane pre-filters by severity or confidence.** A `critique`
  sub-agent or inline `Agent(...)` prompt reports everything with severity and
  confidence attached; filtering happens one level up, where every lens is
  visible and each claim can be checked against the diff. The bounded
  `codex-review` cross-check is the documented vendor-specific exception: its
  terse material-finding filter remains per [`MODEL_NOTES.md`](MODEL_NOTES.md)
  §1 and must not be copied into Claude finder prompts.
- The agent matrix is a ceiling, not a floor. Run only the lenses whose signals
  appear in the diff, and never add an agent to re-check another agent's work.
  See [`MODEL_NOTES.md`](MODEL_NOTES.md) §2–§3.
- Fix a confirmed finding only when likely user harm or a credible security
  exploit justifies the fix's churn and regression risk.
- **A round that only finds non-material test, fixture, comment, or docs polish
  is the signal to ship, not to keep going.** It means the product converged and
  the review has turned to auditing its own artifacts. A test or workflow fix
  needed to prevent a false green remains material and moves the head under
  step 5, invalidating the attestations that named the old commit. Defer
  non-material polish without growing the backlog.
- Create a tracking issue only for a concrete, high-impact follow-up that should
  be scheduled within roughly two weeks.
- A fix without a preceding inline finding, a finding without a reply, or a
  resolved thread without a visible disposition is a failed pass.
- Never copy sensitive source, credentials, private data, or model logs into PR
  metadata.
- Stop at the configured cap and preserve the draft PR on non-convergence.

## Cross-references

- [`MODEL_NOTES.md`](MODEL_NOTES.md) — prompt-authoring deltas for the current
  default model; read before editing any skill or agent.
- [`references/local-review-ledger.md`](references/local-review-ledger.md) — the
  PR-thread ledger contract, including the shared docs/config-only changeset
  classification every skill skips on.
- [`skills/refactorpass/SKILL.md`](skills/refactorpass/SKILL.md) ·
  [`skills/critique/SKILL.md`](skills/critique/SKILL.md) ·
  [`skills/deepcritique/SKILL.md`](skills/deepcritique/SKILL.md) ·
  [`skills/codex-review/SKILL.md`](skills/codex-review/SKILL.md) — the local
  relay lanes.
- [`skills/reviewit/SKILL.md`](skills/reviewit/SKILL.md) — the hosted-reviewer
  lane, including the `tier=flash` cost rule.
- [`skills/review-accessibility/SKILL.md`](skills/review-accessibility/SKILL.md)
  — optional, human-triggered a11y pass; opens its own PR and is not part of
  the relay or the hosted lane.
- `/pushit` and `/review-cycle` are retired; their stubs are gone, so old
  invocations resolve to nothing.
