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
That file is the engine-neutral protocol published by the
[`@loomantix/review-ledger`](https://www.npmjs.com/package/@loomantix/review-ledger)
project and vendored verbatim into every engine repository, so all engines read
the same contract. The helper bundle beside it is vendored from that package's
published tarball and pinned by `review-ledger.version` and
`review-ledger.integrity`; CI byte-compares the bundle, not this document, so a
protocol edit must land upstream rather than here. Where the protocol writes
`<ledger-helper>`, this engine's path is:

```text
.codex/skills/critique/scripts/review-ledger.js
```

## Roles, Not Engine Names

The relay is defined over two roles:

- **author** — the engine that wrote the change. Exactly one.
- **reviewer** — an engine that reads the change cold. Zero, one, or more.

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

### Select the local session mode

The local relay has two explicit session modes. A consumer may declare a default;
otherwise ask the user before the first cross-engine transition. Do not switch
modes silently in the middle of a round.

- **Auto mode** runs the complete bounded chain. For a direct interactive
  transition, Codex may start Claude only through
  `.codex/skills/critique/scripts/run-claude-review.sh`. The launcher pins the
  actual CLI argv to literal `--effort low`; callers cannot supply or override
  the effort. Never hand-compose a `claude` command. An automated wrapper may
  instead run its configured Claude hook after its configuration doctor proves
  the same literal low-effort contract. After Claude returns, validate its
  PR-head and ledger evidence and continue the chain until convergence or the
  configured cap.
- **Handoff mode** never starts the other engine. Each nonterminal pass posts an
  authenticated `local-review-handoff:v1` PR comment and returns control to the
  user, who starts the requested reviewer in a new terminal session. Do not
  choose the other engine's model, effort, flags, or runtime settings.

In handoff mode, when the user says `continue review on PR <number>`, `resume
review`, or similar, first load the latest authenticated handoff comment.
Continue only when it names the current engine and its exact head is still the
PR head. If it names the other engine, stop and ask the user to start that
engine in a fresh terminal.

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

   How the next reviewer starts is a mode choice, not a protocol rule. Auto
   mode launches it from the current session through its low-effort launcher
   and continues when it returns. Handoff mode posts a handoff comment and
   stops, so the next reviewer begins in a fresh user-started terminal. Both
   modes carry the same comment/fix/reply/resolve contract, and neither changes
   which commit an attestation names.

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
     A reviewer holding no attestation on this PR runs adversarially on its
     first cold read whatever the round ordinal: the stance tracks how many
     times that reviewer has read the change, not how many rounds elapsed
     before it joined.
     Once every declared reviewer has read the change cold twice, the remaining
     findings are mostly about the review's own artifacts. A convergence round
     runs only the lanes that can find a reason not to deploy, changes the PR
     only for a realistically reachable blocking defect, defers everything else,
     creates an issue only for an urgent high-impact follow-up, and ends as soon
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

The `agent-loop` skill automates a two-engine instance of this relay with a
required non-mutating validation hook plus `review_max_rounds`,
`codex_review_hook`, and `claude_review_hook`. It still encodes a fixed
Codex-then-Claude order and a position-based restart rather than the head rule
above; that is deliberate for now, and the roster and head-exact rules do not
yet reach it. Under contract v3 every hook writes a structured clean,
changed, or blocked result to `$AGENT_LOOP_REVIEW_RESULT_FILE`. The wrapper
validates that result against observed Git state and the v3 ledger, then posts
the canonical pass/completion attestation itself. It opens a draft PR before
review, exports the pinned PR identity to both hooks, checkpoints private atomic
run state, and verifies that each hook leaves local, remote, and PR heads
aligned. Consumer hooks own semantic finding verification, deterministic inline
posting and disposition, and classification; they must fail or return blocked
if a valid finding or undisposed local-review thread remains.

An automated wrapper must make its mode explicit. Contract-v3 auto mode requires
`config_doctor = true` and `claude_effort_policy = low`; the doctor requires
exactly one literal `--effort low` option in the Claude hook before selection or
claim. Handoff mode stops after each nonterminal engine leg and uses the same
PR-comment protocol as an interactive review.

## Cross-Engine Session Handoff

This section is engine-specific and lives here rather than in the vendored
protocol document, which stays byte-identical across every engine.

In handoff mode, never start another reviewer from the current session. At the
end of a pass, post a deterministic PR comment carrying the exact base, current
head, completed engine and round, outcome, next reviewer, and a pasteable
fresh-session prompt:

```bash
python3 .codex/skills/critique/scripts/local-review-handoff.py post-handoff \
  --repo <owner/repo> --pr <number> --head <full-head-sha> \
  --base <full-base-sha> --from-engine <engine> \
  --to-engine <engine> --round <completed-round> \
  --outcome <clean|minor|material|blocked> \
  [--context-file <public-safe-regular-utf8-file>]
```

The helper owns the `local-review-handoff:v1` marker and prompt. It verifies the
PR head before and after posting, verifies the comment read-back, rejects marker
injection from optional context, and makes an identical retry idempotent.

When the user asks to continue or resume a review, load the latest authenticated
handoff before doing any review work:

```bash
python3 .codex/skills/critique/scripts/local-review-handoff.py show-handoff \
  --repo <owner/repo> --pr <number> --engine <engine>
```

The helper considers the latest handoff from the authenticated GitHub actor,
verifies its content digest and exact live PR head, and fails if the comment
targets another engine. Never fall back to an older handoff addressed to the
current engine. The PR ledger, not a prior terminal transcript, supplies the
remaining context.

A handoff records who runs next; it is not evidence of review. Coverage still
comes from attestations naming the exact head, so a handoff neither creates nor
invalidates one.

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

Invocation:

- **Lean** — `reviewit <pr-number>` for the bounded Gemini Flash and Copilot
  loop. It verifies and deduplicates their findings, fixes confirmed issues,
  pushes, replies, and loops within its cap. Run it after
  `refactorpass <pr-number>` and `critique <pr-number>` when the local relay is
  also running.
- **Deep** — `reviewit <pr-number> deep`, with the larger cap and early-exit
  rules. Its final local `deepcritique` receives the same PR number and ledger,
  and skips the refactor pass when this engine's cleanup latch is already spent
  on the PR.

## Review Tier

Choose deep when the change touches auth, crypto, secret handling, schema/data
shape, GitHub Actions, sync tooling, `.codex/skills/**`, a large refactor, an
area with recurring incidents, or customer/tenant-variable behavior. The tier
selects the depth of both the relay's lanes and the hosted lane; the relay's
round cap is step 7's bound, not the tier's.

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

## Pass Telemetry

Every pass records what it cost, as a `local-review-telemetry:v1` marker in its
own PR comment. The record carries token buckets per exact model, classified
line churn, finding dispositions, and the pass identity needed to ask whether
Deep earns its cost. This engine reports its prompt side as one figure with the
cached and cache-write portions inside it, so the helper subtracts them to reach
the disjoint canonical buckets and keeps the reported figure alongside as a
provider bucket — that arithmetic is what makes an `input` count here mean the
same thing it means for another engine.

The record carries no prose, no file paths, no finding titles, and no money:
rates move and a subscription's marginal cost is zero, so a stored dollar figure
is wrong when written and unverifiable later. Counts keep the whole series
re-priceable.

Emission is opt-in while extraction is being proven on one repository. The
snapshot helper reads `LOOM_REVIEW_TELEMETRY` and does nothing unless it is
exactly `on`. It is environment configuration set once, never an interactive
prompt during a pass — a prompt would block an autonomous run.

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

`<usage-helper>` is `.codex/skills/critique/scripts/usage-snapshot.js`, beside
the vendored ledger helper and invoked the same way. It reads this engine's own
session log; the ledger helper never does, and never may.

Write telemetry working files to `$AGENT_LOOP_LOG_DIR/telemetry/` when that
variable is set, or to another owner-only directory outside the Git worktree.

Before the first reviewer, cleanup agent, or classification read — early enough
that the pass's own setup counts as part of its cost:

```bash
node <usage-helper> snapshot --out "<telemetry-dir>/usage-start.json"
```

After the pass has finalized its v3 result, and after any fix commits:

```bash
node <usage-helper> delta \
  --start "<telemetry-dir>/usage-start.json" --out-dir "<telemetry-dir>"
```

`delta` prints `tokenSource`, `engineVersion`, `durationSeconds`, and the paths
it wrote. This engine reports no per-lane attribution, so `lanesFile` is always
null and `lanes` is absent from the record rather than empty. Pass those through verbatim. `tokenSource` is the provenance of the
numbers and must never be upgraded by hand:

- `session-log-delta` — measured, scoped to this pass.
- `unscoped-session` — measured, but a truthful upper bound rather than this
  pass's cost. A standalone pass with no start snapshot lands here.
- `unavailable` — no usable data, and the record carries **no** token buckets.

**A pass with no usable usage data must never emit zero tokens.** A zero makes
the engine look free and skews every average in its favour, and it is the kind
of defect that survives a year because the dashboard still looks plausible.
Aggregation excludes a missing measurement; nothing zero-fills it.

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

Count only threads this pass posted or dispositioned. `chainInducedRegressions`
counts new fingerprints whose diff anchor traces via `git blame` to a commit
recorded as a fix SHA in an earlier disposition **on this PR** — rework the
chain caused itself, which is far more expensive than either a finding or a
clean pass. A `reopen-occurrence` is not one of these: that is the same defect
still present, not a new one the chain introduced.

### Emit the record

```bash
node <ledger-helper> emit-telemetry \
  --repo <owner/repo> --pr <number> --engine codex \
  --base <full-base-sha> --head <full-head-sha> \
  --pass-type <review|refactor> --review-tier <lean|deep> \
  --trigger <autonomous|interactive> --round <n> \
  --stance <adversarial|convergence> --status <clean|changed|blocked|skipped> \
  --token-source <from delta> --engine-version <from delta> \
  --duration-seconds <from delta> \
  --tokens-file <from delta> \
  --findings-file <path>
```

Omit `--tokens-file` when `delta` reported it as null.
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
