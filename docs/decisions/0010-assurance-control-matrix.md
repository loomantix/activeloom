# 0010 — Assurance control matrix and human review acceptance

- Status: accepted
- Date: 2026-09-21

[0009](0009-repository-assurance-facts.md) fixes the vocabulary: seven orthogonal
fact domains, three provenances, and `unknown` as an explicit absence of claim.
This record is the other half — what those facts _do_. It defines the five
control domains, the fact-to-control matrix, how repository facts compose with
changeset triggers, the human review-acceptance contract, and the monotonicity
invariants that keep the whole thing from collapsing back into a tier ladder.

It adds no fact domain and redefines no term from 0009. Where it needs a concept
0009 does not carry, it says so and names it here.

## Decision

Facts resolve, through a versioned deterministic policy, into five independent
control domains:

| Domain                                      | Owner — the surface that implements it and is the only thing that can change it |
| ------------------------------------------- | ------------------------------------------------------------------------------- |
| `review` — review policy                    | The review entry points and the run controller                                  |
| `autonomy` — agent-loop autonomy            | The agent-loop controller                                                       |
| `data` — data and telemetry safety          | The telemetry gates and the logging/egress lint                                 |
| `supply` — security and supply-chain checks | The repository's CI workflows and required checks                               |
| `governance` — Git and merge governance     | The branch ruleset and the organization registry, external to the resolver      |

There is no domain ordering and no aggregate across domains. A control reads the
facts it names and no others. The resolver emits values; it never applies them —
each owner above reads the resolved vector and acts within its own authority.

Three settings vocabularies, used identically in every table below:

- **defaulted** — the resolver computes the value from facts; a later fact, a
  trigger, or maximum mode may raise it within the same resolution.
- **raised** — a named fact moves this control strictly up its documented order.
  Nothing in resolution moves a control down.
- **locked** — an organization assertion pins the value. A locked control keeps
  its value across resolver-policy changes until the separately governed
  registry changes it ([0009](0009-repository-assurance-facts.md),
  organization-asserted provenance). Lock-eligibility is a property of the
  control and is stated per row; a control marked `no` cannot be locked at all,
  which is itself a governance decision rather than an oversight.

## The five control domains

### `review` — review policy

Owner: the review entry points (`critique`, `deepcritique`, `refactorpass`,
`reviewit`) and the run controller. Reason the domain exists separately: review
depth is the only control a human may lower for a single head, so it must not
share a value with anything that stays machine-governed.

| Control                      | Order / values                       | Default                | Raised by                                                                                                                                                                                                                                                                             | Lock-eligible |
| ---------------------------- | ------------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `review.plan_floor`          | `lean < deep`                        | `lean`, always         | **No fact.** Only an organization lock.                                                                                                                                                                                                                                               | yes           |
| `review.trigger_sensitivity` | set of armed trigger extensions      | base trigger list      | `owned_security_boundaries` (extends trigger 1 to the boundary's own implementation); `distributed_artifact_consumers ≥ known_external` (extends trigger 2 and 3 to publication surfaces); `data_classes` non-empty (extends trigger 1 to the stores and transports of those classes) | yes           |
| `review.required_lenses`     | set, union only                      | `{}`                   | `owned_security_boundaries` → security lens; `data_classes` → data-safety lens; `credible_failure_impacts ∋ financial_assets \| care_delivery \| physical_safety` → correctness/calculation lens; `runtime_access_modes ∋ unauthenticated` → pre-auth surface lens                    | yes           |
| `review.blocking_severity`   | `blocking < blocking+major`          | `blocking`             | `worst_effect_recovery = not_fully_reversible`; `credible_failure_impacts ∋ financial_assets \| care_delivery \| physical_safety`                                                                                                                                                     | yes           |
| `review.minimum_coverage`    | `solo < cross < full`                | `solo`                 | `distributed_artifact_consumers = unbounded_external`; `owned_security_boundaries ∋ authentication \| authorization \| tenant_isolation`                                                                                                                                              | yes           |
| `review.acceptance_floor`    | `human-glance < solo < cross < lean` | `human-glance`, always | **No fact.** Only an organization lock.                                                                                                                                                                                                                                               | yes           |
| `review.evidence_retention`  | `standard < extended`                | `standard`             | `data_classes` non-empty                                                                                                                                                                                                                                                              | yes           |

Two rows deliberately read no fact, and they are the load-bearing ones.

`review.plan_floor` stays `lean` for every fact set, including the most
conservative one resolvable. A repository-wide floor of `deep` is exactly the
"high-risk repository makes every unrelated change Deep" failure this design
rejects: it spends the deep budget on a README while the trigger list — which reads what
the PR actually touched — is the thing that knows a defect is reachable. Facts
raise _sensitivity, lenses, severity, and coverage_; they do not raise the floor.

`review.acceptance_floor` stays `human-glance` for every fact set. A human with
merge authority may accept `human-glance` in the most conservative repository
resolvable. Capping acceptance by facts would rebuild the ladder in the one
place it is most tempting, and it would do so by asserting that a repository's
data classes limit a named human's judgement — which is a claim about the human,
not about the repository, and the facts carry no information about it. An
organization may lock the floor upward; that is a deliberate registry act with
an accountable owner and an audit trail, not a resolver inference.

**`frameworks` is not an input to any control here**, including this one. An
earlier draft of this record carved an exception for `review.evidence_retention`
on the argument that retention selects lifetime and routing rather than a check,
lens, severity, or plan. The exception is withdrawn. Two accepted records —
0009 on the taxonomy and
[0011](0011-assurance-setup-interaction.md) on setup — state the rule without
qualification, and a third record quietly holding a narrow exception is how a
rule stops being enforceable: the next reader has to discover which of the three
governs. Retention resolves from `data_classes` alone. Framework names are still
echoed in the explanation and the evidence record, which is what makes a claimed
audit scope enumerable — echoing is not an input.

**Evidence, per setting.** `standard` retention preserves the resolver
explanation, the policy marker, the resolved plan, the triggers that fired, and
the terminal outcome. `extended` additionally preserves the per-pass roster and
attestation set and the full finding-disposition history for the PR's whole
life. Nothing is _waived_ at `standard`: the difference is retention scope and
duration, not whether a record is written. A control that could waive an
evidence record on a lower setting would make the lower setting unfalsifiable
afterwards, which is the property that matters most when a declaration is later
challenged.

### `autonomy` — agent-loop autonomy

Owner: the agent-loop controller. Reason: autonomy governs what an unattended
agent may _select and edit_, which is a different question from how carefully a
change is reviewed, and a repository can want strict review with wide autonomy
or the reverse.

| Control                        | Order / values                                   | Default            | Raised by                                                                                                                                                             | Lock-eligible |
| ------------------------------ | ------------------------------------------------ | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `autonomy.selection`           | `open < allowlist_only < disabled`               | `open`             | `credible_failure_impacts ∋ physical_safety`; `worst_effect_recovery = not_fully_reversible`                                                                          | yes           |
| `autonomy.scope_exclusions`    | set of excluded surfaces, union only             | `{}`               | `owned_security_boundaries` → that boundary's implementation is excluded from unattended authoring; `data_classes` non-empty → schema and migration surfaces excluded | yes           |
| `autonomy.review_budget_floor` | `none < one_pass < resolved_plan`                | `one_pass`         | any `review.required_lenses` member; `review.blocking_severity = blocking+major`                                                                                      | yes           |
| `autonomy.retries`             | integer ceiling, monotone downward as risk rises | controller default | `worst_effect_recovery ≥ privileged_recovery` lowers the ceiling _(a lower retry ceiling is a stronger control; the order is on strength, not on the integer)_        | yes           |
| `autonomy.stop_conditions`     | set, union only                                  | controller default | `credible_failure_impacts` non-empty → stop on first validation-red rather than retry                                                                                 | yes           |
| `autonomy.publication`         | `draft_pr_only`                                  | `draft_pr_only`    | Nothing. Universal baseline.                                                                                                                                          | no            |

`autonomy.publication` is fixed at `draft_pr_only` for every repository and
cannot be locked because it cannot vary: the agent-loop never holds merge,
deployment, publication, activation, or production authority regardless of any
fact, assertion, or acceptance. Marking it lock-eligible would imply a registry
could grant that authority, and nothing here may.

`autonomy.retries` is the one row whose underlying value moves downward as risk
rises. The control's order is defined on _strength_ — fewer unattended retries
after a failure is the stronger setting — and monotonicity is asserted on the
order, never on the integer. Stating this explicitly is the alternative to the
quiet bug where a reviewer reads "may only raise" and increases a budget.

**Evidence, per setting.** Every setting records the resolved autonomy vector in
the run's policy marker, plus the selection decision, the excluded surfaces that
were consulted, and the stop reason. At `allowlist_only`, the allowlist and its
provenance are preserved. At `disabled`, the record is the refusal itself, which
is preserved rather than being a silent no-op — an absent run and a refused run
must be distinguishable a year later.

### `data` — data and telemetry safety

Owner: the telemetry gates and the logging/egress lint. Reason: these controls
govern what leaves the repository's boundary in a record, which is independent
of how the change was reviewed and survives every acceptance.

| Control                          | Order / values                        | Default             | Raised by                                                                                                                                                | Lock-eligible |
| -------------------------------- | ------------------------------------- | ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `data.egress_policy`             | `default_deny`                        | `default_deny`      | Nothing. Universal baseline.                                                                                                                             | no            |
| `data.evidence_deidentification` | `paths_only < paths_and_shapes_only`  | `paths_only`        | `data_classes` non-empty; `data_classes = unknown`                                                                                                       | yes           |
| `data.finding_quotation`         | `quotation_allowed < structural_only` | `quotation_allowed` | `data_classes ∋ personal \| health \| payment_card \| financial_account \| authentication_credentials \| customer_confidential \| government_controlled` | yes           |
| `data.rationale_copying`         | `never`                               | `never`             | Nothing. Universal baseline.                                                                                                                             | no            |
| `data.measurement_emission`      | `required`                            | `required`          | Nothing. Universal baseline.                                                                                                                             | no            |

Three rows are universal baselines with no fact input and no lock. Default-deny
egress, never copying human rationale prose out of the PR into telemetry, and
keeping measurement emission on are properties of the tooling rather than of the
repository. A fact that could turn any of them off would let a declaration
purchase invisibility, and the declaration is precisely the thing under review.
`data.measurement_emission` being `required` is also what makes the calibration
signal for policy calibration real: a repository whose acceptances are frequent
must be
visible as such.

`data.finding_quotation` at `structural_only` means a reviewer reports the
location, the shape, and the defect, and never the value — the same discipline
0009 already places on the setup agent's reading of fixtures and migrations,
applied to the review record that outlives the PR.

**Evidence, per setting.** `paths_only` preserves file paths, line ranges,
control names, and severities. `paths_and_shapes_only` additionally preserves
type and schema shape while explicitly _waiving_ literal values — that waiver is
the point of the setting and is recorded as a waiver in the explanation, so a
later reader knows the value was withheld rather than absent. No setting waives
the existence of the finding, its severity, or its disposition.

### `supply` — security and supply-chain checks

Owner: the repository's CI workflows and its required checks. Reason: these run
on every push regardless of review plan, and a review outcome must never be able
to satisfy one.

| Control                            | Order / values                                       | Default             | Raised by                                                                                                                                                                                                             | Lock-eligible |
| ---------------------------------- | ---------------------------------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `supply.secret_scanning`           | `required`                                           | `required`          | Nothing. Universal baseline.                                                                                                                                                                                          | no            |
| `supply.dependency_audit`          | `required`                                           | `required`          | Nothing. Universal baseline.                                                                                                                                                                                          | no            |
| `supply.credential_leak_detection` | `required`                                           | `required`          | Nothing. Universal baseline.                                                                                                                                                                                          | no            |
| `supply.audit_blocking_severity`   | `advisory < high < moderate`                         | `advisory`          | `distributed_artifact_consumers ≥ known_external`; `data_classes` non-empty; `runtime_access_modes ∋ unauthenticated`                                                                                                 | yes           |
| `supply.artifact_provenance`       | `none < attested < attested_and_signed`              | `none`              | `distributed_artifact_consumers ≥ organization_only` → `attested`; `= unbounded_external` → `attested_and_signed`                                                                                                     | yes           |
| `supply.dependency_pinning`        | `range_allowed < lockfile_enforced < pinned_digests` | `lockfile_enforced` | `distributed_artifact_consumers = unbounded_external`; `owned_security_boundaries ∋ cryptographic_protection \| secret_management`                                                                                    | yes           |
| `supply.specialized_scanners`      | set, union only                                      | `{}`                | `owned_security_boundaries ∋ cryptographic_protection` → crypto-misuse scanner; `∋ authorization \| tenant_isolation` → access-control scanner; `data_classes ∋ payment_card \| health` → protected-data flow scanner | yes           |

The first three rows are the universal baseline, and they are
deliberately not lock-eligible: a lock implies a registry could unlock, and
there is no fact set, no assertion, and no acceptance under which a repository
stops scanning for secrets. `supply.audit_blocking_severity` is where the
fact-derived depth lives instead.

Specialized scanners provide _advisory evidence about the declaration_ as well
as findings. A scanner finding nothing is not proof that a
protected data class is absent; it warns on contradiction and never rewrites the
declaration.

**Evidence, per setting.** Every setting preserves the check's identity,
version, exit status, and the exact head it ran against. `advisory` preserves
the finding list without a blocking disposition — the finding is recorded, not
waived; only its gate effect differs. `attested` and `attested_and_signed`
additionally preserve the attestation or signature and the identity that
produced it, which is the evidence a consumer of the artifact can later check
independently of this repository.

### `governance` — Git and merge governance

Owner: the branch ruleset and the organization registry, both **external to the
resolver**. Reason: this is the domain most often confused with the others, and
the separation is the point — governance resolves from the contribution and
distribution model and from organization assertions, never from product risk.

| Control                         | Order / values                        | Default                  | Raised by                                                                                         | Lock-eligible |
| ------------------------------- | ------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------- | ------------- |
| `governance.dco`                | `not_required < required`             | contribution-model value | Contribution model only — an open external contributor base. **No product-risk fact.**            | yes           |
| `governance.commit_signing`     | `not_required < required`             | `not_required`           | `distributed_artifact_consumers ≥ known_external`; `worst_effect_recovery = not_fully_reversible` | yes           |
| `governance.admin_merge`        | `permitted < restricted < prohibited` | `permitted`              | `credible_failure_impacts` non-empty; `worst_effect_recovery ≥ privileged_recovery`               | yes           |
| `governance.required_reviewers` | integer, reported expectation         | ruleset value            | Organization assertion only.                                                                      | yes           |

The resolver **reports** these as expected values and never mutates branch
protection, never merges, and never grants authority. The gap between a reported
expectation and the ruleset's actual configuration is a warning in the
explanation and, where an organization enforces it, a failing external check
owned by the registry — not something the resolver reconciles for itself.

`governance.dco` reads no product-risk fact by construction. DCO is about
provenance of contribution: who may contribute and under what certification. A
repository handling clinical data with three internal committers needs it less
than a public library with anonymous contributors, and any rule that raised DCO
from `data_classes` would be asserting the opposite.

**Evidence, per setting.** Every setting preserves the expected value, the
observed ruleset value, and whether they agreed. `required` settings preserve
the per-commit verification result for every commit the PR introduces, not only
its head — a later unsigned fix must not pass through on the head's evidence.
No setting waives the observation; a `not_required` control still records what
the ruleset actually said, so a drift is visible without a policy change.

## How triggers compose with facts

Facts and changeset triggers answer different questions and are combined by one
rule, stated here once:

```
resolved_plan = max( review.plan_floor , trigger_selected_plan )
```

- **Facts set defaults.** They arm and extend triggers, add required lenses,
  raise blocking severity, and raise minimum coverage. They do not select a plan
  for a PR, because they know nothing about what the PR touched.
- **Triggers select the plan** from the changed-file list, exactly as the review
  workflow's trigger list already does. Any one trigger selects Deep; no trigger
  means Lean.
- **`max` is the only combinator.** A trigger can raise the plan above the floor.
  Neither a fact nor a trigger can lower it.

Because `review.plan_floor` is `lean` for every fact set, a repository's facts
never make an unrelated change Deep. The way facts reach a PR is through
`review.trigger_sensitivity`: a repository declaring
`owned_security_boundaries: [authentication]` extends trigger 1 to that
boundary's own implementation paths, so a change _there_ selects Deep in that
repository and would not in a repository that owns no such boundary. A change to
an unrelated build script selects no trigger in either, and is Lean in both.

Required lenses, blocking severity, and minimum coverage apply to whatever plan
resolved. A Lean change in a repository with a data-safety lens requirement runs
Lean _with_ that lens. That is how a high-risk repository gets proportionate
attention on a small change without paying the deep budget for it.

## The human-glance gate

The gate sits **before everything in this record**, unchanged. It classifies the
committed range first, and a range with no review-significant file stops there —
before a plan is resolved, before a control vector is applied, before any policy
or acceptance marker is written, and before any telemetry snapshot.

It is not part of the control matrix and no fact moves it. Its inputs are the
classifier's file classifications and nothing else.

**Line count is never an automatic exemption, at this gate or anywhere else.**
The gate's question is _what kind of file changed_, not _how much changed_. A
one-line change to an authorization check is review-significant; a
two-thousand-line documentation rewrite is not. Size correlates with reviewer
effort, which is the difficulty instinct the tier rules already reject; it
carries no information about what a missed defect reaches. Any rule of the form
"under N lines, skip" would be a scalar exemption reintroduced at the one place
where everything else in this record has been kept off a scale.

An organization lock on `review.plan_floor` does not move the gate either: a
docs-only range in a locked repository is still a docs-only range. What a lock
reaches is what happens once the gate passes.

## Human review acceptance

An authorized non-bot with merge authority on the target branch may select, for
**one exact head**, a plan at or above `review.acceptance_floor`.

### Syntax

A structured PR comment. One command, one head, one plan, one rationale:

```
/assurance accept <plan> --head <exact-head-sha>
<non-empty free-text rationale on the following lines>
```

`<plan>` is exactly one of the four named plans. Anything else is rejected with
the four names listed. A command with an empty or whitespace-only rationale is
rejected — the rationale is required for the record, not for evaluation.

### The four plans

| Plan           | Means                                                                  |
| -------------- | ---------------------------------------------------------------------- |
| `human-glance` | No agent review. The human reads the diff and merges.                  |
| `solo`         | One engine, one adversarial pass.                                      |
| `cross`        | One adversarial pass from each available engine, with no return cycle. |
| `lean`         | The normal Lean plan.                                                  |

`solo` and `cross` cap **adversarial discovery passes**. They do not cap
narrowed exact-head verification that a material fix makes necessary. A plan
that forbade re-checking a fix would convert a review budget into a correctness
hazard, which is the opposite of what a human accepting less review is asking
for.

### Authorization

The actor must be a non-bot identity holding merge authority on the target
branch at the time the command is posted. Authority is captured as a snapshot in
the record, and **rechecked at merge**. An actor who held authority when
accepting and lost it before merge does not carry the acceptance through.

The controller **does not grade the rationale.** It checks that one exists and
is non-empty. An agent judging whether a human's stated reason is good enough
would be an agent holding authority over a human's merge decision, which this
record rejects explicitly. Repeated acceptance is a policy-calibration signal
for the separately governed declaration-honesty and rollout-measurement work —
not evidence of dishonesty, and not grounds for refusal.

### The recorded snapshot

One record per acceptance, containing:

- the normal resolved plan and the accepted plan;
- the actor and the authority snapshot at acceptance time;
- the exact head SHA;
- the triggers that fired and the facts that selected the normal plan;
- the schema version, the policy version, and whether maximum mode was active;
- a digest of the command and of the free-text rationale;
- the unresolved findings outstanding at that head, by identity and severity.

Rationale prose is digested into the record and **never copied into telemetry**
(`data.rationale_copying`). It remains readable in the PR comment where the
human wrote it.

### Invalidation

An acceptance binds one head and one command. It is invalidated by any of:

1. **a changed head** — any new commit, including an amend or a rebase that
   preserves the tree;
2. **a material base or policy change** — the base moved in a way that changes
   the resolved control vector, or the policy version changed;
3. **a new material finding** at the accepted head;
4. **an edited or deleted command comment** — the digest no longer matches;
5. **an amended acceptance** — a later command supersedes the earlier one, which
   is then historical;
6. **loss of authority** — the recheck at merge fails.

Invalidated records are **retained as history**, never deleted. The PR returns
to normal resolution; a human may accept again at the new head.

### Unresolved findings

Findings are never silently dismissed. An acceptance that leaves findings
outstanding records them by identity and severity, and they remain visible on
the PR as open findings with their dispositions intact. Acceptance changes how
much _further discovery_ runs; it changes nothing about what has already been
found.

A **new material defect discovered during exact-head verification returns the PR
to normal resolution.** The human may then authorize the newly resolved plan, or
post a fresh acceptance that explicitly accepts the remaining risk at the new
head. What cannot happen is the original acceptance quietly absorbing a defect
that did not exist when it was written.

## The boundary acceptance does not cross

Review acceptance governs **`review` and `autonomy` only**. It reaches:

- which review plan runs at one exact head, and
- what the agent-loop's review budget for that head is.

It reaches **nothing else**. Specifically, an acceptance comment never:

- merges, or authorizes a merge;
- deploys, activates, promotes, or releases anything;
- publishes an artifact or a package version;
- waives commit signing, DCO, or any `governance` control;
- waives secret scanning, dependency auditing, credential-leak detection, or any
  `supply` control, at any severity;
- waives any `data` control, including egress policy, evidence
  de-identification, or measurement emission;
- satisfies a required check, an organization registry check, a profile check,
  or a branch rule;
- grants production access, secret access, or any credential.

Each of those surfaces keeps its own authority and its own break-glass path with
its own audit event. The single sentence that covers the class: **an acceptance
is an input to the review controller, and the review controller has no authority
to give away.** A reader who finds a proposed behaviour on the wrong side of
this line should treat the proposal as wrong rather than the boundary as
negotiable.

## Monotonicity invariants

Each invariant is stated as a property, then as the counterexample that would
violate it. The counterexamples are the testable half.

**M1 — Fact monotonicity.** Adding a value to a set fact, or raising a scalar
fact along its documented order, never lowers any control in any domain.

> _Violated by:_ a policy that maps `data_classes: [health]` to a clinical lens
> set that **replaces** the generic security lens. A repository declaring
> `[authentication_credentials]` runs the credential lens; adding `health` drops
> it. `review.required_lenses` is union-only for exactly this reason — a fact
> may add a lens, never substitute one.

**M2 — Provenance monotonicity.** An organization assertion may only raise. The
conservative join of declared and organization-asserted facts is the resolution
input, and detection participates in neither direction.

> _Violated by:_ a registry asserting `network_exposure: private_network` on a
> repository that declared `internet`, and the resolution taking the assertion
> as authoritative in both directions — lowering every exposure-derived control
> on the strength of a registry entry that is easier to edit than the service.

**M3 — Unknown conservatism, bounded.** `unknown` resolves every control that
reads that fact at the fact's most conservative value, and raises no control
that does not read it.

> _Violated in one direction by:_ treating `worst_effect_recovery: unknown` as
> `routine_reversal` so the repository validates quietly. Violated in the other
> by treating it as a repository-wide maximum that also raises
> `governance.dco`, which reads no recovery fact. Both break M3; the second is
> the one that gets shipped because it looks conservative.

**M4 — Trigger composition monotonicity.** The resolved plan is
`max(plan_floor, trigger_selected_plan)`. Neither input can lower the other.

> _Violated by:_ a rule that lets a benign fact set downgrade a trigger-1
> change to Lean — "this repository declares no data classes, so its
> authorization change is Lean". The trigger read what the PR touched; the facts
> did not, and the facts do not get a veto over the triggers.

**M5 — Acceptance containment.** An acceptance lowers a control only within
`review` and `autonomy`, only at one exact head, and only down to
`review.acceptance_floor`.

> _Violated by:_ a `human-glance` acceptance that also skips the dependency
> audit because "no agent review runs". The audit is a `supply` control on a
> separate owner and runs on the push, not on the plan.

**M6 — Environment monotonicity.** `LOOM_ASSURANCE_MODE=maximum` raises every
domain to its strictest supported setting. No environment value lowers a
control, selects a plan, replaces a fact, or overrides an assertion.

> _Violated by:_ any variable of the form `LOOM_REVIEW_PLAN=lean`, and equally
> by a precedence rule where an environment value wins in both directions
> because it was written as a simple override rather than a join.

**M7 — Lock persistence.** A locked control keeps its value across resolver
policy changes until the registry changes it.

> _Violated by:_ a policy version that recomputes every control from facts and
> silently drops locks it no longer knows about. A lock a policy upgrade can
> forget is not a lock.

**The explicit non-invariant.** A **new policy version may lower a control
deliberately.** Monotonicity constrains resolution within one policy version; it
does not freeze policy. That is why the policy version is separate from the
schema version, why it appears in every explanation and marker, and why a policy
change must ship a before/after effective-control diff. Claiming monotonicity
across policy versions would make every calibration fix impossible and would be
false the first time one shipped.

## How this avoids a hidden scalar hierarchy

This is the acceptance condition most likely to fail silently, because a matrix
can be perfectly orthogonal on paper and collapse in implementation the first
time someone adds a convenience aggregate. Five structural properties hold the
line, each independently checkable:

**1. Every control names its facts.** No control reads "the facts" or a derived
aggregate. The tables above are the whole input specification, and a control
that needed an unlisted fact would be a matrix change, not an implementation
detail. There is no score, no weight, no risk number, and nothing that could be
sorted.

**2. No cross-domain ordering exists.** The five domains are not ranked and
never compare. There is no operation that takes a control vector and returns a
level, and no place a level would be stored if one were computed.

**3. Incomparability is demonstrable.** Take 0009's own pair. A public
command-line tool — `network_exposure: none`, `distributed_artifact_consumers:
unbounded_external`, `data_classes: []` — resolves
`supply.artifact_provenance: attested_and_signed`,
`supply.dependency_pinning: pinned_digests`,
`governance.commit_signing: required`, and
`data.finding_quotation: quotation_allowed`. An internal web service —
`network_exposure: internet`, `distributed_artifact_consumers: none`,
`data_classes: [personal, health]`, `owned_security_boundaries:
[authentication, tenant_isolation]` — resolves
`supply.artifact_provenance: none`, `governance.commit_signing: not_required`,
`data.finding_quotation: structural_only`, `review.minimum_coverage: cross`, and
a data-safety lens. **Neither vector dominates the other.** Each is strictly
stronger than the other on at least one control. No total order contains both.

**4. The two floors read no fact.** `review.plan_floor` and
`review.acceptance_floor` are the two controls a scalar hierarchy would most
naturally drive, and they are the two this record refuses to let facts touch. If
either ever acquires a fact input, the ladder is back regardless of what the
rest of the matrix says.

**5. Presets never exist at runtime.** Per 0009, presets are setup templates
expanded into explicit facts at write time. Runtime policy reads facts. A preset
name surviving into resolution would be a scalar by another spelling.

**The falsification test, stated so it can be run.** For every pair of controls
`X` and `Y` in the matrix, there must exist two resolvable fact sets `A` and `B`
such that `A` resolves `X` strictly higher than `B` does, and `B` resolves `Y`
strictly higher than `A` does. A pair for which no such `A` and `B` exist is a
pair that always moves together — and a matrix whose controls all move together
is a scalar level with extra columns. Any control that cannot be separated from
another either needs a fact input that distinguishes them, or should be merged
into the other and stop pretending to be independent.

Applied to the universal baselines: `data.egress_policy`,
`data.rationale_copying`, `data.measurement_emission`,
`supply.secret_scanning`, `supply.dependency_audit`,
`supply.credential_leak_detection`, and `autonomy.publication` are constants,
not controls, and are exempt from the test by construction. They are listed in
the matrix so that their constancy is a recorded decision rather than an
omission, and a proposal to give any of them a fact input is a proposal to
change this record.

## Consequences

Every control above has a named owner, a stated reason, an explicit default, and
an explicit lock-eligibility verdict. Two controls read no fact and say why; six
are universal baselines and say why they cannot be locked. Acceptance reaches two
domains and is enumerated against the ones it does not reach.

What this record does **not** do, and what remains for the work that reads it:
the resolver implementation and its policy versioning; the controller and ledger
changes that carry policy and acceptance markers; the setup wizard; the
organization registry and its failure behaviour; and the environment-surface
audit that M6 depends on. Each is free to add controls that read 0009's facts —
but none may add a fact domain without amending 0009 and its schema version, and
none may give `review.plan_floor` or `review.acceptance_floor` a fact input
without amending this one.

Rejected here, and recorded so they are not re-proposed: a repository-wide deep
floor derived from facts; a fact-derived cap on what a human may accept; line
count as an automatic exemption at any gate; acceptance reaching merge,
deployment, publication, signing, scanner, or branch controls; an agent grading
human rationale; a framework name as an input to any control, retention
included; lens sets that substitute rather than union; and any aggregate
that reduces a control vector to a level.
