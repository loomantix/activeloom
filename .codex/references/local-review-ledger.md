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

## Run the refactor pass once per engine

A cleanup pass earns its cost on the first cold read of a changeset. By the
second round the diff has already been simplified once, and a fresh pass over
the same code mostly re-litigates naming and shape. That churn moves the head,
re-stales the other engine's attestation, and changes nothing that ships.

Each engine gets **one** refactor pass per PR. Before running one, search the PR
for a marker naming this engine:

```text
<!-- local-review-refactor:v1 engine=<codex|claude> head=<sha> outcome=<committed|no-op> -->
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
`local-review-pass:v1` and `local-review-complete:v1` markers already on the PR
that name this engine; this pass is one past that count.

- **Rounds 1–2 — adversarial.** The full stance: assume the diff is guilty, run
  every applicable lane, fix every valid finding.
- **Round 3 and later — convergence.** Both engines have now read the change
  cold twice. What remains is rarely a deeper defect; it is the review's own
  surface. Shift the goal from challenging the change to landing it.

A convergence round:

- runs only the lanes that can find a reason not to deploy — code reviewer,
  silent failure hunter, and the security reviewer when its signal is present.
  Drop type/API design, comment/docs, PR test analysis, and tenant-coupling.
  Those found what they were going to find in rounds 1–2, and they regenerate
  work indefinitely;
- changes the PR only for a **blocking** defect: one that ships wrong behavior,
  loses or corrupts data, opens a security or privacy hole, breaks a public
  contract, or breaks deploy or rollout. Everything else becomes a follow-up
  issue — reply `outcome=deferred` with the issue link and resolve the thread;
- makes the smallest edit that clears the blocker. No refactors, no renames, no
  new abstraction, no test or comment hardening;
- ends the loop as soon as it finds no blocking defect. Post the clean-pass
  attestation and recommend this repository's ship step by name.

This is a disposition rule, not a reporting rule. Lanes still report every
evidence-backed finding they have, with severity attached. The narrowing happens
one level up, where the whole set is visible and the orchestrator decides what
the PR changes versus what a follow-up issue tracks.

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
2. Search all prior local-review threads for that fingerprint. Reply to the
   existing thread when it is the same root cause; do not create a duplicate.
3. Post one inline comment on an exact diff anchor before changing the file.
   Prefer a right-side line. Use a left-side line only when the finding concerns
   deleted code. A finding without a defensible diff anchor is not an inline
   finding; keep it out of the automated fix loop or track a genuinely large
   follow-up separately.
4. Include this machine-readable marker:

   ```text
   <!-- local-review:v1 engine=<codex|claude> round=<n> head=<sha> fingerprint=<stable-id> -->
   ```

5. State severity, review lens, evidence, impact, and the expected correction.
   Keep one root cause per thread.

Post only confirmed findings. Never copy raw model output, hidden reasoning,
logs, credentials, private data, or repository content unrelated to the
finding into the PR.

### Use the deterministic ledger helper

Use `.codex/skills/grill/scripts/review-ledger.py` for every local-review
finding, disposition reply, thread resolution, and pass marker. Do not
hand-compose `gh api` form arguments for these mutations.

The helper verifies the current PR head, builds JSON requests, reads mutations
back, and rejects a line unless it exists in GitHub's actual PR patch. A locally
expanded diff is not proof that GitHub accepts the line. Run `preflight-anchor`
before preparing the mutation. When the exact line is unavailable, choose
another causally defensible changed line or explicitly use `--file-level`;
never silently change the anchor type. Keep a finding out of the automated fix
loop when neither anchor is defensible.

Pass comment bodies through stdin so shell parsing cannot turn body text into
request fields:

```bash
python3 .codex/skills/grill/scripts/review-ledger.py preflight-anchor \
  --repo <owner/repo> --pr <number> --head <full-head-sha> \
  --path <repository-relative-path> --line <right-side-line>

python3 .codex/skills/grill/scripts/review-ledger.py post-finding \
  --repo <owner/repo> --pr <number> --head <full-head-sha> \
  --path <repository-relative-path> --line <right-side-line> \
  --body-file - <<'REVIEW_COMMENT'
<!-- local-review:v1 engine=<codex|claude> round=<n> head=<sha> fingerprint=<stable-id> -->
<severity, lens, evidence, impact, and expected correction>
REVIEW_COMMENT
```

After the fix is pushed, reply through the dedicated review-comment reply
endpoint and resolve only after the verified reply succeeds:

```bash
python3 .codex/skills/grill/scripts/review-ledger.py reply \
  --repo <owner/repo> --pr <number> --head <full-fix-sha> \
  --comment-id <top-level-comment-database-id> --body-file - <<'REVIEW_REPLY'
<!-- local-review-disposition:v1 engine=<codex|claude> round=<n> head=<fix-sha> fingerprint=<stable-id> outcome=fixed -->
<fix and validation evidence>
REVIEW_REPLY

python3 .codex/skills/grill/scripts/review-ledger.py resolve \
  --repo <owner/repo> --pr <number> --head <full-fix-sha> \
  --thread-id <graphql-review-thread-id>
```

Use the helper's `post-pr-comment` subcommand for refactor, clean-pass, and
completion markers. A preflight rejection performs no mutation; correct the
anchor before posting. A GitHub mutation or read-back failure is the existing
stop condition—do not retry it with an improvised API command.

## Fix, reply, and resolve

For each published finding:

1. Apply the correction and run the smallest relevant validation.
2. Commit and push with a normal, non-force push.
3. Reply in the same thread with the fix commit SHA, validation result, and a
   concise rationale. A fixed finding must also carry this marker, using the
   same fingerprint as the finding and the full pushed fix SHA:

   ```text
   <!-- local-review-disposition:v1 engine=<codex|claude> round=<n> head=<fix-sha> fingerprint=<stable-id> outcome=fixed -->
   ```

   For dismissal or tracked deferral, reply with evidence or the issue link and
   use `outcome=dismissed` or `outcome=deferred` with the reviewed head.

4. Resolve the review thread only after the reply is visible on GitHub.

If posting, replying, pushing, or resolving fails, stop. Leave the PR draft and
report the exact unresolved thread; do not silently continue.

## Record clean passes and convergence

A pass with no new confirmed findings must leave a PR review comment containing
the engine, round, exact reviewed head SHA, and `no new material findings`.
Include this machine-readable marker so automation can attest the clean pass:

```text
<!-- local-review-pass:v1 engine=<codex|claude> round=<n> head=<sha> -->
```

An automated runner requires that attestation from every pass that committed
nothing: a hook exiting successfully proves only that it ran, not that it read
anything. Clean evidence becomes stale as soon as the PR head changes, so the
marker's `head` must be the exact SHA reviewed, and a pass that fixed something
attests through its thread replies instead.

A review hook that committed must also leave a final-lane completion marker
after its last adversarial lane finishes:

```text
<!-- local-review-complete:v1 engine=<codex|claude> round=<n> before=<reviewed-sha> head=<final-sha> -->
```

The runner requires both this completion marker and a same-round finding plus
`outcome=fixed` disposition tied to the pushed SHA. This prevents an earlier
cleanup commit from masking a final adversarial lane that silently declined.

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
