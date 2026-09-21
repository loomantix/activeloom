# 0011 — Assurance setup interaction design

- Status: accepted
- Date: 2026-09-21

[0009](0009-repository-assurance-facts.md) fixes the fact vocabulary, the
permitted evidence, the declared/detected/organization-asserted precedence
rule, and the definition of a `must-ask` fact. This record turns the short
"one-accept setup path" section of that document into the full interaction
design for the `assurance-setup` skill: where the skill's authority ends, what
it shows, what it asks, what it writes, and what it hands to a human to paste
into a pull request.

It is an interaction design, not an implementation. It fixes no control values:
the control vector is an opaque output of the resolver here, and the matrix
that produces it is decided separately.

## Decision

`assurance-setup` is a separate, repository-scoped skill that reads local
context first, computes a complete candidate declaration, presents it once, and
normally finishes in one user action. It recommends facts. It never interprets
policy, and it never computes a control.

## Separate from `review-setup`

`review-setup` and `assurance-setup` look superficially alike — both are
short interactive skills that end in a written configuration — and they are
kept apart because every property that matters about them differs.

|                   | `review-setup`                          | `assurance-setup`                              |
| ----------------- | --------------------------------------- | ---------------------------------------------- |
| Subject           | The person running reviews              | The repository                                 |
| Scope             | One machine, all repositories           | One repository, all contributors               |
| Storage           | A profile file outside every repository | `assurance:` in `.activeloom-config.yml`       |
| Change path       | Written directly, effective next run    | Committed, reviewed, merged                    |
| Authority         | The user's own preference               | A claim others rely on                         |
| Wrong value costs | A slower or unavailable engine          | A control that should have applied and did not |
| Evidence          | The engine CLIs installed here          | The repository's own content                   |

Merging them would put a per-user preference and a repository-wide assurance
claim behind one command, where accepting a recommendation about your own model
effort and accepting a recommendation about whether this software touches
health data are the same keystroke. The second deserves a pull request and a
reviewer; the first does not.

The separation is one of **authority and user experience**, not of code.
Shared low-level helpers are expected and allowed:

- reading, parsing, and writing configuration with a schema-version guard,
  including the refusal behavior when a file was written by a newer copy;
- the "propose a complete candidate, accept or edit individual fields"
  presentation primitive;
- terminal formatting for a proposal table and a before/after diff;
- the rule that a helper script is the only writer and the skill never edits
  the file by hand;
- non-interactive detection and refusal-to-guess behavior.

What may not be shared is authority. Neither skill may write the other's file,
neither may read the other's settings as an input to its own recommendation,
and a change to one never migrates or rewrites the other. In particular, a
repository's assurance facts must never depend on which engines the person
running setup happens to have installed.

## Context sources and evidence precedence

The permitted evidence is exactly the list in 0009: dependency manifests and
lockfiles, CI and release workflow definitions, deployment descriptors,
publication metadata, repository guidance, source-level structure, security
configuration already present, and an existing `assurance:` block. All of it
local, read-only, and inside the repository the skill was invoked in. Not
production systems, live data stores, cloud accounts, issue trackers, or
anything requiring a credential.

Two handling rules from 0009 hold throughout the interaction, including in the
summary and in the generated pull-request text: **cite paths, never copy file
content**, and **never repeat data values** found in fixtures, migrations, or
logs. The recommendation is a structural claim about the repository and must be
readable by someone not permitted to see the repository's data.

### Precedence among the three provenances

Unchanged from 0009 and restated because the interaction depends on it:

1. **Organization-asserted** wins where it speaks, in the raising direction
   only.
2. **Declared** is authoritative for everything else.
3. **Detected** never participates in resolution at all. It proposes defaults
   during setup and warns about contradictions. That is its whole job.

### Precedence among evidence sources within detection

Detection frequently sees two local signals that point different ways. The
ordering below decides which one the candidate uses. It applies only inside the
recommendation; none of it reaches the resolver.

1. **An explicit existing declaration** beats every detected signal. A re-run
   proposes no change to a declared field on detection alone.
2. **A configuration file whose purpose is to state the thing** beats an
   inference from code shape. A publication manifest marked private beats a
   package name that looks public.
3. **A deployment or workflow definition that is actually executed** beats one
   that is present but unreferenced. An example or template file is weaker
   evidence than a workflow on the default branch.
4. **Code that makes the decision** beats a dependency that could make it. A
   cryptography library in the lockfile is not
   `owned_security_boundaries: cryptographic_protection`; a call site choosing
   parameters is.
5. **Repository guidance** — README, contributor guides, architecture docs —
   is the weakest tie-breaker. It is prose, it ages, and it is the one source
   whose claims nobody validates.

### When two sources disagree

The skill resolves the disagreement conservatively, records it, and surfaces
it. Concretely, for a field where sources conflict:

- If the ordering above cleanly separates them, the higher-precedence source
  supplies the candidate value and the conflict is listed as an assumption in
  the summary.
- If it does not, or if the sources disagree about a **material** fact — one
  where at least one control differs between the candidate values — the field
  becomes `must-ask`. A genuine local contradiction about something that
  changes controls is exactly the residue a question exists for.
- If the field is immaterial under the current policy, the conservative value
  wins silently and nothing is asked. The user is not made to arbitrate a
  disagreement with no consequence.

The skill never averages, never splits the difference, and never picks the
value that produces the lighter control set because it produces the lighter
control set.

## The boundary: recommendation versus resolver

This is the load-bearing separation in the whole design.

**The agent recommends facts.** It reads the repository, proposes a value for
each of the seven fact domains, classifies its confidence, cites the evidence
path behind each confident value, and asks about the residue. That is the
entire scope of its judgement.

**The deterministic resolver computes controls.** It takes declared and
organization-asserted facts plus a named policy version, and emits the control
vector and the explanation described in 0009. It is pure, offline, and
reproducible.

The rules that keep the boundary from eroding:

- **Every control value shown during setup comes from a resolver call.** The
  skill invokes the resolver on the candidate facts and displays what it
  returns. It never predicts, describes, summarizes from memory, or
  hand-writes a control value — including in the pull-request text, where a
  plausible-sounding invented control is hardest to catch.
- **Detected facts are never resolver inputs.** The resolver is called on the
  candidate as if it were declared, so that what the user sees at accept time
  is what the repository will resolve to after the commit lands.
- **The agent may not reason backwards from a desired control.** It is not
  permitted to notice that a fact value would produce an unwelcome control and
  choose the other value. Facts are chosen from evidence only.
- **The agent may not explain why a control applies.** It shows the resolver's
  own explanation, which attributes each control to the facts that selected it.
  An agent-authored rationale for a control is a second, unversioned policy.
- **No fact is invented to make the file validate.** `unknown` is available and
  is the correct answer when nothing establishes a value.
- **A resolver that fails, is unavailable, or reports an unrecognized schema or
  policy version stops setup.** The skill reports the error verbatim and writes
  nothing. It does not degrade to showing facts without controls, and it does
  not guess.

The practical test for any future change to this skill: if the skill would
still be correct after the control matrix is rewritten, it is on the right side
of the boundary. If a policy change would require editing the skill, the skill
has absorbed policy and the change is wrong.

## The summary and the one-accept flow

The skill presents exactly one summary. It contains, in this order:

1. **Header** — the repository, the schema version, the policy version, and
   whether an organization registry was consulted and answered.
2. **Recommended declaration** — all seven fact domains, in schema order, each
   with its candidate value, its classification (`confident`,
   `conservative-default`, `must-ask`, or `declared` on a re-run), and, for
   confident values, the evidence path that produced it. Set values sorted.
   Nothing blank.
3. **What this says about the repository** — a short plain-language reading of
   the facts that carry the most weight. Two or three sentences, not a
   paragraph per domain, and phrased in the repository's own terms rather than
   in compliance vocabulary.
4. **Assumptions and uncertainties** — every `conservative-default`, every
   source disagreement resolved by precedence, and every `unknown` retained,
   each one line, each saying what would change it.
5. **Effective controls** — the resolver's explanation for the candidate
   facts: the control vector and the facts that selected each control,
   verbatim from the resolver.
6. **Diff** — on a re-run, or where an existing declaration is present, the
   before/after control diff plus the fact-level diff. On a first run, the diff
   is against current behavior, so the user can see what setup changes rather
   than only what it establishes.
7. **Warnings** — detection contradictions, unknown framework names, registry
   unavailability, anything the resolver emitted.
8. **The action line** — accept, edit named fields, or cancel.

Then the flow:

- **Zero `must-ask` facts means zero questions.** The summary is the first
  thing the user sees, and accept is the first thing they do.
- **Any `must-ask` facts are asked before the summary**, at most one question
  per fact, phrased in the repository's terms, each stating what the skill
  already found and what it could not distinguish. The answers feed the
  candidate, and the summary that follows is the same summary.
- **Accept is one action** and writes the whole normalized declaration.
- **Edit is per field.** Editing a field re-runs the resolver and re-renders
  the summary, because the control vector may have changed. The user never
  accepts a control vector computed from different facts than the ones being
  written.
- **Cancel writes nothing.**
- **Non-interactive invocation may compute and print the summary; it may never
  accept on the user's behalf.** There is no `--yes`.

## The threshold for asking

A field is asked about only when it is **material** and **locally
undetermined**, as 0009 defines both: material means at least one control
differs between its candidate values under the current policy; locally
undetermined means no permitted local evidence distinguishes them. A field that
is one but not the other is never asked about.

Restated as the four cases the skill actually faces:

- **Evidenced and material** — not asked. The evidence answers it, and the
  answer is shown with its path in the summary where it can be challenged.
- **Evidenced and immaterial** — not asked, not dwelt on.
- **Unevidenced and immaterial** — not asked. It is defaulted conservatively
  and listed as an assumption.
- **Unevidenced and material** — asked. This is the only case.

**Why an evidenced fact is never asked about.** Asking about something the
repository already shows converts setup into a questionnaire, which is the
failure mode this design exists to avoid, and it does so while making the
result _worse_, not merely slower. A question invites the answer the person
prefers rather than the one the repository supports, and the preferred answer
is reliably the cheaper one. Evidence is also attributable and challengeable:
a confident value carries the path that produced it, so a reviewer can check
it and a future contributor can find out why it says what it says. An answer
typed in response to a prompt carries none of that. So the interaction shows
evidenced facts for confirmation in the summary — where they are visible,
cited, and correctable — and reserves questions for the residue that genuinely
cannot be settled locally: whether real regulated data reaches a supported
deployment, whether ingress defined elsewhere is public, who consumes a
published artifact, whether worst-case effects settle externally, and whether
the repository is claimed within an audit scope.

A question budget follows from the same reasoning: if a conventional
repository with ordinary local evidence produces more than a couple of
questions, the detectors are weak or the materiality test is being applied too
loosely. Many questions is a signal to fix, not a setup to ship.

## The four non-simple flows

### `unknown`

`unknown` is a valid, final answer and a valid thing to accept. The skill
offers it explicitly wherever it asks a question, it never writes it merely to
make a file validate, and it never silently converts it to `[]`.

Its effect is the one 0009 fixes: it raises only the controls that read that
fact, each resolving as if the fact held its most conservative value. The
summary says so on the affected line, using the resolver's own attribution.

`unknown` is preferable to a manufactured certainty, and the skill says as
much when a user hesitates: an honest `unknown` produces a conservative
resolution that can be tightened later with evidence, while a guessed value
produces a wrong resolution that nobody will revisit. Not a build failure.

### An existing declaration

A re-run on a declared repository **reports and stops**. It recomputes the
candidate, renders the fact-level and control-level diff against what is
declared, and takes no write action without explicit confirmation of that
specific diff. Accepting the summary is not confirmation to overwrite; the
confirmation is a separate action naming the fields that change.

Fields where the existing declaration and the new candidate agree are shown as
unchanged and are not re-asked. Detection alone never proposes a change to a
declared field — it can only warn. Not a build failure.

### A detected contradiction

Detection that contradicts the declaration produces a **warning** carrying the
evidence path and the two values, inside setup and in the resolver's warning
block on every subsequent resolution. It never fails a build on its own, for
the reason 0009 gives: a screening heuristic that blocks merges is a heuristic
that gets switched off, and the incentive it creates is to weaken the detector
rather than to fix the declaration.

The skill offers to reconcile — by amending the declaration, by recording that
the detection is wrong for this repository, or by leaving the warning standing
— and does none of it silently. Warning, not a build failure.

### A registry conflict

Where an organization registry asserts facts, the possibilities separate
cleanly:

- **Registry raises a declared fact** — not a conflict. The declaration is
  written as the user accepted it, and the resolver applies the assertion in
  the raising direction, attributed on its own line. The summary shows the
  resolved value alongside the declared one so the user is not surprised later.
- **Registry asserts a fact the declaration contradicts downward** — the
  declaration cannot lower it and the summary says so plainly, showing both
  values. The user may still write the declaration; it simply will not take
  effect in that direction. This is not a setup error.
- **Registry locks a control** — the lock is shown in the control vector as
  locked, and no fact the user chooses will unlock it.
- **Registry is configured but unavailable, malformed, or unauthorized** —
  **fails closed**, per the confirmed position. Setup stops and writes nothing,
  because a declaration accepted against an unverified control vector is a
  declaration accepted against nothing. This remains a CI failure at resolution
  time; setup must not be a route around it.
- **No registry configured** — local facts resolve with a loud, recorded
  warning, and setup completes.

The dividing line, stated once: **advisory signals warn; deterministic
conflicts fail.** Detection is advisory, so it always warns. Schema version,
policy version, and registry availability are deterministic, so they fail
closed in CI and setup never papers over them.

## What is written

Acceptance writes, through the helper that owns the file, into `assurance:` in
`.activeloom-config.yml`:

- `schema_version` — the integer schema version this declaration is written
  against.
- `facts` — all seven domains, every one present, normalized and fully
  expanded: set fields as sorted lists or the scalar `unknown`, scalar fields
  as one named value or `unknown`. No field omitted, because within a present
  `assurance:` block a missing field means `unknown` and that is a different
  claim from `[]`.
- `frameworks` — the list, possibly empty, carried verbatim. Audit metadata,
  never a resolver input.

Nothing else. Specifically **not** written:

- **No preset name.** Presets are setup templates, expanded at write time.
  Runtime policy never reads one.
- **No narrative rationale**, organizational or compliance. The rationale goes
  in the pull request, where it is reviewed, not in the file, where it would
  become an unversioned second policy that drifts from the facts beside it.
- **No detected values as declared values** without the user accepting them.
- **No control values, policy version, or resolver output.** Those are derived,
  and a stale copy of a derived value in the declaration is a trap.
- **No evidence paths, file excerpts, or data values.**
- **No timestamps, actor names, or machine identifiers.**

The write touches only the `assurance:` block; the rest of the file is
preserved, comments and all.

### The copy-ready pull request output

The skill prints a pull-request body the user can paste without editing. It is
generated text, not a template to fill in, and it contains:

- **Title** — a conventional-commit subject naming the repository declaration.
- **What this declares** — the seven facts and their values, plain-language.
- **Why each material fact says what it says** — the evidence path for each
  confident value, and the stated answer for each asked fact. Paths only.
- **Assumptions** — every `conservative-default` and every retained `unknown`,
  each with what would change it.
- **Effective control diff** — the before/after control vector, verbatim from
  the resolver, with the policy and schema versions named.
- **Open questions** — anything left `unknown` that a reviewer may be able to
  settle, phrased as a question to the reviewer.
- **Warnings** — detection contradictions and registry notes, if any.

The body is subject to the same two handling rules as everything else: paths,
never content; structure, never data. A reviewer without access to the
repository's data must be able to read it.

## Tests that would prove the interaction

Stated as intentions. Implementation belongs to the lane that builds the skill.

**Evidenced facts generate no questions** — the acceptance property, and the
first thing to break.

- A fixture repository with unambiguous local evidence for all seven domains
  produces zero questions and one summary.
- Each domain gets a fixture whose evidence is sufficient on its own; that
  domain never appears in the `must-ask` set.
- A domain that is evidenced but where the evidence is _weak_ by the precedence
  ordering — guidance prose only — is still not asked about when immaterial.

**The materiality test is applied in both directions.**

- A fact with no local evidence whose candidate values select identical
  controls under the current policy is not asked about; it is defaulted and
  listed as an assumption.
- A fact with no local evidence whose candidate values select different
  controls is asked about.
- The same fixture under a policy version where that fact stops being material
  stops producing the question, with no change to the skill.

**The boundary holds.**

- Every control value in the summary and in the generated pull-request body is
  traceable to a resolver call; none is produced by the agent.
- A resolver error, an unrecognized schema version, or an unrecognized policy
  version stops setup and writes nothing.
- Detected facts never reach the resolver as inputs.

**Precedence and disagreement.**

- A fixture where a publication manifest and a package name disagree resolves
  to the manifest and lists the disagreement as an assumption.
- A fixture where two sources disagree about a material fact produces a
  question, not a silent conservative pick.
- A fixture where they disagree about an immaterial fact produces neither.

**What is written.**

- An accepted declaration round-trips: writing then re-reading yields identical
  normalized facts, and re-running produces an empty diff and no questions.
- All seven domains are present in the written block; none is omitted.
- No preset name, narrative rationale, control value, policy version, evidence
  path, or timestamp appears in the written file.
- `unknown` survives a write and a re-read as `unknown`, never as `[]`.
- Unrelated content and comments in the configuration file are preserved.

**The non-simple flows.**

- A re-run on a declared repository reports a diff and writes nothing without
  a separate confirmation naming the changed fields.
- A detection contradicting a declaration warns and does not fail.
- A configured registry that is unavailable, malformed, or unauthorized stops
  setup with nothing written.
- A registry raising a declared fact is shown as raised and attributed, and the
  written declaration keeps what the user accepted.

**Data handling.**

- No fixture data value, file excerpt, or secret-shaped string appears in the
  summary, the written file, or the pull-request body, for any fixture —
  including fixtures deliberately seeded with realistic-looking values.

## Consequences

Setup on a conventional repository is: run the skill, read one summary, accept.
The cost of the design falls on the detectors and on the materiality test,
which is where it belongs — every question the skill asks is a detector that
could have been better or a policy that could have been clearer.

The boundary between recommendation and resolution is what makes the result
auditable a year later: the declaration records what someone claimed about the
repository, the policy version records what that claim meant at the time, and
neither is contaminated by the agent's reasoning about the other.

Rejected here, and recorded so they are not re-proposed: merging assurance
setup into `review-setup`; a field-by-field interview over facts the repository
already evidences; an agent-authored explanation of why a control applies;
writing a preset name or narrative rationale into the configuration; letting
detection propose changes to a declared field on its own; failing a build on a
detection heuristic; a non-interactive flag that accepts on the user's behalf;
and continuing setup when the resolver or a configured registry cannot answer.
