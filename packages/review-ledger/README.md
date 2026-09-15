# @loomantix/review-ledger

Deterministic local-review ledger protocol, verification, and CLI for multi-engine code review relays (`codex`, `claude`, `gemini`, `antigravity`).

## Overview

The review ledger uses an open draft pull request as a durable ledger of actor-owned, content-integrity-checked records for local adversarial code reviews and cleanup passes. `@loomantix/review-ledger` provides:

- **Strict Protocol Validation**: Deterministic v3 marker serialization, SHA-256 content hashing, sequential occurrence verification, and blocker resolution enforcement.
- **Multi-Engine Support**: Unified contract for `codex`, `claude`, `gemini`, and `antigravity` reviewer engines.
- **Declared Rosters & Coverage**: A pull request declares its author engine and zero, one, or two reviewer engines; coverage is then derived from attestations naming the exact current head.
- **Published Protocol**: The engine-neutral contract every engine follows ships in the tarball at [`protocol/local-review-ledger.md`](./protocol/local-review-ledger.md), so each platform repository vendors one source of truth instead of maintaining its own copy.
- **Standalone CLI & TypeScript API**: Self-contained vendored CLI, dual ESM/CJS distribution, and CLI executable (`review-ledger`).

## Building

This package is not published. It lives in ActiveLoom and exists to produce the
vendored `review-ledger.js` bundle each harness root carries under
`skills/critique/scripts/`. It is a standalone pnpm package with its own
lockfile; there is no root workspace.

```bash
cd packages/review-ledger
pnpm install --frozen-lockfile
pnpm run typecheck
pnpm test
pnpm run build
```

Most GitHub-backed commands require Node.js 18 or later, Git with the reviewed
history available locally, and an authenticated [GitHub CLI](https://cli.github.com/)
session. File-only result validation and `--protocol-version` do not use GitHub.

### Vendored single-file build

The build emits `dist/review-ledger.bundle.js`: the whole CLI as one
self-contained, unminified ES module with a `#!/usr/bin/env node` shebang. It
imports no sibling chunk and needs no `node_modules`, so it can be copied
anywhere and run as `node review-ledger.js`. It answers `--version` with the
version in this package's `package.json`, so a vendored copy can always identify
itself. Invoke it through `node` rather than executing it directly; the vendored
files are committed with mode 0644.

A change to this package is not finished until the bundle is rebuilt and copied
into all three harness roots in the same pull request:

```bash
for root in .claude .codex .agents; do
  cp packages/review-ledger/dist/review-ledger.bundle.js "$root/skills/critique/scripts/review-ledger.js"
done
```

Also update `review-ledger.version` (this package's semver) and
`review-ledger.integrity` (the bundle's sha512) beside each copy. CI rebuilds the
bundle and fails when any vendored copy differs from the fresh build.

The package's `.npmrc` keeps pnpm's virtual store at `../../node_modules/.pnpm`.
esbuild records bundled module paths relative to the build directory, and that
location keeps the output byte-identical to bundles built before the package
moved here. Do not change it without rebuilding every vendored copy.

## CLI Usage

### Recover an interrupted pass

Use the original result and pre-pass snapshot; do not rerun a model just to
recreate a missing attestation:

```bash
review-ledger finalize --repo owner/repo --pr 123 \
  --result-file /path/to/original-result.json \
  --historical-comment-ids-file /path/to/original-snapshot.json
```

The command reads the pinned identity and digest from the saved result, which
it trusts as the original, and performs normal evidence, actor, Git, and
exact-head checks; use `attest` when you hold the pre-pass digest. Blocked or incomplete
results remain incomplete. Explanatory text can differ on a retry without
rewriting the original attestation. Run boundaries distinguish restarted rounds;
contradictory evidence within one run is still rejected. A target branch advancing
does not invalidate the reviewed base when it remains in the same ancestry.

See [recovery semantics](./protocol/local-review-ledger.md#recover-interrupted-reviews)
for bounded run resumption and the distinction between bookkeeping recovery and
missing review work. Complete the required validation before finalization.

When `write-result` reached a completed candidate but its final verification
failed, it writes a blocked result plus `<result-file>.recovery.json`. Preserve
both files. A controller that observed the successful worker return can retry
`recover-result` with the original identity arguments and
`--expected-recovery-sha256 <digest-pinned-at-worker-return>`, reusing the
original historical snapshot and omitting `--classification`. The command
revalidates all live evidence and writes the completed result; ordinary
validation and attestation still follow. It cannot recover an unfinished review
or create a new run. An explicit successful `write-result` retry archives any
obsolete recovery sidecar under its digest.

The `review-ledger` binary exposes all subcommands:

### Check Versions

```bash
review-ledger --version           # e.g. 1.0.2 — this package's version
review-ledger --protocol-version  # 3 — the ledger protocol it speaks
```

The two are independent: `--protocol-version` is the cross-engine compatibility
gate that must agree with every other implementation, while `--version`
identifies this build. `--version` is baked in at build time rather than read
from `package.json`, so it still answers correctly for a vendored single-file
build sitting on its own in a consumer repo.

### Check Protocol Version

```bash
review-ledger --protocol-version
# 3
```

### Preflight Diff Anchor

Verify that an inline review comment targets an exact diff line in GitHub's patch:

```bash
review-ledger preflight-anchor \
  --repo owner/repo \
  --pr 123 \
  --head 0123456789abcdef0123456789abcdef01234567 \
  --path src/utils.ts \
  --line 42 \
  --side RIGHT
```

### Post Inline Finding

Post an inline finding with an actor-owned v3 protocol marker and content hash:

```bash
review-ledger post-finding \
  --repo owner/repo \
  --pr 123 \
  --head 0123456789abcdef0123456789abcdef01234567 \
  --path src/utils.ts \
  --line 42 \
  --engine gemini \
  --round 1 \
  --fingerprint auth-token-leak \
  --occurrence 1 \
  --severity blocking \
  --lens security-reviewer \
  --content-file ./finding-notes.txt
```

### Dispose Finding

Post a disposition reply and resolve the review thread:

```bash
review-ledger dispose \
  --repo owner/repo \
  --pr 123 \
  --head 0123456789abcdef0123456789abcdef01234567 \
  --engine gemini \
  --round 1 \
  --fingerprint auth-token-leak \
  --occurrence 1 \
  --outcome fixed \
  --comment-id 1001 \
  --thread-id PRRT_kwDO123 \
  --content-file ./fix-rationale.txt
```

### Write & Validate Structured Results

Write pass result file:

```bash
review-ledger write-result \
  --repo owner/repo \
  --pr 123 \
  --head 0123456789abcdef0123456789abcdef01234567 \
  --engine gemini \
  --round 1 \
  --base 0000000000000000000000000000000000000000 \
  --before 1111111111111111111111111111111111111111 \
  --result-file ./review-result.json \
  --classification minor
```

Write blocked result:

```bash
review-ledger write-blocked-result \
  --head 0123456789abcdef0123456789abcdef01234567 \
  --engine gemini \
  --round 1 \
  --base 0000000000000000000000000000000000000000 \
  --before 1111111111111111111111111111111111111111 \
  --result-file ./review-result.json \
  --blocker-file ./blocked-reason.txt
```

### Declare the Roster & Check Coverage

Participation is declared, never inferred: an engine that has not attested is
otherwise indistinguishable from one that was never going to run.

```bash
# Claude wrote the change; Codex and Gemini will review it
review-ledger post-roster --repo owner/repo --pr 123 --head <sha> \
  --author claude --reviewers codex,gemini --content-file ./roster-reason.txt

# Solo review is allowed, but must be declared with a recorded reason
review-ledger post-roster --repo owner/repo --pr 123 --head <sha> \
  --author claude --reviewers none --content-file ./solo-reason.txt

# Changed your mind? Post again. The new roster supersedes the old one and
# both stay on the pull request, so the narrowing is visible and ordered.
review-ledger post-roster --repo owner/repo --pr 123 --head <new-sha> \
  --author claude --reviewers none --content-file ./narrowed-reason.txt

review-ledger read-roster --repo owner/repo --pr 123

# Report coverage at the exact current head
review-ledger coverage --repo owner/repo --pr 123 --head <sha>

# ...or fail when the ledger would assert something untrue about what happened
review-ledger verify-coverage --repo owner/repo --pr 123 --head <sha>
```

The roster marker is `local-review-roster:v2`. Its `declaration-sha256` covers
`author`, `reviewers`, `head`, and `supersedes` together with the recorded
reason, so no field can be edited in place after the fact; `head=` binds the
declaration to the commit it was made over; and `supersedes=` chains each
replacement to the roster it replaces. Pull requests carrying the older
`local-review-roster:v1` marker still read, but v1 puts the declaration outside
its own hash and names no commit, so a v1 roster is advisory: re-post it to
record the same choice as v2 evidence.

Neither `coverage` nor `verify-coverage` is a merge gate, and neither should be
wired into one. A developer who has looked at a change and judged its review
sufficient is always free to ship it. Solo review with a recorded reason is a
legitimate outcome, not a degraded one — what these commands owe you is an
accurate record of what happened, not a verdict on it.

`coverage` reports a `tier` over distinct **non-author** engines that attested
the exact head: `solo` (none), `cross` (one), `full` (two or more). The author
engine's own pass is reported as `authorAttested` but never counted — it re-reads
a change while still holding the rationale that produced it.

Because coverage is keyed to the exact head, it is also the invalidation rule.
An engine whose newest attestation names an earlier commit has not reviewed what
the pull request currently contains; an engine whose attestation names the
current commit has, whatever moved the head. A fix therefore invalidates only
the attestations that named the old head, rather than restarting a whole round.

### Classify the Changeset

The source-versus-docs rule every lane skips on is executable code rather than
prose, so four skills reading the same paragraph cannot drift into four
different answers.

```bash
# Which classes does the pinned review range touch, and may the pass skip?
review-ledger classify-changeset --base <base-sha> --head <head-sha>

# Classify a diff captured elsewhere, e.g. in CI
review-ledger classify-changeset --diff-file ./range.diff \
  --prompt-surface prompts/
```

Two answers come back per file and they are independent. `class` is what kind
of work a line represents — `app`, `test`, `docsConfig`, `generated` — which is
what makes tokens-per-application-line comparable. `reviewSignificant` is
whether a lane must run at all. A lockfile proves they cannot be one field: it
must be reviewed and it must stay out of every ratio.

### Record What a Pass Cost

```bash
review-ledger emit-telemetry --repo owner/repo --pr 123 \
  --engine claude --engine-version 2.1.237 \
  --pass-type review --review-tier deep --trigger interactive \
  --round 3 --stance convergence --status changed \
  --base <base-sha> --head <head-sha> \
  --token-source session-log-delta --tokens-file ./tokens.json \
  --findings-file ./findings.json --duration-seconds 512

# Render the record without posting it
review-ledger emit-telemetry ... --dry-run
```

For controller-managed reviews, supply `--telemetry-run-id <64-hex-run-id>`
(`runId` in the builder API) to separate restarted rounds at the same head.
Retries within that run retain the same key. Existing explicit
`--idempotency-key` values take precedence; callers without a controller must
save one unique key per pass and reuse it only for that pass's retries.

`--findings-file` is required, including for clean and skipped passes. Supply
explicit `posted` and `chainInducedRegressions` counts and the full
`bySeverityAndOutcome` ladder (`blocking`, `major`, `minor`, `nit`, each with
`validFixed`, `validDeferred`, and `invalidDismissed`). Missing or partial
measurements return `emitted: false`; they must not become a zero-finding pass.
The programmatic builder retains its existing optional-input defaults.

A pass with nothing to measure — a `clean` pass, a docs/config-only `skipped`
pass, or a `blocked` pass that ended before it dispositioned anything — must
still write a findings file, spelling the zeros out:

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

A measured zero and an omitted measurement are different claims, which is why
the zeros are written rather than inferred. Because emission is never fatal, an
invocation that omits the file reports `emitted: false` and still exits 0, so a
caller that skips this step loses the record without ever failing. Update the
callers that emit on these early-return paths before, or together with, the
bundle update that carries this requirement.

The marker is `local-review-telemetry:v1` followed by a JSON payload. Four
properties are load-bearing:

- **Numbers arrive as arguments.** This package never reads a session
  transcript, a home directory, or any other ambient state. Each engine
  extracts its own usage and passes it in.
- **`engine` and `lens` are open tokens** and the payload is versioned JSON
  validated against a schema, not regex-parsed inline attributes. Instrumenting
  a new engine needs no release of this package.
- **Unavailable never becomes zero.** A missing measurement is `null` and a
  measured zero is `0`; the two never collapse, and nothing is zero-filled.
- **Emission never fails a review.** A telemetry error is reported on stdout
  and the command still exits 0.

Records are written, never read back into a review: a pass must not see prior
telemetry. Callers must exclude them by marker prefix with
`isTelemetryComment` or `excludeTelemetryComments`, so a record type added
later is excluded without teaching the caller each versioned marker.

### Reconcile & Verify Ledger

```bash
# Reconcile finding state
review-ledger reconcile --repo owner/repo --pr 123 --head <sha> --fingerprint auth-token-leak

# Verify the complete thread ledger against live GitHub state
review-ledger verify-ledger --repo owner/repo --pr 123 --head <sha>

# ...or against a snapshot captured through a trusted channel. The digest
# detects later byte changes; it does not prove that the file came from GitHub.
review-ledger verify-ledger --repo owner/repo --pr 123 --head <sha> \
  --threads-file ./threads.json \
  --expected-threads-sha256 "$(sha256sum ./threads.json | cut -d' ' -f1)"
```

### Trust model

Three inputs are assertions the ledger checks, never values it takes on trust:

- `--actor` asserts who the authenticated GitHub session belongs to. It is
  compared against the live `gh api user` login and fails on mismatch; it cannot
  select whose comments count as actor-owned. `AGENT_LOOP_REVIEW_ACTOR` pins the
  same identity for a whole relay.
- `--threads-file` is offline input, not independently authenticated evidence.
  Capture it through a trusted channel and pass `--expected-threads-sha256` (or
  set `AGENT_LOOP_REVIEW_THREADS_SHA256`) from that channel. The digest detects
  changes after capture; a digest computed from the same untrusted file proves
  no GitHub provenance. Prefer a live fetch whenever provenance is required.
- Every thread must carry the repository and PR number it came from, and must
  match the `--repo` / `--pr` under review.

## Programmatic API

```typescript
import {
  writeResult,
  writeBlockedResult,
  readResult,
  formatFindings,
  verifyLedger,
  reconcile,
  postRoster,
  readRoster,
  coverage,
  verifyCoverage,
  classifyRange,
  buildTelemetryRecord,
  emitTelemetry,
  prCommentSink,
} from '@loomantix/review-ledger';

// Format findings table
const markdown = formatFindings([
  {
    path: 'packages/logging/src/index.ts',
    line: 15,
    severity: 'blocking',
    lens: 'security-reviewer',
    rootCause: 'Missing redaction on tenant ID',
  },
]);
```

## Migration from `review-ledger.py`

The subcommands and flags mirror `review-ledger.py`. For a marker whose protocol
version and engine both implementations support, the marker layout, content
hashing, and verification rules are intended to match. This package's tests pin
those rules for this implementation only — they do not execute
`review-ledger.py` or compare against its output, so they cannot detect drift
between the two. Verify compatibility against the implementation you exchange
records with rather than relying on this suite.

1. **Execution**: replace `python3 <skill>/scripts/review-ledger.py ...` with
   `review-ledger ...` or `npx @loomantix/review-ledger ...`. No Python runtime
   is needed; the external Git and GitHub CLI prerequisites above still apply.
2. **Fingerprints are supplied, not derived.** As in the Python, a fingerprint
   is a caller-chosen stable token for one root cause, passed as
   `--fingerprint`. This package deliberately ships no fingerprint generator: an
   engine-local hashing scheme would drift from the tokens already recorded on
   open PRs and would not agree across engines.
3. **Engine set.** This package accepts `codex`, `claude`, `gemini`, and
   `antigravity`. Before exchanging records with another implementation,
   confirm that it supports the selected engine and protocol version.
4. **`formatFindings` is additive**, with no counterpart in the Python. It is a
   presentation helper and no verification path depends on its output.

## License

Apache-2.0. Copyright 2026 Loomantix Inc.
