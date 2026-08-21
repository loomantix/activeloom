# Local Review Ledger

Use an open draft pull request as the durable ledger for every local review
pass. The ledger is part of the review contract, not optional reporting after
the code changes.

This document is the engine-neutral protocol. It is published inside
[`@loomantix/review-ledger`](https://www.npmjs.com/package/@loomantix/review-ledger)
and vendored verbatim by each engine's platform repository, so all engines read
the same contract. Anything specific to one engine — its skill names, its
vendored helper path, its lens roster — belongs in that engine's
`REVIEW_WORKFLOW.md`, not here.

Throughout, `<ledger-helper>` stands for this engine's vendored copy of
`review-ledger.js`. Your `REVIEW_WORKFLOW.md` gives the concrete path.

## Roles, not engine names

The protocol has two roles:

- **author** — the engine that wrote the change. Exactly one per pull request.
- **reviewer** — an engine that reads the change cold. Zero, one, or two.

Every ordering, restart, and convergence rule below is written against those
roles. No rule names a specific engine, and adding a fourth engine changes no
rule in this document.

The author engine's own adversarial pass is worth materially less than a
reviewer's: it re-reads the change while still holding the rationale that
produced it, which is the opposite of the cold read the relay exists to obtain.
The author engine may run the cleanup lane, where it is cheapest, but its pass
never counts toward cross-model coverage.

## Declare the review roster

Participation must be **declared**, never inferred. An engine that has not
posted an attestation is indistinguishable from an engine that was never going
to run, so without a declared roster no reader can tell a round that is
incomplete from one that is finished.

Before the first reviewer runs, post the roster:

```bash
node <ledger-helper> post-roster \
  --repo <owner/repo> --pr <number> --head <full-head-sha> \
  --author <engine> --reviewers <engine[,engine]|none> \
  --content-file <regular-utf8-file>
```

It writes one marker to the pull request:

```text
<!-- local-review-roster:v2 author=<engine> reviewers=<engine[,engine]|none> head=<sha> supersedes=<comment-id|none> declaration-sha256=<hash> -->
```

`declaration-sha256` covers the declaration, not just the reason prose beneath
it. Its pre-image is these lines, joined by `\n`, followed by a blank line and
the content:

```text
local-review-roster:v2
author=<engine>
reviewers=<engine[,engine]|none>
head=<sha>
supersedes=<comment-id|none>
```

So `author=`, `reviewers=`, `head=`, and `supersedes=` cannot be edited in place
after the fact. This is the whole point of the field: the roster's justification
must not be left standing over a roster it no longer describes.

`head=` binds the declaration to the commit it was made over. A roster declared
while a branch held only documentation says nothing about the auth code that
lands on it later.

The authenticated relay actor owns the roster chain, and every local engine in
the relay must use that same pinned actor. `read-roster` returns the effective
declaration and the chain that produced it.

### Re-declaring the roster

Rosters supersede rather than replace. Posting a roster over an existing one
writes a new marker naming the previous comment in `supersedes=`; the previous
declaration stays on the pull request. Re-posting a byte-identical declaration
replays instead of appending.

Readers resolve the effective roster the way they resolve the review tier:

1. Read actor-owned issue comments in chronological order. Accept only a comment
   whose marker is one exact line in the grammar above and whose digest
   verifies. A malformed marker, a failed digest, or two roster candidates in
   one comment is a hard stop.
2. The chain must be a single path: exactly one first declaration, no two links
   superseding the same comment, no link superseding a comment that is not on
   the pull request, and every roster on the pull request covered by the chain.
   The newest link is the effective roster.

Unlike the tier marker, roster links carry **no ancestry requirement** between
their heads. Recording a transition always beats refusing it: a developer who
decides late that a change no longer needs a second reviewer must be able to
record that in one step, and a rebase must never make that step unreachable.
Hard stops are for genuine ambiguity — a forked or dangling chain, a forged
digest — never for a human changing their mind.

Pull requests carrying the older `local-review-roster:v1` marker still read. v1
put the declaration outside its own hash and named no commit, so it is treated
as **advisory**: its declared reviewers are still held to attesting the current
head, but a v1 solo declaration is not evidence a reader will repeat. Posting a
v2 roster supersedes the v1 comment and migrates the pull request in one step.

### How many reviewers

One non-author reviewer is the recommended floor and covers the great majority
of changes. A second adds real value mainly where a defect is expensive and
hard to see: auth, crypto, secret handling, schema and data-shape work, release
and sync tooling, or a change whose blast radius crosses repositories.

Solo review — `reviewers=none` — is a **legitimate outcome, not a degraded
one**. What it must be is declared, with the reason in the roster's content
file, at the head it applies to. That keeps the choice visible and attributable
on the pull request rather than resting on whoever remembered it. Narrowing to
solo late in a pull request's life is equally legitimate; post the narrowed
roster and it becomes an ordered, visible link in the chain.

## Coverage and completeness

Coverage is computed from actor-owned attestations naming the pull request's
**exact current head**:

```bash
node <ledger-helper> coverage        --repo <owner/repo> --pr <number> --head <full-head-sha>
node <ledger-helper> verify-coverage --repo <owner/repo> --pr <number> --head <full-head-sha>
```

`coverage` reports; `verify-coverage` additionally refuses a ledger that would
assert something untrue about what happened. Both report a tier over distinct
**non-author** engines: `solo` (none), `cross` (one), `full` (two or more).

`verify-coverage` refuses when:

- no roster is declared;
- a declared reviewer has not attested this head;
- a solo relay is declared in the v1 grammar, whose declaration sits outside its
  own hash;
- a solo relay was declared at a commit other than this one;
- a declared solo relay carries no attestation from the author engine.

Missing or stale roster evidence clears in one `post-roster` step at the current
head, available at any point in a pull request's life. Missing review evidence
clears when the named engine posts its attestation, or when the developer
deliberately re-declares the roster to reflect the review they judge sufficient.

**Neither command is a merge gate.** The ledger records evidence; it holds no
authority over whether a change ships. Do not wire `verify-coverage` into branch
protection, a required check, or anything that reads as one. A developer who has
looked at a change and judged the review it has had to be enough merges it, and
no roster state may make that impossible or require a workaround to reach.

### The head is the invalidation rule

An engine's attestation is evidence for the exact commit it names, and for no
other. That single fact replaces every position-based restart rule:

- an engine whose newest attestation names an **earlier** commit has not
  reviewed what the pull request currently contains, and is missing from the
  round;
- an engine whose attestation names the **current** commit has, regardless of
  what moved the head, which engine moved it, or how many rounds preceded it.

So a material fix does not "restart at the first engine". It moves the head,
which invalidates exactly those attestations that named the old head — no more.
An engine that already attested the post-fix commit stays valid and does not
re-run. This is what keeps a three-engine relay from costing appreciably more
than a two-engine one; a whole-round restart would spend passes re-confirming a
commit that had already been read cold.

A round is complete when every declared reviewer holds an attestation at the
current head. Convergence additionally requires that the round produced no
material fix and that every local-review thread carries a disposition reply and
is resolved.

## Establish the PR boundary

Before any cleanup or adversarial review:

1. Require a clean, committed feature branch.
2. Reuse the open PR whose head is that branch. If none exists, push the branch
   and open a draft PR before running a reviewer.
3. Record the PR number, base branch, current PR head SHA, and exact base SHA.
4. Refuse to review a different local branch or a stale local head.

Never force-push during a review relay. A moved remote head ends the pass.

## Classify the changeset

Every cleanup and adversarial lane skips docs/config-only changesets. This is
the shared definition for the pinned `<base-sha>..<head-sha>` review range:

- **Source code** — `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`,
  `.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, `.bash`.
- **Prompt surface — source, whatever the extension.** Every path under the
  engine's prompt directory, including its Markdown. These files are read by the
  model as instructions and sync to every consumer, so a defect in them ships
  exactly like a code defect. Classifying them as docs would make the review-tier
  triggers unreachable for the surface they were written to protect.
- **Docs, inert config, or fixtures** — `.md`, `.txt`, `.gitignore`,
  `.gitattributes`, `LICENSE`, `CHANGELOG`, `README`,
  `.env.example`, paths under `docs/`, `*.fixture.*`, and snapshot files.
- **Review-significant config** — workflows, dependency manifests and lockfiles,
  schemas, migrations, deploy configuration, and sync targets are source even
  when their extension is `.yml`, `.yaml`, `.json`, or `.toml`.
- **Anything else** — treat as source.

Zero source files means skip; one or more means run the full pass. A mixed
changeset is not a partial skip.

## Rate every finding on one severity ladder

Every local-review finding carries one of four severities. They are the enum the
ledger helper accepts, and they mean the same thing in every lens, engine, and
skill. Rate by **blast radius** — what breaks, and for whom — not by how
important the finding feels or how hard it was to find.

- **`blocking`** — ships materially wrong behavior, loses or corrupts data,
  exposes a credible security or privacy exploit, breaks a public contract, or
  breaks deploy or rollout.
- **`major`** — a real defect in behavior a user or operator can reach, but not
  blocking.
- **`minor`** — correct-but-improvable, or a defect confined to a non-executing
  surface (comments, docs, naming, test clarity) with no behavioral consequence.
- **`nit`** — style or preference. No defect.

**A factually wrong comment is `minor` by default.** It changes no behavior, so
its blast radius is a reader, not a run — and that holds even when the wrongness
matters a great deal. There is no rung meaning "important but non-behavioral";
`minor` is that rung. The same goes for a stale doc, a misleading name, and a
test whose assertion is weaker than its name claims.

Rating a comment defect `major` because it is embarrassing, or a nit `major`
because the file is important, destroys the ladder's one useful property: that
`major` and above mean a user or operator is exposed.

### When a non-executing surface escalates

A comment or docs finding is `major` only when the statement is wrong enough to
**cause questions about the actual implementation** — a reader acting on it
would reach a wrong conclusion about what the code does, in a way that would
change an engineering decision. Merely imprecise, stale, or overclaimed is
`minor`.

A test finding is `major` only when correcting the test makes it **fail**, and
making it pass again requires an app-code change. The app-code defect the test
was hiding is what earns the rung; the test edit alone never does. A test that is
ineffective but whose repair still passes is `minor`.

Neither bar is a reporting bar. Report the finding either way with its severity
attached — see [`../MODEL_NOTES.md`](../MODEL_NOTES.md) §1.

### Severity is not classification

Severity describes a **finding**: how far the defect reaches. Classification
(`minor` / `material`, below) describes the **change the pass made**: whether
behavior moved. They are independent axes, and the two words colliding on
`minor` is a naming accident, not a mapping.

A `major` finding whose fix edited only comments, only docs, or only tests
classifies `minor` — nothing that executes changed. A `nit` whose fix altered a
conditional classifies `material`. Read the diff, not the label on the thread.
Never restate a severity to reach a classification, or pick a classification to
match a severity.

## Build one immutable review packet

Review fan-out must use one canonical description of the changeset. Without an
explicit contract, each lane tends to rebuild the PR context, ingest the whole
diff, and inherit unrelated implementation conversation. That duplicates the
largest inputs to the pass and reduces the useful context available for review.

Resolve these values once, before invoking any cleanup or adversarial lane:

```bash
git diff --name-only <base-sha>..<head-sha>
git diff --stat <base-sha>..<head-sha>
```

Create one immutable review packet containing, in this order:

1. canonical repository identity and PR number;
2. exact base SHA, reviewed head SHA, and literal review range;
3. resolved round and stance;
4. changed-file list and diff stat, copied exactly from the commands above;
5. repository and path-specific instruction files the lanes must read; and
6. the output contract: actionable findings only, with severity and `file:line`
   evidence, or `NO FINDINGS`.

Use this canonical prefix shape so prompt wording as well as data stays stable:

```text
REVIEW_PACKET_V1
Repository: <owner/repo>
PR: <number>
Base: <full-base-sha>
Head: <full-head-sha>
Range: <full-base-sha>..<full-head-sha>
Round: <number> (<adversarial|convergence>)
Changed files (<count>):
<verbatim name-only output>
Diff stat:
<verbatim stat output>
Instructions:
<ordered repository-relative instruction paths>
Output: findings only, each with severity and file:line evidence; NO FINDINGS if clean; maximum 1000 words, compress but do not omit material findings
END_REVIEW_PACKET_V1
```

Reuse the packet unchanged within one cleanup or adversarial packet epoch. If
the cleanup lane commits, that epoch ends: build a new packet from the same
pinned base through the new head before the adversarial lanes run. Any later fix
ends the adversarial pass; never mutate an existing packet to follow a moved
head.

When spawning review agents, keep the complete packet as a byte-identical prompt
prefix and append only a short lane-specific suffix containing the lens and its
file scope. Put no lane-specific wording before the shared prefix. When the
runtime supports selecting inherited history, use no inherited conversation
history (`fork_turns="none"`) or the smallest permitted history; the packet and
repository files are the source of truth. Do not forward the user's prompt, implementation
transcript, prior lane conclusions, or a pasted whole diff.

The orchestrator reads the complete PR ledger once. Lanes review independently
from the pinned source and do not each reload every historical thread. The
orchestrator verifies and deduplicates their findings against the ledger after
all lanes return.

## Deliver scoped diff data

The changed-file list belongs in the shared packet, but the full diff does not.
Apply these rules when a lane reads the changeset:

1. **Scope each lane to the files it reviews.** Name exact repository-relative
   paths in the lane-specific suffix. A genuinely cross-file lens gets the
   shared stat and pulls individual paths as needed.
2. **Prefer a scoped command over a stored artifact.** Use
   `git diff <base-sha>..<head-sha> -- <path>`. Do not create one whole-diff
   file and hand it to every lane.
3. **Read a pinned artifact at most once.** If a caller already supplied an
   immutable artifact, state its path and size. Revisit a region with targeted
   search or a bounded read, never another full read.
4. **Bound large reads.** Above roughly 25,000 characters, narrow by path or use
   an explicit offset and limit.
5. **Bound lane output.** Use the packet's 1,000-word default unless the
   orchestrator deliberately sets a different ceiling before fan-out. A lane
   must not narrate its process or repeat the packet. It must still report every
   material finding; compact the evidence instead of silently dropping one.

State the changeset size and lane file count in the lane-specific suffix. These
rules reduce duplicated bytes; they never justify dropping a lens, omitting a
needed file, or weakening verification.

## Record the review tier once per PR

The tier decides which lanes run and how many rounds are owed. Resolve it
before the first reviewer per your `REVIEW_WORKFLOW.md`
and record it on the PR, so a later round in a fresh session reads it instead of
re-deriving it.

```text
<!-- local-review-tier:v1 tier=<lean|deep> trigger=<ids-or-none> head=<sha> -->
```

`trigger=` carries **every** trigger the change matched, as the comma-separated
ordinals from your `REVIEW_WORKFLOW.md` — `trigger=3`,
`trigger=1,3`, or `trigger=none`. Recording only one lets a clean result from
that trigger's lens de-escalate the PR while an unrecorded trigger still stands.

Resolve one effective marker during pre-flight:

1. Read issue comments in chronological order. Accept only a comment authored by
   the authenticated GitHub actor running the local review whose marker is one
   exact line in the grammar above, with a full 40-character `head`. Ignore
   marker-shaped comments from other actors as untrusted context. A malformed
   actor-authored candidate or two candidates in one comment is a hard stop.
2. Treat the latest accepted comment as the effective marker. Each replacement
   head must be a descendant of the previous accepted head and an ancestor of
   the current PR head; conflicting or non-forward history is a hard stop. This
   chronological rule is the append-only supersession chain — never choose the
   first marker returned by an API.
3. When the current head is later than the effective marker's head, inspect the
   forward delta against the tier triggers. Retain the tier when the delta adds
   no unrecorded trigger. If it does, or the human directly requests Deep, post
   a replacement at the current head that preserves the recorded triggers and
   adds every new one before invoking a lane. A head mismatch by itself does not
   reclassify the unchanged range.

If no accepted marker exists, classify and post one before invoking a lane.
Create the marker body in an owner-only regular file and use the ledger helper's
`post-pr-comment --head <current-head> --body-file <file>` legacy-v1 path so the
helper verifies the exact head and reads the comment back. State the effective
tier and triggers in the pass output.

The marker is per-PR, not per-engine and not per-round — every engine resolves
the same transition chain. Post a replacement only for an evidence-backed
escalation or de-escalation, naming the confirmed finding, direct request, or
clean lenses that justified it. A pass that exits on the docs/config-only
classification posts no marker.

## Run the refactor pass once per engine

A refactor pass earns its cost on the first cold read of a changeset. By the
second round the diff has already been simplified once, and a fresh pass over
the same code mostly re-litigates naming and shape. That churn moves the head,
invalidates other engines' attestations, and changes nothing that ships.

Each engine gets **one** refactor pass per PR. Before running one, search the PR
for a marker naming this engine:

```text
<!-- local-review-refactor:v1 engine=<engine> head=<sha> outcome=<committed|no-op> -->
```

If one exists, skip the cleanup lanes, say so in the pass output, and go straight
to the adversarial lanes. If none exists, run the cleanup lanes and post the
marker as an informational PR comment when they finish.

Post the marker only for a pass that actually ran the cleanup lanes. A pass that
exited on the docs/config-only classification has not spent its engine's refactor
pass — leave the marker off so a later round whose changeset does contain source
can still run one.

The marker carries no `round`: it is a per-PR, per-engine latch rather than
per-round evidence, and no automated runner parses it.

## Resolve the round, then pick the stance

Resolve this engine's round number before selecting lanes. Use
`$AGENT_LOOP_REVIEW_ROUND` when the automated runner set it. Otherwise count the
`local-review-pass:v3` and `local-review-complete:v3` markers already on the PR
that name this engine; this pass is one past that count.

- **Rounds 1–2 — adversarial.** The full stance: assume the diff is guilty and
  run every applicable lane. Fix only confirmed findings whose expected user or
  security harm justifies the change's churn and regression risk.
- **Round 3 and later — convergence.** Every declared reviewer has now read the
  change cold twice. What remains is rarely a deeper defect; it is the review's
  own surface. Shift the goal from challenging the change to landing it.

A convergence round:

- runs only the lanes that can find a reason not to deploy — code review, silent
  failure detection, and security when its signal is present. Drop type/API
  design, comment/docs, PR test analysis, and tenant-coupling. Those found what
  they were going to find in rounds 1–2, and they regenerate work indefinitely;
- changes the PR only for a realistically reachable `blocking` defect, as the
  severity ladder above defines it, whose expected harm justifies the churn. A
  finding a comment or test edit could clear was never `blocking`. Defer
  everything else and resolve the thread. Create an issue only for a concrete,
  high-impact follow-up that should be scheduled within roughly two weeks;
  otherwise record `outcome=deferred` with a no-issue rationale;
- makes the smallest edit that clears the blocker. No refactors, no renames, no
  new abstraction, no test or comment hardening;
- ends the loop as soon as it finds no blocking defect. Post the clean-pass
  attestation and recommend this repository's ship step by name.

This is a disposition rule, not a reporting rule. Lanes still report every
evidence-backed finding they have, with severity attached. The narrowing happens
one level up, where the whole set is visible and the orchestrator decides what
the PR changes, what merits an urgent follow-up issue, and what should add
nothing to an already deep backlog.

The stance schedule above is the Deep schedule. At Lean the cap is two rounds:
round 1 is adversarial, and round 2 runs only if round 1 made a material fix, in
convergence mode. At both tiers, stop as soon as a complete round produces no
material fix — the cap is a ceiling, not a target.

Convergence rounds do not extend the round cap — they are how the last rounds are
spent. Reaching the cap in convergence mode with open non-blocking findings means
ship the PR and carry the issues, not open a fifth round.

## Hosted reviewers are a different style, not a different phase

Hosted AI reviewers — a Gemini or Copilot pass that runs on the pull request
itself rather than in a local agent session — are a **different style of
review**, not a competing protocol and not a phase that has to come after
something else. Run one whenever it is useful: before the local relay, between
rounds, or after convergence.

**The local relay is the default path.** Review coverage is expected to come
from declared roster engines reading the change cold, and that is what
`verify-coverage` measures. Hosted reviewers are an extension on top of it, not
a substitute for it.

They are, however, the primary path for a repository whose developers have no
local agent engine available — a consumer of this package with neither a local
CLI nor a declared roster still gets real review from a hosted pass. That case
is why the hosted lane exists and stays supported; it is not the case these
defaults are tuned for.

A hosted pass does not invalidate anything on its own. Only a **commit**
invalidates, and only by the head rule above, which treats a hosted-review fix
exactly like any other fix:

- if the fix is minor, attestations at the old head are stale for the usual
  reason and the affected engines re-run when the relay next needs them;
- if the fix is material, the round has a material transition and does not
  converge, the same as if a local reviewer had made it.

Classify a hosted-review fix by its effect on the code, using the same
material/minor rule as everything else. Do not treat "a hosted reviewer touched
this" as a category of its own, and do not add a hosted pass as a ritual step
that every change must clear.

Hosted reviewers are not roster participants. They post under their own
identities, so their comments are context rather than actor-owned ledger
evidence, and they do not attest. Coverage counts local engines only.

## Rebuild context from GitHub

At the start of every pass, read:

- the PR description and changed files;
- the current PR diff and commit list;
- all review threads, including resolved and outdated threads;
- all replies and clean-pass attestations from earlier reviewers;
- the declared roster.

Treat this as the context ledger for the back-and-forth. Do not rely on a prior
model transcript or a local summary. Do not reopen a resolved root cause unless
the current head contains concrete regression evidence that the prior fix or
rationale is wrong.

Machine-readable findings, disposition replies, and clean-pass attestations
count as review evidence only when authored by the authenticated GitHub actor
running the local review. Public comments from other accounts — including hosted
reviewers — are context, not proof that a pass ran or that its finding was
dispositioned.

## Post before editing

Review lanes may return hypotheses privately. Verify each against the source and
deduplicate by root cause before publishing it. For every confirmed finding:

1. Compute a stable fingerprint from the normalized repository-relative path
   and root-cause description. The fingerprint must not include the line
   number, round, engine, or head SHA.
2. Search all prior local-review threads for that fingerprint. A later concrete
   occurrence of the same root cause is appended to and reopens the existing
   thread with `reopen-occurrence`; do not create a duplicate root thread.
3. Post one inline comment on an exact diff anchor before changing the file.
   Prefer a right-side line. Use a left-side line only when the finding concerns
   deleted code. A finding without a defensible diff anchor is not an inline
   finding; keep it out of the automated fix loop or track a genuinely large
   follow-up separately.
4. Put only the human finding prose in a regular UTF-8 content file. The helper
   owns the v3 marker, its field order, and its content hash.
5. State severity, review lens, evidence, impact, and the expected correction.
   Rate the severity off the ladder above, on blast radius. Keep one root cause
   per thread.

Post only confirmed findings. Never copy raw model output, hidden reasoning,
logs, credentials, private data, or repository content unrelated to the
finding into the PR.

### Use the deterministic ledger helper

Use the vendored `review-ledger.js` for every local-review finding, disposition
reply, thread resolution, roster declaration, and pass marker. The legacy v1
refactor latch is the one explicit marker-construction exception: post it with
the helper's `post-pr-comment` command until that informational latch moves to
the v3 protocol. Do not hand-compose `gh api` form arguments for these
mutations.

Invoke it as `node <ledger-helper>` — it requires Node.js and is not executable,
so `./review-ledger.js` will not run. The bundle is ESM; the sibling
`package.json` beside it declares `"type": "module"` so it resolves the same way
regardless of what the surrounding repository's root manifest says.

The file is a build artifact of
[`@loomantix/review-ledger`](https://www.npmjs.com/package/@loomantix/review-ledger),
vendored verbatim from the published tarball at the version recorded in
`review-ledger.version`, with that tarball's sha512 in
`review-ledger.integrity`. `node <ledger-helper> --version` reports the version
it was built from, so a copy can always identify itself without trusting the pin
file beside it. Never edit or reformat it: fixes belong upstream in the package.
In a consumer repository an accidental edit is silently restored by the next
sync rather than caught locally, so treat the file as read-only and keep it out
of any formatter.

The v3 helper verifies the current PR head before and after each mutation,
constructs markers and JSON itself, reads mutations back, and reconciles retries
by an idempotency key containing engine, round, exact head, fingerprint, and
occurrence. It rejects a line unless it exists in GitHub's actual PR patch. A
locally expanded diff is not proof that GitHub accepts the line. Run
`preflight-anchor` before preparing the mutation. When the exact line is
unavailable, choose another causally defensible changed line or explicitly use
`--file-level`; never silently change the anchor type.

Create the human prose with the active file-editing tool under
`$AGENT_LOOP_LOG_DIR/ledger-content/` when that variable is set, or in another
owner-only temporary directory outside the Git worktree. Do not use stdin,
heredocs, command substitution, or model-authored marker text. The helper
preserves literal backticks, dollar expressions, quotes, Unicode, CRLF, and a
missing final newline:

```bash
node <ledger-helper> preflight-anchor \
  --repo <owner/repo> --pr <number> --head <full-head-sha> \
  --path <repository-relative-path> --line <right-side-line>

node <ledger-helper> post-finding \
  --repo <owner/repo> --pr <number> --head <full-head-sha> \
  --path <repository-relative-path> --line <right-side-line> \
  --engine <engine> --round <n> --fingerprint <stable-id> \
  --occurrence 1 --severity <blocking|major|minor|nit> --lens <lens> \
  --content-file <regular-utf8-file>
```

When the same fingerprint recurs on a later reviewed head, append a new numbered
occurrence to its existing root comment and reopen that thread atomically:

```bash
node <ledger-helper> reopen-occurrence \
  --repo <owner/repo> --pr <number> --head <reviewed-sha> \
  --engine <engine> --round <n> --fingerprint <stable-id> \
  --occurrence <next-number> --severity <severity> --lens <lens> \
  --comment-id <root-comment-id> --thread-id <graphql-thread-id> \
  --content-file <regular-utf8-file>
```

After the fix is pushed, use the resumable `dispose` transaction. It posts or
reuses the exact disposition, verifies it, resolves the thread, and verifies
the final state. A lost mutation response is reconciled against GitHub within
the same invocation when possible. If verification still fails, running the
identical command again reuses completed work and finishes only the missing
state transition:

```bash
node <ledger-helper> dispose \
  --repo <owner/repo> --pr <number> --head <full-fix-sha> \
  --engine <engine> --round <n> --fingerprint <stable-id> \
  --occurrence <number> --outcome <fixed|dismissed|deferred> \
  --comment-id <root-comment-id> --thread-id <graphql-thread-id> \
  --content-file <regular-utf8-file>
```

Use `reconcile --fingerprint <stable-id>` to inspect known occurrences and
dispositions after an uncertain response. Retry the identical helper command;
never improvise an API mutation. A preflight rejection performs no mutation.

Before a standalone attestation or final readiness decision, verify the complete
actor-owned v3 ledger at the exact head. This rejects unresolved threads,
unstructured replies, cross-occurrence dispositions, and incomplete pagination:

```bash
node <ledger-helper> verify-ledger \
  --repo <owner/repo> --pr <number> --head <full-head-sha>
```

### Blocking outcomes

A `blocking` finding may not end `deferred`. Both `fixed` and `dismissed` are
valid terminal outcomes — a blocker judged on inspection not to be a defect is
dismissed, and that dismissal is attestable. Only deferral, which carries a live
blocker past the review, is refused.

The rule is evaluated on the **latest** occurrence of a fingerprint. Occurrences
are a sequential history of one root cause, so the highest occurrence is its
current state, and a recorded disposition is immutable by design. Evaluating
every occurrence independently would make a blocker that was deferred once and
later fixed permanently unattestable, leaving only two ways out — rewriting
history or forging the marker — which are the two things the ledger exists to
prevent. When a later occurrence clears an earlier blocking deferral, that
recurrence and its fix must also form strict forward Git transitions.

The same-round **result evidence** paths are deliberately stricter: there a
blocking finding must be `fixed`, not merely not-deferred. That rule governs
what a review result may claim as same-round evidence, which is a different
question from whether the ledger is internally consistent. Do not collapse the
two.

## Fix, reply, and resolve

For each published finding:

1. Apply the correction and run the smallest relevant validation. That scoped
   run dispositions the finding; it never substitutes for the gating run below.
2. Commit the correction. When `$AGENT_LOOP_REVIEW_PUSH_HELPER` is set, invoke
   that wrapper-owned helper with no arguments; otherwise push normally without
   force.
3. Put the fix SHA, validation result, and concise rationale in a content file.
   Use `dispose` with the matching fingerprint and occurrence. For dismissal or
   tracked deferral, use `outcome=dismissed` or `outcome=deferred` and the exact
   reviewed head.
4. Let `dispose` verify the reply and resolution as one resumable transaction.

Write the commit message with the file-editing tool into the same owner-only
temporary directory used for ledger content, then commit with
`git commit -F <file>`. Run each git command as its own plain command. A
worktree-isolated session refuses a git command carrying a heredoc, redirect, or
`&&` chain because it cannot statically verify that the command stays inside the
worktree, and that refusal aborts the pass mid-fix.

If posting, replying, pushing, or resolving fails, stop. Leave the PR draft and
report the exact unresolved thread; do not silently continue.

## Validate before attesting

A scoped run is the right validation for a _fix_. It is never sufficient
evidence for a _pass_. Before writing any pass or completion attestation, run
the repository's gating suite unfiltered, and state in the attestation which
command and config it ran and at which SHA.

Two failure modes make this non-optional, and both have shipped:

- **A scoped run is blind by construction.** It cannot see a sibling suite the
  change broke, a mirrored spec under a second directory, or a spec the change
  itself added and never executed. An engine that edits `src/**/x.spec.ts` and
  runs only that file will report green while `tests/**/x.spec.ts` — the same
  assertions, a second copy — is red.
- **CI may not be running the suite either.** Whether any test job runs on a
  given pull request is a per-repository, per-target-branch policy. A green
  check list can contain zero test jobs. Never infer test health from check
  status; read which jobs actually ran, or run the suite yourself.

Read the consumer repository's declared review gate — the commands its
`AGENTS.md` (or `CLAUDE.md`) names as the gate — and run those. Where a
repository declares none, run its broadest practical suite and say so. If the
gating run is genuinely impractical in the environment, the attestation must
say that plainly instead of implying coverage it does not have.

A gating run that fails is a blocking finding in its own right, even when the
failure predates the round: an attestation cannot certify a head whose suite is
red. This applies to a `clean` pass too — a round that changed nothing still
attests to a head, and that head's suite can be red for reasons no lane examined.

## Record clean passes and convergence

Every pass writes `$AGENT_LOOP_REVIEW_RESULT_FILE` when that variable is set.
For a clean or changed pass, call the ledger helper's `write-result` command so
it fetches the complete thread ledger, derives the forward transition, and
atomically writes the canonical result. Supply `--classification
minor|material` only when the head moved.

Derive that classification from the diff this pass produced, never from the
severities on the threads it dispositioned — see "Severity is not
classification" above. A pass whose commits changed no executing line is
`minor` even when a thread it closed was posted `blocking` or `major`:

```bash
node <ledger-helper> write-result \
  --repo "$GH_REPO" --pr "$AGENT_LOOP_PR_NUMBER" \
  --head "$(git rev-parse HEAD)" --engine "$AGENT_LOOP_REVIEW_ENGINE" \
  --round "$AGENT_LOOP_REVIEW_ROUND" --base "$AGENT_LOOP_REVIEW_BASE_SHA" \
  --before "$AGENT_LOOP_PR_HEAD_SHA" \
  --result-file "$AGENT_LOOP_REVIEW_RESULT_FILE"
```

The wrapper snapshots and seals the pre-pass review-comment IDs and exports the
owner-only file to the helper. A pseudo-v3 marker absent from that snapshot is
current-pass data and fails closed instead of becoming historical evidence.

For a blocked pass, put one short public-safe blocker in an owner-only regular
file and call `write-blocked-result` instead of constructing JSON:

```bash
node <ledger-helper> write-blocked-result \
  --head "$(git rev-parse HEAD)" --engine "$AGENT_LOOP_REVIEW_ENGINE" \
  --round "$AGENT_LOOP_REVIEW_ROUND" --base "$AGENT_LOOP_REVIEW_BASE_SHA" \
  --before "$AGENT_LOOP_PR_HEAD_SHA" \
  --result-file "$AGENT_LOOP_REVIEW_RESULT_FILE" \
  --blocker-file "$AGENT_LOOP_LOG_DIR/blocked-review.txt"
```

The file is always present, including clean and blocked passes, and contains
exactly this contract:

```json
{
  "version": 3,
  "status": "clean|changed|blocked",
  "engine": "<engine>",
  "round": 1,
  "baseSha": "<sha>",
  "beforeSha": "<sha>",
  "afterSha": "<sha>",
  "classification": null,
  "findingFingerprints": [],
  "finalLaneComplete": true
}
```

For `changed`, classification is `minor` or `material`, fingerprints is the
complete same-round disposition set, at least one disposition is `fixed`, and
`finalLaneComplete` is true. For `blocked`, classification is null,
`finalLaneComplete` is false, and the object also contains a short safe
`blocker` string.

The deterministic helper validates this file against the observed before/after
Git state, verifies that its fingerprint set exactly matches the actor-owned
fixed findings, and requires structured dispositions on every actor-owned v3
thread before it posts the canonical `local-review-pass:v3` or
`local-review-complete:v3` attestation. The automated wrapper invokes that
attestation after its draft-PR boundary checks; standalone reviewers receive the
same evidence validation from the helper. Review hooks never post their own
pass/completion markers. A missing or invalid result and a valid `blocked`
result both stop clearly even when the reviewer process exits zero.

### Finalize wrapper and standalone results

Result ownership depends on the caller:

- When `$AGENT_LOOP_REVIEW_RESULT_FILE` is set, serialize the exact result to
  that regular file and return without posting a pass/completion marker. The
  wrapper validates the file and owns attestation.
- When it is unset, the reviewer is standalone. Create an owner-only temporary
  directory outside the Git worktree, serialize the same result to a regular
  file there with `write-result`, then invoke `attest` with the exact repository,
  PR, base, before, and final head. Pass the digest returned by `validate-result`
  as `--expected-result-sha256`. Do not report the pass complete unless the
  helper returns `verified: true`.

**A standalone pass always has a reachable attestation.** The snapshot flags are
optional inputs, not preconditions, and a pass that did not capture one still
attests:

- `--threads-file` / `--expected-threads-sha256` seal a review-thread export so a
  wrapper's evidence cannot shift mid-pass. Omit both and the helper fetches the
  threads live from GitHub, which is the normal standalone path. The sealing rule
  — the helper refuses a `--threads-file` whose digest is not supplied as a
  64-hex value, via that flag or the `AGENT_LOOP_REVIEW_THREADS_SHA256`
  environment fallback — governs an export you chose to pass, not one you owe.
- `--historical-comment-ids-file` is the one input that is genuinely
  order-sensitive: it names the v3 comment IDs that already existed **before**
  this pass posted anything, so the helper can tell historical evidence from
  current-pass data. Omit it and every v3 record on the PR is treated as
  current-pass. For a pass whose only v3 threads are its own — a first round, or
  any round that inherited none — that is exactly right. Where earlier rounds or
  another engine left v3 threads behind, capture the snapshot in pre-flight;
  after the pass has posted its own findings it can no longer be reconstructed.
- `--allowed-heads-file` widens the accepted before/head transition set. A pass
  whose before and head are the two SHAs it actually reviewed does not need it.

Never substitute prose for the marker. A write-up that names
`local-review-pass:v3` or `local-review-complete:v3` in its text is not an
attestation: `verify-coverage` does not match it, so the round reads as one the
reviewer never ran, while a human reading the PR sees a finished review. If the
helper genuinely refuses, that refusal is the finding — finalize `blocked` with
the diagnostic and say so, rather than reporting the pass complete without one.

Docs/config-only skips follow the same rule with a `clean` result whose
`beforeSha` and `afterSha` both name the reviewed head. A skip returns only
after wrapper result creation or standalone attestation succeeds; it does not
spend the refactor latch.

## Converge

Run the relay until all of the following hold, then mark the PR ready:

1. `verify-coverage` passes at the exact current head — a roster is declared and
   every declared reviewer holds an attestation naming that head.
2. The round that produced those attestations contained no material fix.
3. `verify-ledger` passes at the same head: every actor-owned thread carries a
   structured disposition and is resolved.

Classify committed fixes as `material` or `minor` by effect, not by path or by
finding severity — read the diff the pass produced, per "Severity is not
classification" above. `material` covers substantive correctness,
security/privacy, data-safety, compatibility, deployment/sync, or
review-integrity changes, including tests or workflows needed to prevent a false
green. `minor` is low-risk non-behavioral cleanup or polish. A material fix
means the round did not converge; a minor fix is kept and does not by itself
prevent convergence.

Each engine pass remains evidence for the exact head it reviewed, and a later
minor commit does not rewrite that historical fact. A round may converge on a
minor A-to-B transition without pretending the earlier reviewer read B; its
exact-head attestation stays historical evidence, and `verify-coverage` is the
authority on what has been read at the head that will actually merge.

**A round that only finds non-material test, fixture, comment, or docs polish is
the signal to ship, not to keep going.** It means the product converged and the
review has turned to auditing its own artifacts. A test or workflow fix needed to
prevent a false green remains material. Defer non-material polish without growing
the backlog, and create a tracking issue only for a concrete, high-impact
follow-up that should be scheduled within roughly two weeks.

Stop at the tier's round cap — four at Deep, two at Lean. Leave the PR draft and
report non-convergence
instead of continuing an unbounded cycle.

The next reviewer must read the ledger before reviewing the new head. That
requirement is what carries prior rationale forward after local model context
has been discarded.
