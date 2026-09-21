# 0012 — Assurance ledger, marker versions, and compatibility

- Status: accepted
- Date: 2026-09-21

[0009](0009-repository-assurance-facts.md) fixes the fact vocabulary,
[0010](0010-assurance-control-matrix.md) fixes what the facts do, and
[0011](0011-assurance-setup-interaction.md) fixes how a repository declares
them. None of the three says what is written down when a run actually happens.
This record does: which marker families change, which are proven not to, what a
run finishes with, and the order in which producers may start emitting any of
it.

It also resolves two defects that only appear when 0009, 0010, and 0011 are read
against each other and against the code, because this is the record that has to
encode them. The amendments to 0009 and 0010 ship alongside it.

Everything below was read out of the current tree. Where the source contradicts
an accepted position, the source is stated and the position is corrected here
rather than restated.

## What was inspected

The evidence surface is smaller than it looks, and it has exactly two writers.

**The ledger package** — `packages/review-ledger`, `@loomantix/review-ledger`
1.5.0, whose `src/index.ts` is the whole public surface:

| File               | What it owns                                                                                         |
| ------------------ | ---------------------------------------------------------------------------------------------------- |
| `src/constants.ts` | `PROTOCOL_VERSION = 3`, `TELEMETRY_VERSION = 1`, and every marker regex                              |
| `src/protocol.ts`  | `buildFindingBody`, `buildDispositionBody`, `matchProtocol`, `matchMarkerLine`                       |
| `src/ledger.ts`    | `threadProtocolRecords`, `postPrComment`, `attest`, `finalize`, `verifyLedger`                       |
| `src/roster.ts`    | `matchRoster`, `attestationsAtHead`, `coverageTier`, `coverage`, `verifyCoverage`                    |
| `src/runs.ts`      | `reviewRuns`, `reviewRunForComment` — the run-marker reader                                          |
| `src/telemetry.ts` | `validateTelemetryRecord`, `matchTelemetry`, `buildTelemetryBody`, `prCommentSink`, `emitTelemetry`  |
| `src/changeset.ts` | `classifyPath`, `classifyFiles` — the human-glance classifier                                        |
| `src/cli.ts`       | 23 subcommands, including `verify-ledger`, `verify-coverage`, `classify-changeset`, `emit-telemetry` |
| `src/types.ts`     | `CoverageTier`, `TelemetryRecord`, `TelemetryReviewTier`                                             |

**The run controller** — `.codex/skills/critique/scripts/local-review-handoff.py`,
one script shared by every engine surface, which owns `HANDOFF_V1_RE`,
`RUN_V1_RE`, `RUN_END_V1_RE`, `TIER_CAPS`, `_run_records`, `_run_end`,
`_sequence_decision`, `_finish_run`, and `_verify_convergence_ledger`.

**The chain runner** — `.codex/skills/critique/scripts/review-chain-runner.py`,
which drives `start-run`, `next-pass`, `authorize-pass`, and `finish-run`.

**The protocol document** — `packages/review-ledger/protocol/local-review-ledger.md`
is the canonical copy. `scripts/render-prompts.py` renders it to
`.claude/references/`, `.codex/references/`, and `.agents/references/`, listed in
`prompts/rendered-files.txt`. All four are byte-identical today; any wording
change here is four files or it is a parity failure.

**Compatibility tests** — `src/__tests__/constants.test.ts`,
`protocol-rules.test.ts`, `runs.test.ts`, `roster.test.ts`,
`verification.test.ts`, `telemetry.test.ts`, `legacy-threads.test.ts`, and on
the Python side `tests/test_review_sequence.py`,
`tests/test_review_restart_rounds.py`, `tests/test_review_scope_checkpoint.py`,
`tests/test_human_glance.py`, `tests/test_telemetry_gates.py`.

## Defect 1 — `solo` and `cross` are two vocabularies, not one

0010 uses `solo` and `cross` in two different ordered sets:
`review.minimum_coverage` over `solo < cross < full`, and the human acceptance
plans over `human-glance < solo < cross < lean`. Different tops is the visible
half of the problem. The source shows a worse half.

`CoverageTier` (`src/types.ts`) is `'solo' | 'cross' | 'full'`, and
`coverageTier` (`src/roster.ts`) computes it from one number: how many
**distinct non-author engines** attested the exact head. Zero is `solo`, one is
`cross`, two or more is `full`. It is a measurement of posted attestations,
recomputable by anyone reading the pull request, and `verifyCoverage` never
compares it against a required minimum — it fails only on roster integrity and
missing declared reviewers.

The acceptance plans are a different kind of thing: a plan is an instruction
about what to run, a coverage tier is a measurement of what ran. They do not
even agree extensionally. An accepted `solo` plan — one engine, one pass — yields
coverage `solo` if that engine is the author and coverage `cross` if it is not.
An accepted `cross` plan yields `cross` or `full` depending on how many engines
were available. One name, two sets, and no function between them.

### Which side is renamed

The coverage names are load-bearing in code: `CoverageTier` in `src/types.ts`,
`coverageTier` and `coverage` in `src/roster.ts`, the `tier` field of the
`verify-coverage` JSON that `_verify_convergence_ledger` runs on every converged
`finish-run`, and `roster.test.ts`. They are also the older vocabulary. They stay.

**The acceptance-plan vocabulary is renamed** to
`human-glance < single-engine < all-engines < lean`. Each name now says what
0010's own table says the plan means, and no name is shared with a measurement.
`human-glance` keeps its spelling because it is already load-bearing elsewhere —
the automatic gate, `tests/test_human_glance.py`, and
`.github/workflows/review-glance-label.yml` — and because the gate and the
accepted plan genuinely name the same thing: no agent review.

### Whether an accepted plan overrides the minimum

**It does not. An accepted plan is not a coverage claim, the minimum is left
unmet and recorded as unmet, and the run finishes `human-accepted`.**

M5 permits lowering `review.minimum_coverage`, since it sits in the `review`
domain. Permitting is not requiring, and the source says why it must not be
done. The coverage tier is computed from attestations at the exact head.
Anything that raised it without an attestation behind it would make the one
number an auditor can independently recompute from the pull request stop being
recomputable, and it would do so precisely in the runs where independent
recomputation matters most.

So acceptance does not change the resolved control vector at all. The vector,
`review.minimum_coverage` included, is recorded unchanged in the policy marker.
Acceptance changes which plan runs. `review.minimum_coverage` becomes a
**reported expectation** compared against the measured `coverageTier` at finish
— exactly the pattern 0010 already uses for the `governance` controls, where the
resolver reports an expected value, observes the actual one, and records whether
they agreed.

That gives `human-accepted` a definition instead of a label: a run finishes
`human-accepted` when it completed under a valid acceptance at the exact head
and at least one `review`-domain expectation is knowingly unmet.

One corollary, stated because it is the tempting exception: a run under a valid
acceptance whose executed plan happens to **meet** the minimum still finishes
`human-accepted`, never `covered`. Otherwise the terminal outcome would depend
on how many engines happened to be available that day, and an acceptance would
sometimes vanish from the terminal record.

## Defect 2 — the explanation format has no slot for locks or maximum mode

0009's resolver explanation format has a header, `facts`, `frameworks`,
`controls`, and `warnings`. 0010 requires locks (lock-eligibility is stated per
row, and M7 asserts lock persistence across policy versions) and requires the
acceptance snapshot to record "whether maximum mode was active". 0011 displays
locks in the control vector and says no fact the user chooses will unlock one.
The format carries neither.

It is not only a missing field. 0009's requirement 2 says every resolved control
appears "with the facts that selected it, and only those" — which is
unsatisfiable for a locked control and for one raised by `LOOM_ASSURANCE_MODE=maximum`,
because no fact selected either. The format is amended in 0009 (see the
amendment below) to add a mode token to the header and an **origin** column to
each control line, with origin in `resolved`, `locked`, or `maximum`.

The marker consequence lands here: the policy marker's control-vector digest is
taken over the rendered control section, so it binds the origin column and the
header mode token by construction. Two resolutions that differ only in whether a
control was locked or raised by maximum mode therefore digest differently, which
is the property that makes the digest worth binding at all.

## The two new marker families

Both are **pull-request issue comments posted by the run controller**, the way
`local-review-run:v1` already is through `_post_issue_comment`. Neither is ever
posted into a review thread, and neither is posted through the ledger's
`post-pr-comment`, so `PR_V1_MARKERS` in `src/constants.ts` does not change.

Both follow the layout `matchMarkerLine` enforces for every authenticated
record: the marker is the first complete line, non-empty content follows, and
the digest covers the content. `RUN_END_V1_RE` is the protocol's only
authenticated record that does not — it is marker-only — and that exception is
closed below rather than extended.

### `local-review-policy:v1`

One record per resolution, superseded rather than edited.

```
<!-- local-review-policy:v1 schema=<int> policy=<int> mode=<normal|maximum>
     head=<40-hex> control-vector-sha256=<64-hex> org-assertion-sha256=<64-hex|none>
     default-plan=<lean|deep> supersedes=<none|comment-id>
     content-sha256=<64-hex> -->
```

| Field                   | Bound to                                                                                                              |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `schema`                | `assurance.schema_version` from the declaration (0009)                                                                |
| `policy`                | resolver policy version, separate from schema per 0009 and 0010's explicit non-invariant                              |
| `mode`                  | `maximum` when `LOOM_ASSURANCE_MODE=maximum` was active, else `normal` (M6)                                           |
| `head`                  | the exact commit the resolution describes                                                                             |
| `control-vector-sha256` | digest of the rendered `controls` section of the explanation, origin column included                                  |
| `org-assertion-sha256`  | digest of the organization assertion consumed, or `none` for a registry-less adopter                                  |
| `default-plan`          | `max(review.plan_floor, trigger_selected_plan)` — the plan before any acceptance                                      |
| `supersedes`            | previous policy comment ID, or `none`, giving the same append-only chain `reviewRuns` and `resolveRoster` already use |
| `content-sha256`        | the record's own content digest, per `matchMarkerLine`                                                                |

The content is the resolver explanation verbatim. It is prose-free by 0009's
requirement 6, so nothing here copies rationale.

### `local-review-acceptance:v1`

One record per acceptance. Superseding, not editing, is what makes 0010's
invalidation rule 5 — an amended acceptance supersedes and the earlier one
becomes historical — representable.

```
<!-- local-review-acceptance:v1 head=<40-hex> actor=<login>
     normal-plan=<lean|deep> accepted-plan=<human-glance|single-engine|all-engines|lean>
     policy=<policy-comment-id> authority-sha256=<64-hex>
     command-sha256=<64-hex> rationale-sha256=<64-hex>
     unresolved-sha256=<64-hex|none> supersedes=<none|comment-id>
     content-sha256=<64-hex> -->
```

| Field                           | Bound to                                                                                                                                                                                                                   |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `head`                          | the one exact head the acceptance binds (invalidation rule 1)                                                                                                                                                              |
| `actor`                         | the non-bot login that posted the command                                                                                                                                                                                  |
| `normal-plan` / `accepted-plan` | 0010's recorded snapshot, in the renamed plan vocabulary                                                                                                                                                                   |
| `policy`                        | comment ID of the `local-review-policy:v1` record in force, which is how the triggers, facts, schema and policy versions, and maximum-mode flag reach the record without being copied into it (invalidation rules 2 and 3) |
| `authority-sha256`              | digest of the merge-authority snapshot at acceptance time, rechecked at merge (rule 6)                                                                                                                                     |
| `command-sha256`                | digest of the command comment, so an edit or deletion breaks the match (rule 4)                                                                                                                                            |
| `rationale-sha256`              | digest of the free-text rationale — never the prose, per `data.rationale_copying`                                                                                                                                          |
| `unresolved-sha256`             | digest of the outstanding findings by identity and severity, or `none` when there were none                                                                                                                                |
| `supersedes`                    | previous acceptance comment ID, or `none` (rule 5)                                                                                                                                                                         |

The controller checks that a rationale exists and is non-empty. It does not
grade it, per 0010.

**The acceptance record is the terminal record for a `human-glance`
acceptance**, and this is forced by the source rather than chosen. A
`human-glance` acceptance runs no pass, so `start-run` is never called and no
`local-review-run:v1` exists; `_sequence_decision` fails closed on a run with no
engine sequence, and a run-end marker naming a run that was never declared is
not a record any reader accepts. There is therefore nothing for a terminal
marker to attach to, and the acceptance record must stand alone.

The same source constraint settles how `data.measurement_emission` is satisfied
in that case. `emit-telemetry` requires `--pass-type`, `--round`, `--stance`,
and `--status` (`src/cli.ts`), none of which exist when no pass ran, and the
protocol document already states that a human-glance range stops before the
telemetry snapshot. The acceptance marker is the measurement surface for
`human-glance`; telemetry carries the acceptance fields for every plan that
actually runs a pass.

## The finish marker

`RUN_END_V1_RE` in the controller closes over `outcome=(converged|exhausted|aborted)`.
It cannot express `covered` or `human-accepted`, so a version is required. Two
source facts shape what the new version is.

**The runner already computes `covered` and throws it away.**
`_sequence_decision` returns `plan-complete` when a finite `--chain` plan
completes without the return leg convergence requires — `"converged" if clean
and len(latest) > 1 else "plan-complete"`. `review-chain-runner.py` then
collapses it twice, at both `finish-run` call sites, with
`"converged" if status == "converged" else "exhausted"`. A completed finite
roster and a spent budget are recorded today as the same outcome. `covered` is
not a new concept; it is an existing one that has no way to be written down.

**The terminal marker carries no content digest.** `RUN_END_V1_RE` is matched
with `fullmatch` against a marker-only body, while every record that carries a
decision another pass has to trust — `local-review-run:v1`, both roster
versions, both attestation markers, findings, dispositions, and the handoff
marker — binds one. The two exceptions besides this marker are the
`local-review-tier:v1` and `local-review-refactor:v1` latches, which record a
resolved classification rather than a terminal judgement.
`human-accepted` is the most authority-bearing outcome in the vocabulary, and
putting it on an undigested grammar would leave the record a human's merge
decision rests on weaker than every record an engine writes.

```
<!-- local-review-run-end:v2 id=<64-hex>
     outcome=<converged|covered|human-accepted|exhausted|aborted>
     head=<40-hex> policy=<none|comment-id> acceptance=<none|comment-id>
     coverage=<solo|cross|full> after=<none|comment-id>
     content-sha256=<64-hex> -->
```

`coverage` records the measured `coverageTier` at the terminal head, which is
what makes the shortfall under `human-accepted` legible without re-reading the
pull request. `acceptance` is `none` for every outcome except `human-accepted`,
and required for it. `after` preserves v1's recovery link. The content is the
terminal summary, digested like every other authenticated record.

The three existing outcomes keep their exact meanings.

## Telemetry

`TELEMETRY_VERSION` is 1. The new fields require **version 2**, and the reason
is a concrete misread rather than tidiness.

`validateTelemetryRecord` checks the known fields and rejects a record whose
`version` is not `TELEMETRY_VERSION`. It does not reject unknown keys.
`knownTelemetryRecord` then projects a validated record onto an explicit field
list, and `buildTelemetryBody` renders that projection. Adding acceptance fields
to v1 in place would therefore be **silently truncated** by any older writer that
round-trips a record: it would validate the input, drop the fields it does not
know, and re-render a record that looks complete and is not. That is exactly the
shape the rollout has to exclude — a consumer presenting evidence as whole while
having discarded part of it.

The version bump removes the possibility by construction, because the
discriminator is inside the marker. `matchTelemetry` looks for the literal
`TELEMETRY_V1_MARKER`; against a v2 body it raises "local-review telemetry
record is of an unsupported version" rather than parsing it. `isTelemetryComment`
matches on `TELEMETRY_MARKER_PREFIX` and so still excludes a v2 record from
ledger comment scans, which is the behaviour `excludeTelemetryComments` needs.
And `emitTelemetry` reports every failure on stdout and exits 0, so no telemetry
version skew can fail a review that found real defects.

v2 adds four fields to `TelemetryRecord`:

| Field               | Type                    | Carries                                                          |
| ------------------- | ----------------------- | ---------------------------------------------------------------- |
| `policyVersion`     | `number \| null`        | resolver policy version; `null` for an undeclared repository     |
| `assuranceMode`     | `'normal' \| 'maximum'` | M6's only environment surface                                    |
| `acceptedPlan`      | plan name or `null`     | the accepted plan, `null` when none                              |
| `coverageShortfall` | `boolean`               | whether `review.minimum_coverage` was unmet at the terminal head |

`reviewTier` stays `'lean' | 'deep'` and keeps meaning the resolved plan.
Nothing carries rationale, control names with values, actor identity, or any
free-form string: `data.rationale_copying` is a universal baseline, and a
digest already sits in the acceptance marker for anyone entitled to the pull
request.

## State transitions

Five runs, written as the records that exist when each finishes. `P` is the
policy marker, `A` the acceptance marker, `R` the run marker, `E` the terminal.

**Clean** — resolved plan runs, every participant attests the exact head, the
initiator returns clean.
`P → R → passes → E(converged, acceptance=none, coverage=<measured>)`.
`finish-run --outcome converged` runs `_verify_convergence_ledger`, which is
`verify-ledger` and `verify-coverage` at the exact head. Unchanged from today.

**Fixed** — a pass finds something material, it is fixed, the head moves.
`P → R → pass(material) → fix → P'(new head) → targeted exact-head verification
→ … → E(converged)`.
Two things hold here. The policy marker is re-emitted at the new head, because
`head` binds it. And the adversarial-pass budget does not suppress the narrowed
verification of the fix: 0010 states it, and nothing in `_sequence_decision`
counts a targeted re-check as a discovery pass, since only `PASS_V3_RE` and
`COMPLETE_V3_RE` attestations become events.

**Covered** — a finite roster completes at the exact head with no return leg.
`P → R(plan:v1 mode=chain) → passes → E(covered)`.
This is `_sequence_decision`'s existing `plan-complete`, written down instead of
collapsed. `finish-run --outcome covered` requires the same exact-head
verification `converged` requires; what it does not assert is relay convergence,
which is the distinction the protocol document already draws for `status
covered`.

**Exhausted** — the cap is reached with material findings outstanding.
`P → R → passes → E(exhausted, coverage=<measured>)`.
Unchanged. `_finish_run` already checks that a sequenced run's decision is
`exhausted` or `plan-complete` before accepting the outcome; that check narrows
to `exhausted` once `covered` exists.

**Aborted** — the run stops without a terminal judgement.
`P → R → passes → E(aborted, after=none)`, recoverable by `resume-run`, whose
subsequent terminal names `after=<recovery comment ID>`. Unchanged, and
deliberately so: abort is the one terminal that must never require a policy or
acceptance record to be writable.

**Human-accepted**, in its two shapes:

- _Curtailed run_ — `P → A → R → reduced passes → E(human-accepted, policy=<P>,
acceptance=<A>, coverage=<measured>)`. Validity of `A` is rechecked at the
  terminal head and again at merge. If a new material defect appears during
  exact-head verification, `A` is invalidated by rule 3, the pull request
  returns to normal resolution, and the run either continues under the resolved
  plan or ends `exhausted`. It never ends `human-accepted` on a superseded
  acceptance.
- _No run_ — `P → A`, and `A` is terminal. No run marker, no terminal marker, no
  telemetry record, for the reasons given above.

## Rollout

Five steps. The property to preserve is that at every moment, the set of
evidence kinds any producer emits is a subset of what every deployed reader
parses — so no reader ever sees evidence it would have to guess at.

1. **Readers only.** The ledger gains `read-policy`, `read-acceptance`, and
   `verify-acceptance`, and accepts telemetry `version` 1 **or** 2 while
   `buildTelemetryBody` still emits 1. The controller's `_run_end` matches
   `RUN_END_V1_RE` **or** `RUN_END_V2_RE` while `_finish_run` still emits v1.
   No producer behaviour changes and no existing evidence changes meaning.
2. **Sync and verify.** Propagate to every supported consumer and confirm the
   vendored `review-ledger.js` and `local-review-handoff.py` are at or above the
   reader version in each. Confirmation is reading the installed artefact, not
   reading the sync log.
3. **Emit the new families.** `local-review-policy:v1` and
   `local-review-acceptance:v1` start being posted. This step is safe ahead of
   step 2's completion in principle and is sequenced after it anyway, because
   these families are new: no deployed reader has an opinion about them, for the
   reason proven in the next section.
4. **Enable the new outcomes.** `run-end:v2` and telemetry v2 emission turn on,
   per repository, only where step 2 is confirmed.
5. **Keep reading v1 indefinitely.** Nothing rewrites historical evidence and no
   step deletes a v1 reader. Retirement is a separate decision with its own
   migration evidence.

The direction that could have gone wrong is telemetry, and it is the reason the
order matters more than the count of steps. Had the acceptance fields been added
to v1 in place, step 4 would have shipped records that old readers validate,
truncate, and re-render as complete — a window in which a consumer reports
evidence it does not hold. The version bump is what closes it, and it closes it
at the parser rather than by scheduling.

## Proven unchanged, and assumed unchanged

The distinction is the point of this section. "Proven" means the matcher was
read and either its grammar is untouched or its prefix cannot collide with the
new families.

**Proven unchanged by source inspection:**

- `FINDING_V3_RE`, `DISPOSITION_V3_RE`, `PSEUDO_V3_RE`, `FINDING_V1_RE`,
  `DISPOSITION_V1_RE`. `threadProtocolRecords` reads only review-thread comments
  and fails closed on any unrecognised `local-review` marker on a first line
  (`PROTOCOL_THREAD_MARKER_RE`). Both new families are issue comments, so that
  path is never reached — and the invariant that keeps it true is that policy
  and acceptance markers are never posted into a thread.
  `protocol-rules.test.ts` already pins the fail-closed side.
- `ROSTER_V1_RE` and `ROSTER_V2_RE`. `rosterCandidates` filters on the literal
  `'<!-- local-review-roster:'`; neither new prefix contains it.
- `PASS_V3_RE` and `COMPLETE_V3_RE`. `matchAttestationMarker` requires the
  marker at index 0 and `_sequence_decision` fails closed only on bodies
  containing `'<!-- local-review-pass:'` or `'<!-- local-review-complete:'`.
  Disjoint prefixes.
- `local-review-run:v1`, both readers (`RUN` in `src/runs.ts`, `RUN_V1_RE` in
  the controller). The accepted plans map onto the existing finite
  `local-review-plan:v1 mode=chain engines=…` content that `_content_sequence`
  already parses, and the round cap is an upper bound, so a shorter accepted
  plan needs no new cap. Both readers also key on `startsWith`/`fullmatch`
  against the v1 spelling, so had a bump been needed it would have silently
  dropped runs into the `legacy` attestation namespace via
  `reviewRunForComment` — an argument for not bumping it, not a licence.
- `HANDOFF_V1_RE`. Its `outcome=(clean|minor|material|blocked)` is a per-pass
  outcome, orthogonal to run terminals.
- `local-review-tier:v1` and `local-review-refactor:v1`, and `PR_V1_MARKERS`
  membership, since `postPrComment` is not the posting path for either new
  family.
- The human-glance classifier. `classifyPath` and `classifyFiles` read file
  classifications and set `skip` from `reviewSignificantFiles === 0`. 0010 puts
  the gate ahead of everything and says no fact moves it; no change here does.
- `PROTOCOL_VERSION = 3`. Nothing in this record alters a v3 grammar.

**Assumed unchanged, not proven:**

- The trigger list that `review.trigger_sensitivity` extends. It is prose in
  `REVIEW_WORKFLOW.md`, not code, so "unchanged" is a claim about a document the
  resolver does not yet read.
- `src/effect.ts` and `RangeEffect`. No control in 0010 names it and no marker
  here carries it, but it was not traced through the agent-loop surfaces that
  consume it.
- The engine-identity skew between `SUPPORTED_ENGINES` in `src/constants.ts`
  (which includes `antigravity`) and `ENGINES` in the controller (which does
  not, and aliases `antigravity` to `gemini` inside `_sequence_decision`). It is
  pre-existing and nothing here touches it; whether the assurance work is
  affected was not established.
- Consumer-side vendored copies at versions older than the ones in this tree.
  Step 2 of the rollout exists because that set cannot be established from here.

## Amendments shipped with this record

**To 0009**, the resolver explanation format: the header gains a mode token, the
control lines gain an origin column, requirement 2 is amended so a control with
no selecting facts is expressible, and requirement 3 extends attribution to
locks and maximum mode.

**To 0010**: `review.acceptance_floor` moves to
`human-glance < single-engine < all-engines < lean`; the acceptance-plan table
is renamed to match; a note states that acceptance does not lower
`review.minimum_coverage` and that the minimum is a reported expectation
compared against the measured coverage tier; M5's counterexample gains the
coverage case.

**Also to 0010**, two smaller inconsistencies, neither of which had a separate
amendment in `git log`: the redundant `data_classes = unknown` clause is dropped
from `data.evidence_deidentification` in favour of one note under the `data`
table saying M3 supplies the unknown case for every fact-reading row; and
`supply.audit_blocking_severity` gains the same "the order is on strength, not
on the value" note `autonomy.retries` already carries, because a lower blocking
threshold blocks more and the row reads like a typo without it.

## Consequences

Two marker families are added, one terminal marker and the telemetry record are
versioned, and every other marker family in the protocol is proven unchanged
rather than assumed so. The evidence a run leaves behind is now sufficient to
answer, from the pull request alone, which policy resolved it, whether a human
reduced it, what coverage was actually measured, and whether an expectation was
knowingly unmet.

What this record does **not** do, and what remains for the work that reads it:
the resolver itself, the controller and ledger implementation of the markers
above, the organization registry and its failure behaviour, the setup wizard,
and the per-consumer reader-version audit that step 2 of the rollout depends on.

Rejected here, and recorded so they are not re-proposed: sharing one name
between a plan vocabulary and a measurement vocabulary; letting an acceptance
raise a measured coverage tier; adding acceptance fields to telemetry v1 in
place; carrying `human-accepted` on the one terminal grammar that authenticates
nothing; and treating a completed finite roster and a spent budget as the same
outcome.
