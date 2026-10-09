# 0016 — Review metrics leave the PR conversation

- Status: proposed
- Date: 2026-10-05 (evidence cutoff; sources retrieved the same day)
- Tracking: #397 (this record), #398 (defaults and disclosure)

Review telemetry is posted as one JSON-bearing comment per pass on the pull
request under review. This record moves it to a dedicated, locked issue in the
same repository and adds a pull-based exporter. Nothing else about the record
changes: same marker, same body, same validation, same non-fatal emission.

"Telemetry" below is the code's name for what user-facing text should call
review metrics; #398 owns that rename.

## Decisions taken as inputs

These were settled before the research and are not reopened here.

1. GitHub stays the default store, outside the conversation. The default needs
   only `gh` and a pull request.
2. Export is pull only. A pass writes to the store; an `export` command reads
   records out as JSONL.
3. Telemetry has no human-visible presence in the PR by default.
4. Durable means a record is recoverable later without reading the conversation.
5. Telemetry failure never fails a review, and never falls back to PR comments.
6. A reviewer never has its own cost history placed in its inputs.
7. Legacy telemetry comments are never deleted or rewritten.
8. Cutover is per repository, with no dual-write.
9. Only telemetry moves. Ledger records and attestations are untouched.
10. No new authority: no wider token scope, no secret, no app, and no write
    path into repository contents or merge gates.
11. Telemetry is never control evidence.
12. Records stay allowlisted: identifiers, enums, counts, and hashes.

## What was measured

Three recent pull requests in this repository, conversation comments only:

| PR   | Comments | Telemetry | Ledger markers | Human prose | Telemetry share of bytes |
| ---- | -------- | --------- | -------------- | ----------- | ------------------------ |
| #395 | 25       | 10        | 15             | 0           | 18,345 of 33,011 (56%)   |
| #373 | 15       | 5         | 10             | 0           | not measured             |
| #372 | 13       | 4         | 9              | 0           | not measured             |

Two things follow. Telemetry is 31–40% of comments and the only kind that
renders as a raw JSON block; each ledger marker renders as one or two sentences
under a hidden HTML comment. And after telemetry leaves, the conversation is
still entirely machine-authored: the human-readable review lives in inline
threads (12 on #395), not in the conversation. Moving telemetry removes the
JSON; it does not make the conversation a human discussion. That is a separate
project and is not proposed here.

## Candidates

Verified against GitHub's documentation on the date above.

| Candidate                  | Verdict  | Reason                                                                                                                                                                                                                                                                                    |
| -------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dedicated git ref or notes | Rejected | Creating a ref needs contents write. Rulesets target branches and tags only, so a custom namespace is a channel for commits that no review, signature, or push rule covers. Fails decision 10.                                                                                            |
| Check-run output           | Rejected | "Write permission for the REST API to interact with checks is only available to GitHub Apps", so a local pass cannot write at all. Since 2026-10-01 checks follow the Actions retention setting: 90 days by default and at most 90 days on a public repository. Fails decisions 4 and 10. |
| Workflow artifacts         | Rejected | Written only from a workflow run; same 90-day ceiling on public repositories. No pass in this repository runs in a workflow. Fails decision 4.                                                                                                                                            |
| Discussion comments        | Rejected | Needs the Discussions feature enabled and a separate `discussions` permission, and is GraphQL-only, so it adds a scope and a second API surface for no gain over an issue. Comment ceilings were not verified.                                                                            |
| Dedicated issue comments   | Accepted | The same endpoint family, token, and code path as today, pointed at a different issue number. One hard limit: an issue stops accepting comments at 2,500, so the store rotates.                                                                                                           |
| Minimised PR comments      | Rejected | Still one collapsed row per pass in the conversation. Remains available as an optional cleanup for legacy comments (see Migration).                                                                                                                                                       |

The four architectures first proposed in #397 — in-process storage adapters, an
HTTP ingestion API, OTLP export through a Collector, and a local journal with
replay — are rejected as the default for one shared reason: each puts a
destination, a secret, or delivery-semantics machinery inside a review pass,
which processes untrusted diff content. Pull export serves the same adopters
from outside the pass. Revisit when a named adopter needs push. A single
edit-in-place summary comment, `<details>`-collapsed JSON, and dual-write are
rejected because they keep machine data in the conversation or buy nothing the
exporter does not already provide.

## Who can write

| Writer                                   | PR comment (today) | Store issue                                  |
| ---------------------------------------- | ------------------ | -------------------------------------------- |
| Maintainer's local token, write access   | Yes                | Yes, same scope                              |
| Fork contributor's local token, no write | Yes                | No — the store is locked; record not written |
| Hosted workflow, same-repository PR      | Not used           | Not used                                     |
| Hosted workflow, PR from a public fork   | No (read-only)     | No (read-only)                               |

Every producer today is a local invocation of `emit-telemetry`; no workflow in
this repository emits. A fork contributor's record is dropped by design:
losing it is acceptable, accepting a forged one is not. The pass reports the
skip locally and continues.

If a hosted pass ever emits, the job that runs a model must not be given
`issues: write`. It hands the record to a second job that runs no model,
validates the record against the schema, and holds the scope. That is a
follow-up, not part of this change.

## The store

- **Shape.** One open issue per repository carrying a reserved label, locked,
  with one record per comment. The comment body is exactly what
  `buildTelemetryBody` renders today, so `matchTelemetry` parses legacy and new
  records alike. The record already names its `repo` and `pr`.
- **Creation.** A setup command creates the label and the issue and locks it.
  A pass never creates the store. With no store, emission is skipped with a
  stated reason and the pass continues.
- **Discovery.** A pass finds the store by label and state. Only someone with
  triage access can apply a label, so an outsider cannot plant a store.
- **Spoofing.** Locking limits comments to people with write access. The
  exporter additionally accepts a record only when the comment's
  `author_association` is owner, member, or collaborator, with an optional
  explicit login list. Rejected comments are counted, not silently skipped.
- **Concurrency.** Comments append without coordination. Two writers racing on
  one idempotency key can both post; the exporter collapses identical replays
  and reports conflicting ones, keeping the earliest.
- **Enrichment.** Duration enrichment stays an in-place edit of one comment,
  null to value, exactly as now. The export line carries the comment's
  `updated_at` so a downstream store can upsert.
- **Rotation.** A pass warns once the store passes 2,000 comments. A rotate
  command closes the full issue and opens the next. The exporter reads every
  issue carrying the label, open or closed.
- **Side effects.** The body is a fenced block with no issue or user
  references, so it creates no cross-reference on any pull request.
  Subscribers to the store issue are notified per record; the setup command
  says so.

## Export contract

`export` writes one JSON object per line and a trailer:

```json
{"schema":"activeloom.review-metrics.export/v1","source":{"kind":"store-issue","repo":"owner/name","issue":412,"commentId":1234567890,"authorAssociation":"MEMBER","createdAt":"2026-10-05T15:43:03Z","updatedAt":"2026-10-05T15:51:10Z"},"record":{"version":3,"idempotencyKey":"…","pr":395,"…":"…"}}
{"schema":"activeloom.review-metrics.export-trailer/v1","sources":[{"kind":"store-issue","issue":412,"commentsReported":1840,"commentsRead":1840}],"records":1836,"duplicatesCollapsed":3,"conflicts":0,"rejectedAuthor":1,"malformed":0,"complete":true}
```

- `source.kind` is `store-issue` or `pr-comment`; the latter is a legacy record.
- Deduplication is by `idempotencyKey` across both kinds.
- `complete` is true only when, for every source, the comments read equal the
  count GitHub reports for that issue. A partial export says so.
- `record` is the stored record unmodified. Its own `version` governs its
  shape; the export schema version governs the envelope only.
- The exporter needs read access and nothing else, and writes to stdout.

Legacy records are read from pull requests by number range or date. The
repository-wide comment listing is not relied on for completeness.

## Configuration

The store is selected by a third rendered gate beside the existing two:
`telemetry.store: pr | issue` in `.activeloom-config.yml`, rendered as
`LOOM_REVIEW_TELEMETRY_STORE`. Unset means `pr`, which is today's behaviour.

The gate reader, the rendered configuration, and the ledger bundle reach a
consumer in one sync, so within a repository they cannot disagree. The reader
rejects unknown keys and unknown values by disabling both gates, so the order is
expand then use: ship a reader and engine that accept the key, then let a
repository set it. `emit` and `extract` keep their names and meanings.

## Migration and rollback

1. Ship the exporter reading legacy PR comments only. Nothing else changes.
2. Downstream readers move to the exporter. Until they do, no repository
   switches.
3. Ship the store sink, the setup and rotate commands, and the gate key.
4. Each repository runs setup and sets `telemetry.store: issue`.

Rollback is setting the key back to `pr`. Records written to the store stay
there and the exporter keeps reading both, so nothing is lost in either
direction.

Legacy comments stay. Hiding them with GitHub's minimise action is reversible
and deletes nothing, and may be offered later as an explicit command that an
adopter runs per pull request. It is not part of this change.

## Conflicts raised

- **[0010](0010-assurance-control-matrix.md) makes `data.measurement_emission`
  a universal baseline that no declaration can turn off.** Today's
  `telemetry.emit: off` already contradicts that, and an opt-in default (#398)
  would widen the gap. No code reads the control yet. #398 has to resolve it:
  either the baseline is amended, or opt-in is rejected.
- **[0012](0012-assurance-ledger-compatibility.md) reserves telemetry v2 for
  assurance acceptance fields.** Decision 11 holds only if those fields remain
  a calibration signal that no gate reads. This record assumes that and changes
  nothing in v2.
- **Decision 6 gets weaker on its own.** Prompts currently tell a reviewer to
  exclude telemetry when reading the PR's comments. Once telemetry is not on the
  PR, those instructions are vacuous and a reviewer has no reason to open the
  store. The standing instruction never to read a telemetry marker stays.
- **#312** (the runner's pre-pass snapshot blocks on its own telemetry) cannot
  occur in a repository using the store, because no telemetry lands on the PR
  between snapshot and comparison. It still needs its fix for repositories on
  `pr`.

## Open for stage 2

- Idempotency lookup lists the whole store, up to 25 pages near the ceiling.
  Measure it; rotate earlier or filter by `since` if it matters.
- Recovery of a record skipped for a missing or full store: the pass already
  prints the record, so retaining it in the run directory and replaying from
  file is the likely answer.
- Whether the `issues` comment endpoints behave identically on a locked issue
  for every token type an adopter might use.
- The field audit for decision 12 found no free-text field. Engine, lens, and
  language keys match `^[a-z0-9-]+$`; provider bucket keys `^[a-z0-9_]+$`;
  model, effort, and version strings `^[A-Za-z0-9._:/-]+$`. The last admits a
  path-like value, which #398's public profile should consider.

## Stage 2

In order: the exporter over legacy comments; a `report` command on top of it
(tokens per pull request, rounds to convergence, findings by engine and
outcome); the store sink with setup and rotate; the gate key; a CI check that
the package contacts only the GitHub API; a published JSON Schema for the
record and the export line; a recipe for loading the JSONL into SQLite or
DuckDB.

## Appendix A — what assumes telemetry is a PR comment

Edit points only; mirrored and rendered copies follow their source.

| Area             | Where                                                                                                                                               | Change                                                                                                |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Sink             | `packages/review-ledger/src/telemetry.ts` — `prCommentSink`, `enrichTelemetryDuration` (result `sink` is hard-coded `'pr-comment'`)                 | Parameterise the target issue; add the store sink                                                     |
| CLI              | `packages/review-ledger/src/cli.ts` — `emit-telemetry` and `enrich-telemetry-duration` construct `prCommentSink` directly                           | Resolve the store from the gate; add `export`, setup, rotate                                          |
| Runner           | `.codex/skills/critique/scripts/review-chain-runner.py` — `telemetry_recorded` reads the PR's comments to choose emit or enrich                     | Read the store instead; it is a second, looser parser than `matchTelemetry` and should stop being one |
| Runner snapshots | Same file — telemetry is filtered from `before-comments.json` and the diagnose snapshot                                                             | Becomes a no-op; keep while any repository is on `pr`                                                 |
| Managed Gemini   | Same file — the worker runs with emission off so the runner alone publishes, and deduplication relies on both seeing one comment list               | Same list, new location                                                                               |
| Prompts          | `prompts/review/{claude,codex,agents}/REVIEW_WORKFLOW.md.hbs` "Pass Telemetry" sections and the critique, refactorpass, and pr-critique skill files | Reword "PR comment"; exclusion-by-prefix instructions become vacuous                                  |
| Gates            | `.claude/skills/critique/scripts/review-telemetry-gates.js`, `scripts/sync-engine.py`, `scripts/review-telemetry.json.template`                     | Accept and render the store key                                                                       |
| Protocol docs    | `packages/review-ledger/protocol/local-review-ledger.md`, `packages/review-ledger/README.md`, `.codex/references/review-chain-runner.md`            | State the store                                                                                       |
| Tests            | `packages/review-ledger/src/__tests__/telemetry.test.ts` and `cli.test.ts`, `tests/test_review_chain_runner.py`                                     | Cover both stores                                                                                     |
| Unaffected       | `usage-snapshot.js`, `telemetry-pass-key.js`, every ledger, roster, and run-marker reader                                                           | None; they match their own markers and never depended on telemetry                                    |

No code reads telemetry for gating, convergence, round counting, or the refactor
latch.

## Appendix B — a conversation, before and after

Before: the first twelve of #395's 25 conversation comments, in posted order,
each shown by its first visible line.

```text
run        Deep review started at `c8d3be219fe4`: Codex → Claude; up to 4 passes per engine.
roster     Review author: Codex. Independent reviewers: Claude.
refactor   Codex round 1 cleanup: direct inspection of the new skill, rendered adapters, …
telemetry  77 lines of JSON
telemetry  77 lines of JSON
pass       Runner-verified codex pass 1 at c8d3be219fe4ff1f8ec34f58d48140e8f9dee014.
refactor   Claude round 1 cleanup: read the canonical skill source, its three rendered adapters, …
telemetry  77 lines of JSON
telemetry  78 lines of JSON
complete   Runner-verified claude pass 1 at c07b009d3361444d1cf4b340fab91db6dec7f638.
telemetry  78 lines of JSON
pass       Runner-verified codex pass 2 at c07b009d3361444d1cf4b340fab91db6dec7f638.
```

After: the same stretch with telemetry in the store.

```text
run        Deep review started at `c8d3be219fe4`: Codex → Claude; up to 4 passes per engine.
roster     Review author: Codex. Independent reviewers: Claude.
refactor   Codex round 1 cleanup: direct inspection of the new skill, rendered adapters, …
pass       Runner-verified codex pass 1 at c8d3be219fe4ff1f8ec34f58d48140e8f9dee014.
refactor   Claude round 1 cleanup: read the canonical skill source, its three rendered adapters, …
complete   Runner-verified claude pass 1 at c07b009d3361444d1cf4b340fab91db6dec7f638.
pass       Runner-verified codex pass 2 at c07b009d3361444d1cf4b340fab91db6dec7f638.
```

The five records are comments on the store issue and appear in `export`.

## Sources

- GitHub REST, check runs: <https://docs.github.com/en/rest/checks/runs>
- GitHub rulesets: <https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets>
- Retention of checks, runs, and statuses from 2026-10-01: <https://github.blog/changelog/2026-08-27-actions-retention-will-cover-checks-workflow-runs-and-statuses/>
- Locking conversations: <https://docs.github.com/en/communities/moderating-comments-and-conversations/locking-conversations>
- `GITHUB_TOKEN` permissions, including fork pull requests: <https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/controlling-permissions-for-github_token>
- The 2,500-comment ceiling is stated by GitHub's own error text ("Commenting is disabled on issues with more than 2500 comments") and was not found in the documentation; confirm it in stage 2.
