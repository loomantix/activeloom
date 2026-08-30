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
   revert, or hotfix in roughly the last 90 days. Evidence is a specific defect,
   revert, or hotfix commit you can name; ordinary commit traffic on an actively
   developed path is not evidence, and neither is the path being important.
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
4. Run each declared reviewer against the current head, under the ledger's
   comment/fix/reply/resolve contract. Claude's lane is `critique <pr-number>`
   at Lean and `deepcritique <pr-number>` at Deep; other engines use their own
   equivalents. Reviewer order within a round is a scheduling choice, not a
   protocol rule — what matters is which commit each one read.

   How a non-author reviewer starts is a session choice, not a protocol rule.
   **Session mode** — the user starts each reviewer in a fresh terminal — is
   always available and is the only mode for an engine with no tested launcher.
   **Auto mode** lets the current session start a declared reviewer itself,
   through that engine's checked-in launcher and nothing else. Both modes carry
   the same contract and neither changes which commit an attestation names.

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

### Auto mode

Auto mode is available for the `gemini` reviewer, launched through
[`skills/critique/scripts/run-agy-review.sh`](skills/critique/scripts/run-agy-review.sh).
The launcher pins `gemini-3.7-flash-high`, literal `--effort high`, accept-edits
mode, unattended permissions, and structured JSON output. A pass defaults to a
30-minute bound through `LOCAL_REVIEW_PASS_TIMEOUT_SECONDS`; values above the
hard 3600-second ceiling are rejected. Under agent-loop the wrapper sets that
variable itself, to the smallest of what remains of the run's
`review_timeout_seconds` budget, the configured `hook_timeout_seconds`, and that
same 3600-second ceiling — less a margin, so the reviewer CLI reaches its own
timeout and writes a structured result before the wrapper's bound kills it. On
the shipped defaults the remaining budget is not the binding constraint until
more than half of it is gone.
A caller supplies only the repository, PR, base, head, and round. It
refuses to start unless the current repository, the PR's ownership and head
repository, local HEAD, PR head, and remote head all match the requested exact
head over a clean worktree, and unless the reviewer CLI resolves exactly one
live `deepcritique` skill backed by a clean `loomantix/gemini-platform`
checkout at the launcher's pinned commit that vendors this engine's
`review-ledger` version. Hand-composing the CLI command instead is outside the
tested contract.

Honour the effective roster exactly: run the declared reviewers that lack an
attestation at the current head, and treat `gemini` as a default only when
declaring a new relay. Replacing a reviewer already in flight takes an explicit
superseding declaration.

**A returning launcher is not a completed review.** Accept the round only when
the launcher exits zero — it requires structured `status == SUCCESS` and a
non-blank response — _and_ `verify-coverage` shows that engine's authenticated
attestation at the exact reviewed head. The two disagree in both directions: the
Antigravity CLI reports a turn-level `ERROR` when any single tool call in the
turn failed, so a review that finished and posted its ledger evidence can still
exit nonzero; and a reviewer can narrate completion, or post a comment, without
publishing a result at the head that will merge. Read the ledger rather than the
narration, and re-run the round rather than relaxing the launcher.

The launcher starts a fresh one-shot by omitting every continuation flag. The
Antigravity CLI has no equivalent of Claude's `--no-session-persistence`, so it
still records a local conversation; use session mode where local conversation
persistence is prohibited.

The `agent-loop` wrapper drives its own fixed engine slots and is not part of
this lane.

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

## Pass Telemetry

Every pass records what it cost, as a `local-review-telemetry:v1` marker in its
own PR comment. The record carries token buckets per exact model, classified
line churn, finding dispositions, and the pass identity needed to ask whether
Deep earns its cost. It carries no prose, no file paths, no finding titles, and
no money: rates move and a subscription's marginal cost is zero, so a stored
dollar figure is wrong when written and unverifiable later. Counts keep the
whole series re-priceable.

### Two gates: measuring and publishing

Extraction and emission are separate decisions and have separate gates. Both
are read by the usage helper and nowhere else; both are environment
configuration set once, never an interactive prompt during a pass, because a
prompt would block an autonomous run.

| Variable                        | Governs                             | Default           |
| ------------------------------- | ----------------------------------- | ----------------- |
| `LOOM_REVIEW_TELEMETRY`         | emitting a record to a pull request | off               |
| `LOOM_REVIEW_TELEMETRY_EXTRACT` | measuring this pass at all          | the emission gate |

Each accepts exactly `on` or `off`, ignoring surrounding whitespace and case.
Any other non-empty value is neither: the helper stays disabled and reports an
error, so a typo reads as a misconfiguration rather than as a deliberate
opt-out.

Publication is the part that warrants an opt-in rollout, so it is the part that
keeps the original variable and its original meaning — nothing changes for a
repository that has already set it. Measurement is separable because a local
consumer of usage data has no business publishing anything: set
`LOOM_REVIEW_TELEMETRY_EXTRACT=on` with the emission gate off and the numbers
are available while emission is structurally unreachable rather than merely
unrequested.

The helper reports `enabled` for extraction and `emit` for emission on every
payload, in every mode and on every failure path. **Invoke `emit-telemetry`
only when `emit` is true**, and never derive that from anything else.

### Never read a telemetry marker

A pass must not read prior telemetry: not into a finder prompt, not into a
review packet, not into context assembly, not into a summary of the PR. An
agent that can see its own measured cost and a readable trend has been handed a
target, and the thing it can most easily optimise is the review rather than the
spend. Reporting **this** pass's own numbers at the end, after findings are
posted, is fine — a single figure with no baseline is not a trend.

Filter by the `local-review-telemetry:` prefix rather than by a list of known
markers, so a record type added later is excluded by default instead of leaking
into reviewer context until someone teaches the filter about it.

### Snapshot before, delta after

`<usage-helper>` is `.claude/skills/critique/scripts/usage-snapshot.js`, beside
the vendored ledger helper and invoked the same way. It reads this engine's own
session log; the ledger helper never does, and never may.

Write telemetry working files to `$AGENT_LOOP_LOG_DIR/telemetry/` when that
variable is set, or to another owner-only directory outside the Git worktree.

After the pre-flight has resolved the mandatory repository, PR, base, head,
round, and stance identity, but before it reads review threads, classifies the
changeset, or invokes the first reviewer or cleanup agent:

```bash
node <usage-helper> snapshot --out "<telemetry-dir>/usage-start.json"
```

The skill and identity-resolution pre-flight necessarily precede this boundary
and are not included in the record. A failure before the boundary reports
`telemetry not emitted: boundary unresolved`; it cannot truthfully construct the
PR-bound record that `emit-telemetry` requires. A failure after the snapshot has
all mandatory identity fields and emits `status=blocked`. Compare pass records
only on this shared boundary.

After the pass has finalized its v3 result, and after any fix commits:

```bash
node <usage-helper> delta \
  --start "<telemetry-dir>/usage-start.json" --out-dir "<telemetry-dir>"
```

`delta` prints `enabled`, `emit`, `tokenSource`, `reason`, `engineVersion`,
`durationSeconds`, and the paths it wrote. When `emit` is false, do not invoke
`emit-telemetry` at all. When `emit` is true but `enabled` is false — extraction
switched off on a repository that still publishes — the helper reports
`tokenSource: unavailable` itself and the record carries no usage; pass that
through like any other value rather than repairing the gap by hand. Otherwise
pass non-null values through verbatim, except `reason`: it is diagnostic only,
names why a measurement was downgraded or abandoned, is not an `emit-telemetry`
argument, and never upgrades provenance.
Report it in the pass output so a telemetry outage can be told apart from
telemetry being switched off. A failed `snapshot` reports `scoped: false` and no
`snapshotFile`; treat the start file it did not write as stale and do not pass
it to `delta`. `tokenSource` is the provenance of the numbers and must never be
upgraded by hand:

- `session-log-delta` — measured, scoped to this pass.
- `unscoped-session` — an unattributed session total. It may include or omit
  work relative to this pass and must not be used in pass-cost comparisons. A
  standalone pass with no usable start snapshot lands here.
- `unavailable` — no usable data, and the record carries **no** token buckets.

**A pass with no usable usage data must never emit zero tokens.** A zero makes
the engine look free and skews every average in its favour, and it is the kind
of defect that survives a year because the dashboard still looks plausible.
Aggregation excludes a missing measurement; nothing zero-fills it. The rule is
about absence versus measured zero, so it holds for every count the helper
reports, not only the token buckets, and an `unavailable` record reports no
engine version, duration, or turn count at all — a record that has declared its
own inputs unusable may not go on to quote values drawn from them.

### Identify the prompt stack

A record that says what a pass cost but not what it was running cannot answer
"did that prompt change help" — findings-per-token is a property of the prompt
as much as of the model. `<hash-helper>` is
`.claude/skills/critique/scripts/prompt-stack-hash.js`. Run it once per pass,
any time after the boundary and before emission:

```bash
node <hash-helper> --repo-root "<repository root>"
```

It reads only files already checked into the repository — no session log, no
path under a home directory — so it is ungated and safe to run anywhere. It
always exits 0 and always prints one JSON object carrying `promptStackSha256`,
`repoInstructionsSha256`, and `hashInputVersion`. Pass the two digests through
verbatim and omit the corresponding flag when one is null. Nothing else it
prints is an `emit-telemetry` argument.

**The two digests are never collapsed into one.** The synced stack is
fleet-wide and moves when upstream moves; repo-local instructions are per
repository. A combined digest would make every repository look like a different
prompt generation forever, which destroys the cross-repository correlation the
hash exists to enable.

#### Hash input, version 1

The digest input is a definition, not an implementation detail. Two engines
that hashed the same stack in different orders would mint two identities for
one prompt generation, which reads downstream as a real difference and is worse
than having no hash. Version 1 is:

- **Prompt stack** — an enumerated list of synced review prompt files, not a
  glob: `MODEL_NOTES.md`, `REVIEW_WORKFLOW.md`,
  `references/local-review-ledger.md`, and the `critique`, `deepcritique`,
  `refactorpass`, and `reviewit` skill bodies. Scripts are excluded: the ledger
  bundle and the usage extractor are not prompts, and folding them in would
  move the digest on every ledger release. Finder lenses that live outside the
  synced surface are not covered — a file the helper cannot read in a consumer
  checkout cannot be part of a digest that has to be reproducible there.
- **Repo instructions** — root `AGENTS.md` and root `CLAUDE.md`, both declared
  for both engines so the same repository state yields the same digest whichever
  engine emitted the record. Nested instruction files are out of scope: their
  discovery depends on which directories a pass happened to touch.
- **Order** — paths sorted byte-wise, computed from the set rather than taken
  from the order the list is written in.
- **Normalisation** — a leading UTF-8 BOM is stripped and CRLF and lone CR
  become LF, so a checkout with `core.autocrlf` on holds the same prompt
  generation as one without. Nothing else is normalised: trailing whitespace
  and blank-line changes are real edits to a prompt and must move the digest.
- **Framing** — the digest is taken over per-file digests, each record carrying
  its path, prefixed by a domain string that names the hash-input version and
  which set it is. A rename is a change, a file moved between the two sets
  cannot collide, and no byte can be shifted across a file boundary unnoticed.
- **Absence** — a declared file that is absent is recorded as absent rather than
  skipped, so a consumer that opted out or drifted has a visibly different
  identity. A set with nothing present at all yields null, not the digest of
  "everything absent".
- **Failure** — a declared file that exists and cannot be read yields null for
  that whole digest. Never a partial hash: one covering part of the stack is
  indistinguishable from one covering all of it.

The version is mixed into the digest rather than reported beside it, so a later
redefinition cannot silently rewrite the meaning of records already emitted —
version 2 produces different digests by construction. Changing the file list,
the order, the normalisation, or the framing **is** a redefinition and bumps it.

`hashInputVersion` is not `promptStackVersion`. The latter is the prompt stack's
semantic version, which nothing computes yet; it stays null and is not this
helper's to fill.

### Count the findings

Write this pass's own dispositions to a regular file with the active
file-editing tool — never a heredoc or command substitution:

```json
{
  "posted": 0,
  "bySeverityAndOutcome": {
    "blocking": { "validFixed": 0, "validDeferred": 0, "invalidDismissed": 0 },
    "major": { "validFixed": 0, "validDeferred": 0, "invalidDismissed": 0 },
    "minor": { "validFixed": 0, "validDeferred": 0, "invalidDismissed": 0 },
    "nit": { "validFixed": 0, "validDeferred": 0, "invalidDismissed": 0 }
  },
  "chainInducedRegressions": 0
}
```

For this schema, `posted` is the number of unique finding threads this pass
handled: the union of roots it newly posted and inherited occurrences it
dispositioned. Count each thread once even when both happened in this pass; this
keeps `posted` greater than or equal to the summed outcomes the helper validates.
`chainInducedRegressions`
counts new fingerprints whose diff anchor traces via `git blame` to a commit
recorded as a fix SHA in an earlier disposition **on this PR** — rework the
chain caused itself, which is far more expensive than either a finding or a
clean pass. A `reopen-occurrence` is not one of these: that is the same defect
still present, not a new one the chain introduced.

### Emit the record

```bash
node <ledger-helper> emit-telemetry \
  --repo <owner/repo> --pr <number> --engine claude \
  --base <full-base-sha> --head <full-head-sha> \
  --pass-type <review|refactor> --review-tier <lean|deep> \
  --trigger <autonomous|interactive> --round <n> \
  --stance <adversarial|convergence> --status <clean|changed|blocked|skipped> \
  --token-source <from delta> --engine-version <from delta> \
  --duration-seconds <from delta> \
  --tokens-file <from delta> --lanes-file <from delta> \
  --prompt-stack-sha256 <from hash helper> \
  --repo-instructions-sha256 <from hash helper> \
  --findings-file <path>
```

Omit `--review-tier`, `--engine-version`, `--duration-seconds`, `--tokens-file`,
`--lanes-file`, `--prompt-stack-sha256`, and `--repo-instructions-sha256`
whenever the corresponding value is null. A docs/config-only
skip can legitimately have no resolved review tier, and unavailable usage can
legitimately have no engine version or duration; the omitted options serialize
as null without inventing a value or failing the emission.

`--stance` is the one value on this list's other side: it is mandatory and
cannot be omitted, yet it follows the tier's schedule, and the two schedules
diverge from round 2. A branch that emits before a tier is resolved — a
docs/config skip, or a cleanup lane that resolves no tier at all — must
therefore derive it rather than reaching for the tier it does not have. Read the
effective tier marker already on the PR, without classifying or posting one, and
fall back to `adversarial` when none exists: a PR carrying no tier marker has
had no prior round, and both schedules make round 1 adversarial. Resolve this
before the first branch that can emit, not at the point of emission.
Omit `--changeset-file` and the classifier runs over `<base>..<head>` itself.
Add `--truncated` when a lane silently truncated the diff it was given: a lane
that reviewed less than it was asked to produces cheap, bad findings, which is
the exact pattern that otherwise reads as efficiency.

`trigger` is `autonomous` when a runner set the `AGENT_LOOP_*` variables and
`interactive` otherwise.

### What each pass emits

| Pass                                  | `--pass-type` | `--status` |
| ------------------------------------- | ------------- | ---------- |
| Adversarial pass, nothing to fix      | `review`      | `clean`    |
| Adversarial pass that committed a fix | `review`      | `changed`  |
| Pass that could not complete          | `review`      | `blocked`  |
| Docs/config-only skip                 | `review`      | `skipped`  |
| Cleanup pass that committed           | `refactor`    | `changed`  |
| Cleanup pass that found nothing       | `refactor`    | `clean`    |
| Cleanup skipped on a spent latch      | `refactor`    | `clean`    |
| Cleanup on a docs/config-only skip    | `refactor`    | `skipped`  |
| Cleanup that could not complete       | `refactor`    | `blocked`  |

A skip still burns tokens reading and classifying the PR, and "we spent eight
thousand tokens deciding not to review" is exactly the machinery overhead worth
seeing. `skipped` is reserved for the changeset that had nothing reviewable in
it — the record rejects a `skipped` pass carrying review-significant files, so a
cleanup pass that stopped on a spent latch reports `clean` instead. Its
changeset was reviewable; this engine had simply already spent its one pass.

### Emission failure is never fatal

`emit-telemetry` exits zero either way and prints `emitted` with the reason on
failure. Report that outcome and move on. Never retry it into the review, never
let it change the v3 result, and never let it delay marking the PR ready. This
is why the record is a separate marker in a separate comment rather than an
extension of the attestation, whose body is byte-verified and hash-checked: a
telemetry defect must not fail a review that found real defects.

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
- `skills/critique/scripts/usage-snapshot.js` — this engine's pass-scoped usage
  extractor. It reads the session log, which the vendored ledger helper never
  does; see "Pass Telemetry" above.
- `skills/critique/scripts/prompt-stack-hash.js` — the prompt-generation
  identity for a telemetry record. Reads only checked-in repository files; see
  "Identify the prompt stack" above.
- [`skills/critique/scripts/run-agy-review.sh`](skills/critique/scripts/run-agy-review.sh)
  — the auto-mode launcher for the `gemini` reviewer.
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
