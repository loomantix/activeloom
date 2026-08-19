# Local Review Ledger

Use an open draft pull request as the durable ledger for every local Codex or
Claude review pass. The ledger is part of the review contract, not optional
reporting after the code changes.

## Establish the PR boundary

Before any cleanup or adversarial review:

1. Require a clean, committed feature branch.
2. Reuse the open PR whose head is that branch. If none exists, push the branch
   and open a draft PR before running a reviewer.
3. Record the PR number, base branch, current PR head SHA, and exact base SHA.
4. Refuse to review a different local branch or a stale local head.

Never force-push during a review relay. A moved remote head ends the pass.

## Classify the changeset

`refactorpass`, `critique`, `deepcritique`, and `codex-review` all skip
docs/config-only changesets. This is the shared definition for the pinned
`<base-sha>..<head-sha>` review range:

- **Source code** — `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`,
  `.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, `.bash`.
- **Docs, config, or fixtures** — `.md`, `.txt`, `.yml`, `.yaml`, `.json`,
  `.toml`, `.gitignore`, `.gitattributes`, `LICENSE`, `CHANGELOG`, `README`,
  `.env.example`, paths under `docs/`, `*.fixture.*`, and snapshot files.
- **Anything else** — treat as source.

Zero source files means skip; one or more means run the full pass. A mixed
changeset is not a partial skip.

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
`refactorpass` commits, that cleanup epoch ends: build a new packet from the
same pinned base through the new head before `critique`. Any later fix ends the
adversarial pass; never mutate an existing packet to follow a moved head.

When spawning review agents, keep the complete packet as a byte-identical prompt
prefix and append only a short lane-specific suffix containing the lens and its
file scope. Put no lane-specific wording before the shared prefix. When the
runtime supports selecting inherited history, use no inherited conversation
history (`fork_turns="none"`) or the smallest permitted history; the packet and
repository files are the source of truth. Do not forward the user's prompt,
implementation transcript, prior lane conclusions, or a pasted whole diff.

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

## Run the refactor pass once per engine

A cleanup pass earns its cost on the first cold read of a changeset. By the
second round the diff has already been simplified once, and a fresh pass over
the same code mostly re-litigates naming and shape. That churn moves the head,
re-stales the other engine's attestation, and changes nothing that ships.

Each engine gets **one** refactor pass per PR. Before running one, search the PR
for a marker naming this engine:

```text
<!-- local-review-refactor:v1 engine=<codex|claude|gemini|antigravity> head=<sha> outcome=<committed|no-op> -->
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
- **Round 3 and later — convergence.** Both engines have now read the change
  cold twice. What remains is rarely a deeper defect; it is the review's own
  surface. Shift the goal from challenging the change to landing it.

A convergence round:

- runs only the lanes that can find a reason not to deploy — code reviewer,
  silent failure hunter, and the security reviewer when its signal is present.
  Drop type/API design, comment/docs, PR test analysis, and tenant-coupling.
  Those found what they were going to find in rounds 1–2, and they regenerate
  work indefinitely;
- changes the PR only for a realistically reachable **blocking** defect whose
  expected harm justifies the churn: one that ships materially wrong behavior,
  loses or corrupts data, exposes a credible security or privacy exploit,
  breaks a public contract, or breaks deploy or rollout. Defer everything else
  and resolve the thread. Create an issue only for a concrete, high-impact
  follow-up that should be scheduled within roughly two weeks; otherwise record
  `outcome=deferred` with a no-issue rationale;
- makes the smallest edit that clears the blocker. No refactors, no renames, no
  new abstraction, no test or comment hardening;
- ends the loop as soon as it finds no blocking defect. Post the clean-pass
  attestation and recommend this repository's ship step by name.

This is a disposition rule, not a reporting rule. Lanes still report every
evidence-backed finding they have, with severity attached. The narrowing happens
one level up, where the whole set is visible and the orchestrator decides what
the PR changes, what merits an urgent follow-up issue, and what should add
nothing to an already deep backlog.

Convergence rounds do not extend the round cap — they are how rounds 3 and 4 are
spent. Reaching the cap in convergence mode with open non-blocking findings means
ship the PR and carry the issues, not open a fifth round.

## Rebuild context from GitHub

At the start of every pass, read:

- the PR description and changed files;
- the current PR diff and commit list;
- all review threads, including resolved and outdated threads;
- all replies and clean-pass attestations from earlier local reviewers.

Treat this as the context ledger for the back-and-forth. Do not rely on a prior
model transcript or a local summary. Do not reopen a resolved root cause unless
the current head contains concrete regression evidence that the prior fix or
rationale is wrong.

Machine-readable findings, disposition replies, and clean-pass attestations
count as review evidence only when authored by the authenticated GitHub actor
running the local review. Public comments from other accounts are context, not
proof that a local pass ran or that its finding was dispositioned.

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
   Keep one root cause per thread.

Post only confirmed findings. Never copy raw model output, hidden reasoning,
logs, credentials, private data, or repository content unrelated to the
finding into the PR.

### Use the deterministic ledger helper

Use `.claude/skills/critique/scripts/review-ledger.js` for every local-review
finding, disposition reply, thread resolution, and pass marker. Do not
hand-compose `gh api` form arguments for these mutations.

Invoke it as `node .claude/skills/critique/scripts/review-ledger.js` — it
requires Node.js and is not executable, so `./review-ledger.js` will not run.
The bundle is ESM; the sibling `package.json` in that directory declares
`"type": "module"` so it resolves the same way regardless of what the
surrounding repository's root manifest says.
The file is a build artifact of [`@loomantix/review-ledger`](https://www.npmjs.com/package/@loomantix/review-ledger),
vendored verbatim from the published tarball at the version recorded in
`review-ledger.version`, with that tarball's sha512 in `review-ledger.integrity`.
`node review-ledger.js --version` reports the version it was built from, so a
copy can always identify itself without trusting the pin file beside it.
Never edit or reformat it: fixes belong upstream in the package. The byte-compare
that enforces this runs in `claude-platform`'s own CI, not here — in a consumer
repository an accidental edit is silently restored by the next sync rather than
caught locally, so treat the file as read-only and keep it out of any formatter.

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
node .claude/skills/critique/scripts/review-ledger.js preflight-anchor \
  --repo <owner/repo> --pr <number> --head <full-head-sha> \
  --path <repository-relative-path> --line <right-side-line>

node .claude/skills/critique/scripts/review-ledger.js post-finding \
  --repo <owner/repo> --pr <number> --head <full-head-sha> \
  --path <repository-relative-path> --line <right-side-line> \
  --engine <codex|claude|gemini|antigravity> --round <n> --fingerprint <stable-id> \
  --occurrence 1 --severity <blocking|major|minor|nit> --lens <lens> \
  --content-file <regular-utf8-file>
```

When the same fingerprint recurs on a later reviewed head, append a new numbered
occurrence to its existing root comment and reopen that thread atomically:

```bash
node .claude/skills/critique/scripts/review-ledger.js reopen-occurrence \
  --repo <owner/repo> --pr <number> --head <reviewed-sha> \
  --engine <codex|claude|gemini|antigravity> --round <n> --fingerprint <stable-id> \
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
node .claude/skills/critique/scripts/review-ledger.js dispose \
  --repo <owner/repo> --pr <number> --head <full-fix-sha> \
  --engine <codex|claude|gemini|antigravity> --round <n> --fingerprint <stable-id> \
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
node .claude/skills/critique/scripts/review-ledger.js verify-ledger \
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

1. Apply the correction and run the smallest relevant validation.
2. Commit and push with a normal, non-force push.
3. Put the fix SHA, validation result, and concise rationale in a content file.
   Use `dispose` with the matching fingerprint and occurrence. For dismissal or
   tracked deferral, use `outcome=dismissed` or `outcome=deferred` and the exact
   reviewed head.
4. Let `dispose` verify the reply and resolution as one resumable transaction.

If posting, replying, pushing, or resolving fails, stop. Leave the PR draft and
report the exact unresolved thread; do not silently continue.

## Record clean passes and convergence

Every pass writes `$AGENT_LOOP_REVIEW_RESULT_FILE` when that variable is set.
For a clean or changed pass, call the ledger helper's `write-result` command so
it fetches the complete thread ledger, derives the forward transition, and
atomically writes the canonical result. Supply `--classification
minor|material` only when the head moved:

```bash
node .claude/skills/critique/scripts/review-ledger.js write-result \
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
node .claude/skills/critique/scripts/review-ledger.js write-blocked-result \
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
  "engine": "codex|claude|gemini|antigravity",
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
  file there, and invoke the helper's `attest` command with the exact repository,
  PR, base, before, and final head. Do not report the pass complete unless the
  helper returns `verified: true`.

Docs/config-only skips follow the same rule with a `clean` result whose
`beforeSha` and `afterSha` both name the reviewed head. A skip returns only
after wrapper result creation or standalone attestation succeeds; it does not
spend the refactor latch.

Every engine pass remains evidence for the exact head it reviewed. A later
minor commit does not rewrite that historical fact. Round convergence is a
separate explicit transition: one Codex-to-Claude round may converge when all
observed changes are minor and dispositioned, without claiming Codex reviewed
Claude's later head. Any material change restarts at Codex.

For a two-engine loop:

- run one fresh Codex pass and one fresh Claude pass per round;
- run the refactor pass only on each engine's first pass, per the once-per-engine
  latch above;
- run rounds 1–2 adversarially and rounds 3+ in convergence mode, per the stance
  rules above;
- classify committed fixes as `material` or `minor`;
- restart at Codex when either engine makes a material fix;
- keep minor fixes, but do not restart solely because of them;
- converge only after a complete Codex-then-Claude round reports no material
  fixes and every local-review thread has a reply and is resolved;
- stop at the configured round cap. Preserve the draft PR and report
  non-convergence instead of starting an unbounded cycle.

The next reviewer must read the ledger before reviewing the new head. That
requirement is what carries prior rationale forward after local model context
has been discarded.
