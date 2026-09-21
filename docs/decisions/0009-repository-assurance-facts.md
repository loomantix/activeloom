# 0009 — Repository assurance facts and framework metadata

- Status: accepted
- Date: 2026-09-21

This record defines a taxonomy rather than an engine divergence. It is the
vocabulary the assurance resolver, the control matrix, and the setup skill are
all written against, so it is recorded here rather than in any one of them.

## Decision

A repository describes itself to ActiveLoom as a set of **orthogonal facts**,
not as a point on a scale. Public-artifact risk, externally reachable product
risk, protected-data risk, distribution reach, and operational impact do not
form one total order, and every attempt to collapse them into one produces a
repository that is simultaneously above and below its own tier.

The declaration lives under `assurance:` in `.activeloom-config.yml`:

```yaml
assurance:
  schema_version: 1
  facts:
    data_classes: []
    runtime_access_modes: []
    network_exposure: unknown
    distributed_artifact_consumers: unknown
    owned_security_boundaries: []
    credible_failure_impacts: []
    worst_effect_recovery: unknown
  frameworks: []
```

Seven fact domains, each answerable by someone who knows the repository, none
of them requiring compliance counsel to answer. `frameworks` is audit metadata
and never a resolver input.

`schema_version` is an integer. A version this resolver does not recognize —
newer, or malformed — fails closed rather than degrading to a guess.

## The seven fact domains

Each domain below gives its type, its values with definitions, a counterexample
for the values that are most often claimed wrongly, and its aggregation rule
for a repository that spans several concerns.

Every set-valued field accepts either a list (possibly empty) or the scalar
`unknown`. Every scalar field accepts one of its named values or `unknown`.

### `data_classes` — set

What data the software this repository produces processes, stores, or
transports in a supported deployment. Not what a test fixture contains, and not
what an adjacent system holds.

| Value                        | Means                                                                                                                         |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `personal`                   | Data identifying, or reasonably linkable to, a natural person.                                                                |
| `health`                     | Health or clinical information about an identifiable person.                                                                  |
| `payment_card`               | Cardholder data — primary account numbers, and the elements that travel with them.                                            |
| `financial_account`          | Bank, payout, or ledger-account details and balances belonging to a party.                                                    |
| `authentication_credentials` | Credentials belonging to users, tenants, or other systems that this software issues, verifies, stores, or forwards.           |
| `customer_confidential`      | Non-public content entrusted by a customer that falls in no other class — documents, messages, agreements, usage records.     |
| `government_controlled`      | Data handled under a government control regime: export-controlled material, or identifiers carrying statutory handling rules. |

Counterexamples:

- An aggregate count of accounts with no per-person row is **not** `personal`.
  A hashed identifier that can still be re-linked to a person **is**.
- A clinical terminology table or a public drug reference is **not** `health`;
  no person is attached to it.
- An opaque processor token or charge identifier that cannot reconstruct a card
  is **not** `payment_card`. Retaining last four digits and expiry **is**.
- A currency-conversion table is **not** `financial_account`. A stored payout
  destination **is**.
- The repository's own deploy credential held by CI is **not**
  `authentication_credentials` — that is covered by the universal baseline, and
  treating it as a data class would make every repository declare it, which
  would make the value carry no information.

**Aggregation:** union. A repository that handles clinical notes in one service
and invoices in another declares both.

### `runtime_access_modes` — set

_Who_ can reach a running instance of this software. Deliberately separate from
where it runs, which is `network_exposure`.

| Value                     | Means                                                                                                |
| ------------------------- | ---------------------------------------------------------------------------------------------------- |
| `workforce_authenticated` | Reachable only by authenticated members of the operating organization.                               |
| `external_authenticated`  | Reachable by authenticated principals outside that organization — customers, partners, tenant users. |
| `unauthenticated`         | Some capability performs work or returns data with no authenticated principal.                       |

Counterexamples:

- A service reachable only over a private network, whose users are a customer's
  staff, is `external_authenticated`. The audience is external even though the
  network is not. This is the distinction the two fields exist to keep apart.
- A sign-in endpoint **is** `unauthenticated`. It is pre-authentication attack
  surface by construction, and a declaration that omits it is describing the
  authenticated half of the system only.
- A static liveness or readiness probe returning a fixed value is **not**
  `unauthenticated`; it performs no work and returns no data.

An empty set is a positive claim that nothing here exposes a reachable runtime —
a library, a CLI, a build tool. That claim does not make the repository
low-consequence; distribution reach is carried by the next field but one.

**Aggregation:** union.

### `network_exposure` — scalar

Where a running instance listens.

| Value             | Means                                                                                    |
| ----------------- | ---------------------------------------------------------------------------------------- |
| `none`            | Nothing produced here listens on a network in a supported deployment.                    |
| `private_network` | Listens only on a network that requires separate admission — private network, mesh, VPN. |
| `internet`        | Reachable from the public internet, directly or through a public ingress, proxy, or CDN. |
| `unknown`         | The declarer could not establish it from what they can see.                              |

Counterexample: a service behind an internet-facing gateway with an address
allowlist is `internet`. The allowlist is a control on that exposure, not a
different exposure class; controls are resolved outputs, never declared inputs.

**Aggregation:** conservative maximum along `none < private_network < internet`.

### `distributed_artifact_consumers` — scalar

Who may consume an artifact published out of this repository. Distribution, not
deployment.

| Value                | Means                                                                                        |
| -------------------- | -------------------------------------------------------------------------------------------- |
| `none`               | Nothing is published for consumption outside its own deployment.                             |
| `organization_only`  | Published where only the operating organization can consume it — a private registry or feed. |
| `known_external`     | Distributed to an enumerable set of external consumers under agreement.                      |
| `unbounded_external` | Published where anyone may consume it — a public registry, download, or store.               |
| `unknown`            | The declarer could not establish it.                                                         |

Counterexamples:

- A container image built here and deployed by the same pipeline is `none`.
  That is deployment; nobody else consumes the artifact.
- A private internal package consumed by other repositories of the same
  organization is `organization_only`, and that is materially different from a
  public package: the consumer set is enumerable, reachable, and can be told to
  upgrade.

The pair that shows why this is its own axis: a public command-line tool that
runs entirely on a developer's machine is `unbounded_external` with
`network_exposure: none`, while an internal-only web service is `internet` with
`distributed_artifact_consumers: none`. Neither dominates the other.

**Aggregation:** conservative maximum along
`none < organization_only < known_external < unbounded_external`.

### `owned_security_boundaries` — set

Security decisions this repository's code _makes_, as distinct from security
properties it _consumes_ from something else.

| Value                      | Means                                                                                                   |
| -------------------------- | ------------------------------------------------------------------------------------------------------- |
| `authentication`           | Establishes or verifies identity claims — issuing or validating sessions and tokens, verifying factors. |
| `authorization`            | Decides what a principal may do — permission checks, policy evaluation, scope or role enforcement.      |
| `tenant_isolation`         | Keeps one tenant's data and actions separate from another's — query scoping, per-tenant key selection.  |
| `cryptographic_protection` | Chooses or implements encryption, signing, hashing, key derivation, or their parameters.                |
| `secret_management`        | Stores, distributes, rotates, or brokers secrets on behalf of something else.                           |

Counterexamples:

- Calling an identity provider's SDK and trusting its verified result is
  consuming. Implementing the token validation, deciding session lifetime, or
  writing the verification itself is owning.
- Enabling transport encryption on a managed load balancer is consuming.
  Selecting a key-derivation function, or implementing envelope encryption, is
  owning.
- Reading a secret from an injected environment variable is consuming.
  Handing secrets to other workloads is owning.

An empty set is a positive claim that this code makes no security decisions of
its own — a claim worth making, and worth being able to challenge.

**Aggregation:** union.

### `credible_failure_impacts` — set

What a defect that shipped undetected could plausibly reach. _Credible_ means a
plausible defect in this software, not an unbounded chain of unrelated
failures.

| Value                 | Means                                                      |
| --------------------- | ---------------------------------------------------------- |
| `internal_operations` | Degrades the operating organization's own work.            |
| `customer_operations` | Degrades a customer's ability to do their work.            |
| `financial_assets`    | Causes incorrect movement, loss, or misstatement of money. |
| `care_delivery`       | Affects the delivery or clinical correctness of care.      |
| `physical_safety`     | Can contribute to physical harm to a person.               |

Counterexamples:

- A report that is read but never acted on financially is
  `customer_operations`, not `financial_assets`. No money moves.
- A tool whose clinical output is reviewed by a clinician before it reaches the
  record is still `care_delivery`. Human review changes the likelihood of a
  defect landing, not the domain it lands in, and likelihood is not what this
  field measures.

These values deliberately do not order. `financial_assets`, `care_delivery`,
and `physical_safety` can coexist in one repository, and ranking them against
each other is exactly the scalar collapse this taxonomy exists to avoid.

**Aggregation:** union.

### `worst_effect_recovery` — scalar

How hard it is to undo the worst credible effect.

| Value                  | Means                                                                                                                                                                                                               |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `none`                 | The worst credible effect needs no recovery — a wrong value shown, corrected on the next run.                                                                                                                       |
| `routine_reversal`     | Reversible by an ordinary user or operator through a supported path: edit, re-run, redeploy, revert.                                                                                                                |
| `privileged_recovery`  | Reversible only with elevated access or an out-of-band process: restore from backup, administrative correction, coordinated re-issue.                                                                               |
| `not_fully_reversible` | Some effect cannot be undone — data disclosed to a third party, an artifact consumed by unknown parties, money settled externally, a submission accepted by an external authority, a message delivered to a person. |
| `unknown`              | The declarer could not establish it.                                                                                                                                                                                |

Counterexample: a published package version can be deprecated but not
un-consumed, so a repository publishing to an open registry is
`not_fully_reversible` even when every published version can be yanked.

**Aggregation:** conservative maximum along
`none < routine_reversal < privileged_recovery < not_fully_reversible`.

## Aggregation, in one rule

Mixed-risk repositories aggregate at repository scope. There is no path-scoped
declaration: per-path risk sensitivity already lives in the changeset triggers,
and a second, weaker path taxonomy whose only new power is exempting paths puts
a careless glob one edit away from covering a service.

- **Set fields** take the union of every concern in the repository.
- **Scalar fields** take the conservative maximum along the documented order.
- **`unknown` is subsumed only by a strictly higher known value.** If any
  contributing concern is `unknown` and no other contributor sits above it in
  the order, the aggregate is `unknown`. `unknown` never averages down.

The aggregate of a repository is therefore always at least as conservative as
any one thing inside it, which is the property that makes "split the repository"
the honest remedy for a declaration that has become expensive.

## `unknown` versus verified absence

These are different claims and the schema keeps them apart.

- `[]` on a set field, or a named low value on a scalar field, is **verified
  absence**: someone looked and none applied. It is a positive assertion, it is
  attributed in the resolver's explanation as declared, and it can be
  challenged.
- `unknown` is the absence of a claim. It is never written by tooling to make a
  file validate, and it is never silently treated as `[]`.
- Within a present `assurance:` block, a **missing** field is `unknown`, not
  empty. Saying "none" requires writing `[]`.
- `unknown` **raises only the controls that read that fact**, and each such
  control resolves as if the fact held its most conservative value. It does not
  raise unrelated controls, and it does not push the whole repository to a
  maximum.
- A repository is never resolved as low-assurance on the strength of `unknown`.

The consequence worth stating plainly: an adopter who declares nothing gets a
conservative resolution and a warning, not a silent downgrade — and an adopter
who declares `[]` everywhere has made a reviewable claim rather than an
unexamined one.

## Declared, detected, and organization-asserted facts

Three provenances, one precedence rule.

**Declared** — written in the repository's own configuration by someone with
write access to it. Authoritative for the repository's own defaults. It is also
cheap to change, which is why it cannot be the only thing standing between a
repository and a reduced control set.

**Detected** — inferred by tooling from repository content. Detection is
heuristic and its output changes when the detector changes.

- It is never written into the declaration automatically.
- It never participates in resolution, in either direction.
- Its two jobs are to propose defaults during setup, and to warn when a
  declaration contradicts visible evidence in the repository.
- A detection that contradicts a declaration warns; it does not fail a build on
  its own. A screening heuristic that blocks merges is a heuristic that gets
  switched off.

**Organization-asserted** — supplied by a registry outside the repository, for
repositories that organization owns. It may assert minimum facts and lock
specific controls, and it is authoritative over the declaration where it
speaks — but only in the raising direction.

Resolution is therefore the conservative join of **declared** and
**organization-asserted** facts, and nothing else. Detection stays out of it
deliberately: resolution must be reproducible from two recorded inputs, so that
the same commit resolves identically today and a year from now, rather than
moving because a detector was improved in between.

## Framework metadata

`frameworks` is a list of names identifying compliance or audit regimes a
repository is claimed to sit within.

- **Frameworks are not resolver primitives.** No control is selected because a
  framework name is present, and removing a name changes no control. A
  declaration with an empty `frameworks` list and one naming three regimes
  resolve identically given the same facts.
- **There is no universal framework-to-control mapping**, and this record
  refuses to ship one. The obligations behind a given regime depend on the
  operating organization's own scope statement, so a shared mapping would be
  wrong in both directions at once: asserting obligations an adopter does not
  have, and implying coverage the mapping cannot deliver.
- **What the metadata is for:** explanation, evidence routing, and audit
  reporting. The resolver echoes the names in its explanation and its evidence
  records, so that the set of repositories claimed within a scope can be
  enumerated, and so that an organization assertion can be checked against what
  a repository claims about itself.
- **A name outside the known vocabulary warns and is carried verbatim.** It is
  never rejected, and never given meaning.
- **The word "regulated" is not derived here.** Legal and contractual scope is
  an organizational assertion or it is not made at all. Nothing lets a
  repository self-declare into or out of a regulatory scope by editing a list.

## Resolver explanation format

The resolver emits one explanation per resolution. It is the artifact a human
reads when the outcome surprises them, and the artifact an auditor reads later,
so the format is fixed rather than left to the caller.

Requirements:

1. **Deterministic.** Identical inputs produce byte-identical output. Facts
   appear in schema order; controls appear in a stable order; sets are sorted.
2. **Complete and minimal.** Every fact appears with its resolved value and its
   provenance. Every resolved control appears with the facts that selected it,
   and only those. Nothing that did not participate is listed.
3. **Attributed.** A value raised by an organization assertion, or defaulted
   because a fact is `unknown`, says so on its own line.
4. **Versioned.** Schema version and policy version appear in the header, since
   the same facts under a later policy may resolve differently.
5. **Reproducible offline** from the recorded declaration and the named policy
   version, with no network access and no repository scan.
6. **Prose-free and PR-sized.** One line per fact and per control.

The shape:

```
assurance: schema 1, policy 3
facts
  data_classes                    health, personal            declared
  runtime_access_modes            external_authenticated      declared
  network_exposure                internet                    org-asserted (declared: private_network)
  distributed_artifact_consumers  none                        declared
  owned_security_boundaries       authentication, tenant_isolation  declared
  credible_failure_impacts        care_delivery, customer_operations  declared
  worst_effect_recovery           not_fully_reversible        unknown, resolved conservatively
frameworks
  <names, echoed, no control effect>
controls
  <domain>.<control>              <value>   <- <facts that selected it>
warnings
  - <one line per warning, including detection contradictions>
```

## What the setup agent may read

Setup is context-first. The agent reads the repository and proposes a complete
declaration; it does not begin with a questionnaire.

Permitted evidence, all of it local and read-only:

- dependency manifests and lockfiles — authentication libraries, cryptography
  libraries, payment or clinical SDKs, database drivers;
- CI and release workflow definitions — what is built, where it deploys,
  whether it publishes, which registry, which scanners are already required;
- deployment descriptors present in the repository — containers, charts,
  serverless definitions, infrastructure declarations — for listeners and
  ingress;
- publication metadata — package name and visibility, registry target, private
  flags, license — for distribution reach;
- repository guidance — README, contributor and agent guides, architecture
  docs — for the stated purpose and audience;
- source-level structure — route and handler declarations with their
  authentication middleware, schema and migration definitions, cryptographic
  call sites, tenancy-scoping helpers;
- security configuration already present — scanner configuration, required
  checks, existing allowlists;
- an existing `assurance:` block, when re-running.

Explicitly not evidence: production systems, live data stores, cloud accounts,
issue trackers, and anything requiring a credential. Detection reads the
repository it is run in and nothing else.

Two handling rules the agent follows while reading: it cites paths as evidence
and never copies file content into its recommendation, and it never repeats
data values found in fixtures, migrations, or logs. The recommendation is a
structural claim about the repository, and it should be readable by someone who
is not permitted to see the repository's data.

## The one-accept setup path

1. **Compute a complete candidate.** Every field gets a value. Nothing is left
   blank for the user to fill in.
2. **Classify each field** as `confident` (repository evidence distinguishes
   the value), `conservative-default` (no evidence, defaulted upward), or
   `must-ask`.
3. **A field is `must-ask` only when it is material and locally
   undetermined.** _Material_ means at least one control differs between its
   candidate values under the current policy. _Locally undetermined_ means no
   repository evidence distinguishes them. A field that is one but not the
   other is never asked about.
4. **Ask only the `must-ask` set**, at most one question per fact, phrased in
   the repository's own terms. Never a compliance questionnaire, and never a
   walk through all seven domains.
5. **Present one summary**: the full expanded declaration, the effective
   control vector, the delta from current behavior, and the evidence path
   behind each confident value.
6. **Accept in one action**, or edit individual fields. Acceptance writes
   normalized, fully expanded facts — never a preset name, never narrative
   rationale. Presets exist only as setup templates and are expanded at write
   time; runtime policy never reads one.
7. **Zero `must-ask` facts means zero questions.** The path is accept-or-edit,
   not interview-then-accept.
8. **Never change an existing declaration without explicit confirmation.** A
   re-run on a declared repository reports a diff and stops.

The residue that genuinely cannot be settled locally, and is therefore what the
questions are normally about:

- whether real personal, health, or payment data reaches a supported
  deployment, when the code merely has the shape for it — the same schema
  serves a demo and a clinical system;
- whether the service is reachable from the public internet, when ingress is
  defined outside the repository;
- who actually consumes a published artifact, when the registry target is not
  declared locally;
- whether worst-case effects settle externally — money moved, a message
  delivered, a filing accepted;
- whether the repository is claimed within an audit scope, which is always an
  organizational answer and never a local one.

## Consequences

The taxonomy meets its acceptance conditions by construction: private
organization-only packages and public artifacts are separated by
`distributed_artifact_consumers`; audience and network position are separated by
`runtime_access_modes` and `network_exposure`; and coexisting financial,
clinical, and safety impacts are carried as an unordered set rather than
competing for one rung on a ladder.

What this record does **not** do, and what remains for the work that reads it:
the control matrix that maps facts to review, autonomy, telemetry, supply-chain,
and governance decisions; the resolver implementation; the setup skill; ledger
and marker compatibility; and the organization registry and its failure
behavior. Those are separate decisions, and each is free to add controls that
read these facts — but none of them may add a fact domain without amending this
record and the schema version with it.

Rejected here, and recorded so they are not re-proposed: scalar assurance
levels; runtime-visible preset names; per-path assurance declarations; universal
framework-to-control mappings; and letting detection participate in resolution.
