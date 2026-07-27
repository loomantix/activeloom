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

At the start of every pass, read the PR description, changed files, current
diff, commits, and all review threads, including resolved and outdated threads.
Also read replies and clean-pass attestations from earlier local reviewers.

Treat this as the context ledger for the back-and-forth. Do not rely on a prior
model transcript or a local summary. Do not reopen a resolved root cause unless
the current head contains concrete regression evidence that the prior fix or
rationale is wrong.

## Post before editing

Review lanes may return hypotheses privately. Verify each against the source and
deduplicate by root cause before publishing it. For every confirmed finding:

1. Compute a stable fingerprint from the normalized repository-relative path
   and root-cause description. Do not include line, round, engine, or head SHA.
2. Search prior local-review threads for the fingerprint. Reply to the existing
   thread when it is the same root cause; do not create a duplicate.
3. Post one inline comment on an exact diff anchor before changing the file.
   Prefer a right-side line. Use a left-side line only for deleted code.
4. Include this machine-readable marker:

   ```text
   <!-- local-review:v1 engine=<codex|claude> round=<n> head=<sha> fingerprint=<stable-id> -->
   ```

5. State severity, review lens, evidence, impact, and expected correction. Keep
   one root cause per thread.

A finding without a defensible diff anchor stays out of the automated fix loop
or becomes a separately tracked architectural follow-up. Never copy raw model
output, hidden reasoning, logs, credentials, private data, or unrelated source
into the PR.

## Fix, reply, and resolve

For each published finding:

1. Apply the correction and run the smallest relevant validation.
2. Commit and push with a normal, non-force push.
3. Reply in the same thread with the fix commit SHA, validation result, and
   concise rationale. For dismissal or tracked deferral, reply with evidence or
   the issue link.
4. Resolve the thread only after the reply is visible on GitHub.

If posting, replying, pushing, or resolving fails, stop. Leave the PR draft and
report the exact unresolved thread.

## Record clean passes and convergence

A pass with no new confirmed findings leaves a PR review comment naming the
engine, round, exact reviewed head SHA, and `no new material findings`. Clean
evidence becomes stale as soon as the PR head changes.

For a two-engine loop:

- run one fresh Codex pass and one fresh Claude pass per round;
- classify committed fixes as `material` or `minor`;
- restart at Codex when either engine makes a material fix;
- keep minor fixes, but do not restart solely because of them;
- converge only after a complete Codex-then-Claude round reports no material
  fixes and every local-review thread has a reply and is resolved;
- stop at the configured round cap, preserving the draft PR and reporting
  non-convergence.

The next reviewer must read this ledger before reviewing the new head. That is
how prior rationale survives after local model context has been discarded.
