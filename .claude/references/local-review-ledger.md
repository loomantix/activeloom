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

`refactorpass`, `grill`, `deepgrill`, and `codex-review` all skip
docs/config-only changesets. This is the shared definition; classify the file
list for the pinned `<base-sha>..HEAD` range:

- **Source code** — `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`,
  `.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, `.bash`.
- **Docs / config / fixtures** — `.md`, `.txt`, `.yml`, `.yaml`, `.json`,
  `.toml`, `.gitignore`, `.gitattributes`, `LICENSE`, `CHANGELOG`, `README`,
  `.env.example`, anything under `docs/`, `*.fixture.*`, snapshot files.
- **Anything else** — treat as source.

Zero source files means skip; one or more means run the full pass. A mixed
changeset is not a partial skip — the source files justify the spend.

## Deliver the diff once

Every lane that fans out to review agents must decide how the changeset reaches
them. Left unspecified, the default improvisation is to write the whole diff to
one file and hand that path to every agent, each of which reads all of it and
then reads it again. The changeset is the largest single input to a review pass
and the most duplicated one, so its delivery is part of the contract.

Resolve the changed-file list once, from the pinned range, and reuse it:

```bash
git diff --name-only <base-sha>..HEAD
git diff --stat <base-sha>..HEAD
```

Then apply these rules.

1. **Scope each agent to the files it reviews.** Name the exact paths in the
   agent's prompt. An agent that owns four files must not be handed the other
   forty. A lens that genuinely spans the whole changeset — architectural
   altitude, cross-file consistency — gets the `--stat` summary and pulls
   individual files as it needs them.
2. **Prefer a scoped command over a stored artifact.** A per-path
   `git diff <base-sha>..HEAD -- <path>` is reproducible, needs no temp file,
   and returns only what the agent asked for. Reach for it before writing a
   diff to disk.
3. **An artifact, if one exists, is pinned and read once.** State its path and
   size in the prompt. Re-access a region with a targeted `grep -n` or a bounded
   read, never a second full read: the file cannot have changed, so a repeat
   read returns bytes the agent already has.
4. **Bound any large read.** Above roughly 25k characters, read with an explicit
   offset and limit, or narrow the range with `-- <path>`.

State the changeset's size when briefing an agent, the same way agent prompts
state an output ceiling. An agent told the diff spans 40 files reads
differently from one handed an unlabeled path.

These rules are about duplicated bytes, not about depth. Never drop a lens, skip
a file an agent needs, or leave a finding unpursued to satisfy them.

## Rebuild context from GitHub

At the start of every pass, read the PR description, changed files, current
diff, commits, and all review threads, including resolved and outdated threads.
Also read replies and clean-pass attestations from earlier local reviewers.

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
   concise rationale. A fixed finding must also carry this marker, using the
   same fingerprint as the finding and the full pushed fix SHA:

   ```text
   <!-- local-review-disposition:v1 engine=<codex|claude> round=<n> head=<fix-sha> fingerprint=<stable-id> outcome=fixed -->
   ```

   For dismissal or tracked deferral, reply with evidence or the issue link and
   use `outcome=dismissed` or `outcome=deferred` with the reviewed head.

4. Resolve the thread only after the reply is visible on GitHub.

If posting, replying, pushing, or resolving fails, stop. Leave the PR draft and
report the exact unresolved thread.

## Record clean passes and convergence

A pass with no new confirmed findings leaves a PR comment naming the engine,
round, exact reviewed head SHA, and `no new material findings`, carrying this
machine-readable marker:

```text
<!-- local-review-pass:v1 engine=<codex|claude> round=<n> head=<sha> -->
```

An automated runner requires that attestation from every pass that committed
nothing: a hook exiting successfully proves only that it ran, not that it read
anything. The marker's `head` must be the exact SHA reviewed, and a pass that
fixed something attests through its thread replies instead.

Clean evidence goes stale when **product code** changes, not whenever the head
moves. A later commit touching only tests, fixtures, comments, or docs leaves
every production line the attesting engine read byte-identical, so that
attestation still covers the new head: record it as carried forward, naming both
the attested SHA and the current one.

Do not treat every head move as invalidating. That reading is what produces an
unbounded loop, and it is not a hypothetical — test and doc hardening is always
available to find, so each engine's commits perpetually re-stale the other's
attestation and no round can terminate. The loop then feels productive, because
every round genuinely does surface findings; they are just findings about the
review's own artifacts rather than about the product.

Verify a carry-forward rather than assuming it. Diff the attested SHA against
the current head restricted to product paths; if that diff is empty, the
attestation holds and the round is done.

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
- classify committed fixes as `material` or `minor` **by what the fix changes,
  not by how severe the finding sounded**: a fix is `material` only when it
  changes product code — the application or library source that ships. A fix
  touching only tests, fixtures, comments, or docs is `minor` even when the
  finding that produced it was severity-high, because the shipped behavior is
  unchanged;
- restart at Codex when either engine makes a material fix;
- keep minor fixes, but do not restart solely because of them;
- converge after a complete Codex-then-Claude round in which neither engine
  changed product code, and every local-review thread has a reply and is
  resolved;
- stop at the configured round cap, preserving the draft PR and reporting
  non-convergence.

**When a pass changes no product code, stop and say so.** Do not open another
round, and do not let the round cap imply the remaining rounds are owed. State
that the pass was minor-only, that the other engine's attestation carries
forward, and recommend this repository's ship step by name — whatever it uses to
merge the PR. The reviewer that notices this is the one responsible for
surfacing it; a caller watching rounds go by cannot see that the fixes stopped
touching product code.

The signal to watch for is a round whose findings are all about tests, fixtures,
or comments. That means the product converged and the review has moved on to
auditing its own artifacts. Those findings can be real and still not be reasons
to keep reviewing: hardening assertions creates fresh assertions to mutate, so
the supply never runs out and the next round is guaranteed to find more. Ship,
and carry anything genuinely worth doing to a follow-up issue.

The next reviewer must read this ledger before reviewing the new head. That is
how prior rationale survives after local model context has been discarded.
