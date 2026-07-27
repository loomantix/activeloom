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

## Fix, reply, and resolve

For each published finding:

1. Apply the correction.
2. Run the smallest relevant validation.
3. Commit and push with a normal, non-force push.
4. Reply in the same thread with the fix commit SHA, validation result, and a
   concise rationale. For a dismissal or tracked architectural deferral, reply
   with the evidence or issue link instead.
5. Resolve the review thread only after the reply is visible on GitHub.

If posting, replying, pushing, or resolving fails, stop. Leave the PR draft and
report the exact unresolved thread; do not silently continue.

## Record clean passes and convergence

A pass with no new confirmed findings must leave a PR review comment containing
the engine, round, exact reviewed head SHA, and `no new material findings`.
Clean evidence becomes stale as soon as the PR head changes.

For a two-engine loop:

- run one fresh Codex pass and one fresh Claude pass per round;
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
