# Review Workflow

This file is synced from `loomantix/activeloom` into consumer repos. Consumer-specific
edits will be overwritten on the next sync.

## Automatic chain execution

For an explicitly requested automatic review chain, use the deterministic
`.codex/skills/critique/scripts/review-chain-runner.py` controller. Resolve the
tier and its triggers first, then supply either an exact `--chain` or a repeating
`--cycle --until-converged`. Do not implement the outer loop in conversation.
Read [runner usage](../.codex/references/review-chain-runner.md) before starting;
it defines required validation commands, durable checkpoints, and recovery.
The Codex control surface must be installed even when another engine starts
the command. If it is absent, report the missing installation; do not substitute
a raw reviewer CLI or silently fall back to a conversational auto loop.

Each worker owns one pass only. The runner owns launch order, result verification,
attestation, and bounded progression. Existing handoff sessions and the separate
issue-implementation `agent-loop` keep their own contracts. An active legacy run
is not silently converted to a new plan or granted a fresh budget.

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
   - **Use the resolved tier's stance:** Lean round 1 is adversarial and round 2
     converges; Deep rounds 1–2 are adversarial and rounds 3–4 converge.
     The first pass after escalation remains adversarial regardless of ordinal.
     A reviewer holding no attestation on this PR runs adversarially on its
     first cold read whatever the round ordinal: the stance tracks how many
     times that reviewer has read the change, not how many rounds elapsed
     before it joined.
     After the tier's adversarial passes, the remaining
     findings are mostly about the review's own artifacts. A convergence round
     runs only the lanes that can find a reason not to deploy, changes the PR
     only for a realistically reachable blocking defect, defers everything else,
     creates an issue only for an urgent high-impact follow-up, and returns to
     the controller when clean so it can verify all required exact-head coverage.

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

## Finding severity

Rate findings by the behavior and people affected, using the same four levels
in every engine and lens:

- **blocking**: ships materially wrong behavior, loses or corrupts data, exposes
  a credible security/privacy exploit, breaks a public contract, or breaks rollout.
- **major**: a reachable defect with a concrete user or operator consequence
  that does not meet the blocking bar.
- **minor**: a limited defect or improvement with no material behavioral consequence.
- **nit**: style or preference, with no defect.

Severity describes a finding; pass classification describes the effect of its
fix. Apply the classification rules in the packaged ledger protocol separately.
Instructions and tests can affect review integrity, so their file type alone
does not determine severity or classification.

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
   credential handling, PHI/PII, tenant or customer isolation. Evaluate the
   bounded-repair exception below before selecting this trigger.
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
6. **Explicitly requested** — a human directly asked for a deep review.
   Novelty alone does not select this trigger; assess the actual risk under
   triggers 1–5. An internal
   `deep` argument passed between tier-aware skills only asserts the recorded
   tier; it is not a new request.

### Bounded repairs

A narrow repair may remain Lean even on a sensitive path when it removes an
unsafe operation or restores an established invariant using existing controls,
without changing authorization, cryptography, tenant isolation, persistence,
public contracts, or the control's accepted value shapes. Verify the affected
call site and regression coverage; include the security and test lenses where
applicable. For example, replacing a sensitive log value with a constant or a
boolean passed through an existing type-restricted sanitizer can qualify.

This is an exception to trigger 1, not an exemption from risk review. Evaluate
triggers 2–6 independently. For trigger 5, the original defect being repaired is
not by itself evidence of repeated failed fixes; a recurring regression or a
separate recent defect in the same behavior still counts. A new sanitizer, a
broadened allowlist value shape, or an unproven boundary change remains Deep.
Record why the exception applies. Small line count alone is not that evidence.
The exception fails closed: when it is unclear whether a change stays inside
it, it does not, and trigger 1 stands.

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

### The run controller

Rounds are numbered inside an authenticated `local-review-run:v1` marker, not
across the PR's whole history. The script that owns those markers is the run
controller, and this repository ships exactly one, shared by every engine
surface:

```
.codex/skills/critique/scripts/local-review-handoff.py
```

It lives under `.codex/` for historical reasons and is not Codex-only; invoke it
by that path from any surface. Its commands are `start-run`, `next-pass`,
`authorize-pass`, `finish-run`, `post-handoff`, and `show-handoff`. It implements neither `status`
nor `resume-run`. For legacy unsequenced runs, select one past this engine's highest completed round inside
the active run (1 when it has none), then confirm it with `authorize-pass`.
An aborted run is terminal in this controller. Preserve its evidence; a new
`start-run --restart` requires fresh explicit user authorization and starts
at round 1 with a full cap. This is a new review, not a budget-preserving resume.

For a requested alternating chain, record its ordered participants with
`start-run --sequence gemini,claude` (or the user's chosen order). The author
remains a required participant when named, separate from independent reviewer
coverage. Before each leg, call `next-pass --repo <owner/repo> --pr <number>
--head <exact-head-sha>` and authorize its returned engine and per-engine round.
Name each engine exactly once: the sequence must be two or three distinct
engines, and the controller repeats that cycle rather than taking a spelled-out
round trip. List the initiating engine first. A cycle must include its return
pass, but that pass need not be chronologically last when every participant
already holds non-material evidence at the exact current head. For literal
repeated steps, use the runner's finite `--chain` instead of `--sequence`.

Convergence requires strictly more than one full cycle: a return to the
initiating engine, and every participant's latest attestation on the current
head with no material outcome. A two-engine chain therefore does not converge
when the second engine comes back clean; it converges on the initiator's return
pass. Coverage alone cannot complete a sequenced chain. `finish-run --outcome
converged` also runs `verify-ledger` and `verify-coverage`.

In handoff mode, `post-handoff --to-engine` must name the engine `next-pass`
returned, not necessarily the originating engine, and the handoff's from-engine,
round, head, and outcome must describe the last completed pass. Preserve legacy
runs; adding a sequence requires an explicitly authorized restart, not an
implicit upgrade.

Preflight the sequence, tier, cap, mode, and tested launcher availability. A
missing launcher blocks that auto leg; it does not authorize substituting
same-engine subagents or shrinking the chain. Keep the existing auto-mode
capability rules below. At completion or handoff, report ordered
engine/round/head/outcome evidence, per-engine pass counts, validation, and the
actual terminal status. Count authenticated passes, not launches or subagent lanes.

When the target branch's effective GitHub rules require signed commits,
`start-run` verifies GitHub's signature status for every commit introduced by
the PR, not only its current head. `authorize-pass`, `post-handoff`, and a
converged `finish-run` repeat that check so a later unsigned fix cannot pass
through the relay. An unsigned or otherwise unverified commit stops before a
reviewer budget is spent. Prepare signed replacement commits in an isolated
worktree, prove their trees match, and obtain explicit approval for a
lease-protected force-push before rewriting published history. Repositories
whose effective target-branch rules accept unsigned commits are unchanged.

If that path does not exist in the checkout under review, say so and stop rather
than proceeding unauthorized — an absent controller is a missing gate, not a
licence to skip one.

### Escalate and de-escalate on evidence

Resolve the initial classification, including the bounded-repair exception,
before starting a run. A tentative Deep guess is not a reason to spend a Deep
pass: inspect the actual delta and record the justified tier first. For an
existing run, use the evidence-based transitions below and the installed
controller's supported recovery flow; preserve findings and consumed rounds.
This policy does not add a tier-amendment command or authorize a budget reset.
An explicit human request for Deep remains in force until the human revises it;
internal launcher arguments and "continue in auto" do not create that request.

After a justified classification, escalation and de-escalation follow the
evidence rules below. Suspicion or an unverified severity label does not move a tier.

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

Trigger 6 — an explicitly requested deep review — is never de-escalated by a reviewer alone. The
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

## Recover a blocked pass

Before interrupting a running reviewer for an apparent posting-order violation,
read the installed skill for its active phase. A local cleanup commit ahead of
the PR, or a latch not yet posted, can be expected before the enclosing pass's
single publication. Older skills also allowed cleanup without inline findings;
that instruction mismatch is not proof that the reviewer ignored its contract.
Reconcile at the phase's publication/result boundary. Current cleanup must have
fixed finding evidence; neither a commit title nor a latch can replace it.
If evidence is genuinely missing, preserve the work and use supported recovery;
never fabricate a prior finding or certify an incomplete pass.

A reviewer returns one pass; the auto controller owns the remaining authorized
work. A valid blocked result is not a clean attestation, but it is not by itself
a failed launcher or a reason to end the whole run.

Read the blocker and reconcile local, remote, and PR heads plus the complete
thread ledger. Then choose the next action from evidence:

- For a reachable code defect with an authorized fix, apply the smallest
  correction, validate, push, and dispose its existing finding. Continue only
  the missing exact-head reviewers within the remaining run budget.
- For incomplete bookkeeping, use the installed helper's supported recovery
  flow in [Recover interrupted reviews](references/local-review-ledger.md#recover-interrupted-reviews).
  Preserve the original result and pre-pass snapshot. Finalization cannot turn
  a blocked result into completed review evidence.
- For identity conflicts, unknown mutation outcomes, unsupported recovery, or
  a launcher failure, reconcile or report the precise unresolved boundary.
  Retry uncertain writes only with the same idempotent operation. A known
  preflight rejection that performed no write may be corrected and retried.
  Keep the tested launcher and ledger invariants intact.

Decide finding severity and intended disposition before posting. Reserve
`blocking` for a defect that must be fixed or disproved before this PR can
proceed. A planned follow-up is not a blocking disposition; evaluate its
reachability, scope, and severity first. If an existing blocker was mistaken,
record the evidence through the helper's supported disposition path. Dismiss
only an invalid claim; never edit a marker or use dismissal to hide a real
deferred blocker. An unavailable severity-amendment operation is not needed
when the authorized code fix can resolve the finding instead.

Ask for a decision only when the next action needs authority, information, or
risk acceptance the user has not supplied. Keep the run active while recovery
can progress; close it when it actually converges, exhausts its cap, or reaches
an unresolved terminal boundary. Recovery does not reset the round budget.

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

## Pass Telemetry

At the pass boundary, create and save this pass's idempotency key:

```bash
node .agents/skills/critique/scripts/telemetry-pass-key.js \
  <owner/repo> <pr> <controller-run-id-or-standalone> <authenticated-actor> \
  <engine> <review-or-refactor-or-hosted> <round> <reviewed-head>
```

Use the controller's authenticated run ID when present. `standalone` creates
a fresh attempt identity; invoke it once and retain its returned key in the
pass's private working files. Pass `--idempotency-key <returned-key>` to every
emission, reusing it on a retry. Different actors and restarted runs must not
share keys. Never regenerate a standalone key to retry publication. If key
creation fails, report telemetry failure rather than falling back to a key
that may identify a different pass. This works with existing ledger bundles.

Every review and cleanup pass attempts one `local-review-telemetry:v1` PR
comment after finalizing its review result, including blocked and skipped
passes. Telemetry is best-effort: report failure and continue without changing
the review outcome. Never read prior telemetry into reviewer context; filter
all comments carrying the `local-review-telemetry:` prefix.

Before reading or classifying the diff, resolve the repository, PR, literal
base/head SHAs, engine, pass type, round, and stance. Resolve the engine from
the runtime actually running the pass — `gemini` or `antigravity` — the same
way the skills do; never hardcode it. A misattributed record is counted against
the wrong engine and skews both series, which is worse than the absent
measurement this distribution already reports. Then invoke:

```bash
node .agents/skills/critique/scripts/usage-snapshot.js snapshot
```

The helper reads repository gates from its synced `review-telemetry.json` and
non-empty environment overrides. `LOOM_REVIEW_TELEMETRY` governs publication;
`LOOM_REVIEW_TELEMETRY_EXTRACT` governs measurement and inherits publication
when unset. Missing gates default on. Set `telemetry.emit: off` in the
consumer's sync configuration — not in the rendered `review-telemetry.json`,
whose only keys are the two variables named above — or set
`LOOM_REVIEW_TELEMETRY=off` to opt out; extraction also turns off unless
explicitly enabled. An invalid value is not an opt-out: it disables the gate
and reports an error, so a typo reads as a misconfiguration rather than as a
deliberate `off`.
Never infer permission from anything except the helper's `emit` field.

This distribution has no pass-scoped usage adapter. The helper truthfully reports
`tokenSource: unavailable` and no token buckets; do not guess counts from
context size, another engine's log, or a whole-session estimate. At the end of
each pass run the helper with `delta` and retain this unavailable provenance.
Record elapsed wall time only when a start/end clock was actually captured.

When `emit` is true, invoke the ledger's `emit-telemetry` command with:

- `--repo`, `--pr`, `--engine` (the engine resolved at the pass boundary),
  `--base`, and `--head` from the pass boundary;
- `--pass-type review` or `refactor`, `--round`, and `--stance`;
- `--review-tier lean` or `deep` when resolved;
- `--trigger autonomous` for a runner invocation, otherwise `interactive`;
- `--status clean`, `changed`, `blocked`, or `skipped` as actually observed;
- `--token-source unavailable`, omitting token/model/version arguments;
- `--findings-file` pointing to an explicit numeric findings object below.

Omit `--changeset-file` to use the package's changeset classifier. `skipped`
is reserved for a changeset with zero review-significant files; a cleanup
that stops at its previously spent latch reports `clean`. Pass the original
boundary even when fixes moved the head; do not silently attribute the pass
to a different diff. Omit prompt hashes until this harness has a hasher.

The findings file contains `posted`, `chainInducedRegressions`, and
`bySeverityAndOutcome`. The latter has one object for each of `blocking`,
`major`, `minor`, and `nit`, each containing `validFixed`, `validDeferred`, and
`invalidDismissed` integer counts. Derive these from findings and dispositions
this pass actually posted, including dispositions of older findings in
`posted`. Count a chain-induced regression only when its new fingerprint's
anchor traces to an earlier fix on this PR. Explicit zeros mean measured zero;
never omit the file and assume the package knows what happened. Carry no
finding titles, paths, transcript text, or other prose into telemetry.

If the pass fails before its mandatory identity is resolved, report
`telemetry not emitted: boundary unresolved`. Otherwise blocked passes follow
the same emission path. Report the returned `emitted` result and reason;
never retry a telemetry failure into a review or treat missing usage as zero.
