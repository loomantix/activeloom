import { execFileSync } from 'node:child_process';
import {
  DISPOSITION_V1,
  DISPOSITION_V1_RE,
  FINDING_V1,
  FINDING_V1_RE,
  LEGACY_THREAD_MARKER_RE,
  PROTOCOL_THREAD_MARKER_RE,
  PR_V1_MARKERS,
  PROTOCOL_VERSION,
  SUBPROCESS_MAX_BUFFER,
} from './constants.js';
import { classifyRangeEffect } from './effect.js';
import { fail, LedgerError } from './errors.js';
import {
  assertActor,
  assertThreadScope,
  compareIsForward,
  currentActor,
  deleteIssueComment,
  findMatchingAttestation,
  findMatchingBody,
  getGitHubRunner,
  getIssueComments,
  getPostedCommentId,
  getPrFiles,
  getReviewComments,
  jsonOutput,
  loadAllowedHeads,
  loadHistoricalCommentIds,
  postReviewComment,
  findRootThread,
  reviewThreads,
  rowsHaveHistoricalMarkers,
  setThreadState,
  verifyComment,
  verifyGitTransition,
  verifyHead,
  verifyIssueComment,
  verifyPseudoV3History,
  verifyReviewBase,
} from './github.js';
import { sha256Bytes, sha256Text } from './hash.js';
import { validateAnchor } from './diff.js';
import {
  buildDispositionBody,
  buildFindingBody,
  matchDisposition,
  matchFinding,
  matchPseudoV3,
  readLegacyBody,
  resolveContent,
  verifyV1Marker,
} from './protocol.js';
import {
  readResult,
  readResultBytes,
  resultHead,
  validateResultData,
  writeResultFile,
} from './result.js';
import { reviewRuns } from './runs.js';
import {
  archiveResultRecovery,
  historicalSnapshotDigest,
  readResultRecovery,
  saveResultRecovery,
} from './recovery.js';
import type {
  AttestParams,
  AttestResult,
  DisposeParams,
  DisposeResult,
  DispositionV1Match,
  DispositionV3Match,
  FinalizeParams,
  FindingV1Match,
  FindingV3Match,
  GitHubReviewCommentNode,
  GitHubReviewThreadNode,
  LedgerResult,
  PostFindingParams,
  PostFindingResult,
  PostPrCommentParams,
  PreflightAnchorParams,
  PreflightAnchorResult,
  ReconcileParams,
  ReconcileResult,
  RecoverResultParams,
  ReopenOccurrenceParams,
  ReopenOccurrenceResult,
  ReplyParams,
  ResolveParams,
  ResolveResult,
  SupportedEngine,
  VerifyLedgerParams,
  VerifyLedgerResult,
  WriteResultParams,
} from './types.js';

/**
 * Extract every actor-owned protocol record from a review thread.
 *
 * A comment that presents itself as a local-review record but does not parse as
 * one is fatal, never skipped: silently ignoring an unparsable marker is how a
 * mangled or forged record disappears from the ledger instead of failing it.
 */
export function threadProtocolRecords(
  thread: GitHubReviewThreadNode,
  historicalCommentIds?: Set<number> | undefined,
): {
  findingsV3: Array<[number, FindingV3Match]>;
  dispositionsV3: Array<[number, DispositionV3Match]>;
  findingsV1: Array<[number, FindingV1Match]>;
  dispositionsV1: Array<[number, DispositionV1Match]>;
} {
  verifyPseudoV3History([thread], historicalCommentIds);
  const comments = thread.comments.nodes;
  const actor = currentActor();

  const findingsV3: Array<[number, FindingV3Match]> = [];
  const dispositionsV3: Array<[number, DispositionV3Match]> = [];
  const findingsV1: Array<[number, FindingV1Match]> = [];
  const dispositionsV1: Array<[number, DispositionV1Match]> = [];

  for (let index = 0; index < comments.length; index++) {
    const row = comments[index]!;
    const body = String(row.body ?? '');
    const author = row.author ?? row.user;
    if (!author || typeof author.login !== 'string') {
      if (body.includes('<!-- local-review')) {
        fail('could not establish local-review comment ownership');
      }
      continue;
    }
    if (author.login !== actor) {
      continue;
    }

    const firstLine = body.split('\n', 1)[0]!.replace(/\r$/, '');
    const pseudo = matchPseudoV3(body);
    const findingV3 = matchFinding(body);
    const dispositionV3 = matchDisposition(body);
    if (body.includes('<!-- local-review:v3') && !findingV3 && !pseudo) {
      fail('actor-owned local-review:v3 marker is malformed or unsupported');
    }

    const findingV1 = FINDING_V1_RE.exec(body);
    const dispositionV1 = DISPOSITION_V1_RE.exec(body);

    if (
      PROTOCOL_THREAD_MARKER_RE.test(firstLine) &&
      !findingV3 &&
      !dispositionV3 &&
      !findingV1 &&
      !dispositionV1
    ) {
      fail('actor-owned local-review marker is malformed or unsupported');
    }

    if (findingV3) {
      findingsV3.push([index, findingV3]);
    }
    if (dispositionV3) {
      dispositionsV3.push([index, dispositionV3]);
    }

    const legacyMarker = LEGACY_THREAD_MARKER_RE.test(firstLine);
    if (
      (body.includes(FINDING_V1) ||
        body.includes(DISPOSITION_V1) ||
        legacyMarker) &&
      !findingV1 &&
      !dispositionV1
    ) {
      fail('actor-owned legacy local-review marker is malformed');
    }

    if (findingV1?.groups) {
      verifyV1Marker(body, findingV1, 'finding');
      findingsV1.push([
        index,
        {
          engine: findingV1.groups['engine'] as SupportedEngine,
          round: parseInt(findingV1.groups['round']!, 10),
          head: findingV1.groups['head']!,
          fingerprint: findingV1.groups['fingerprint']!,
        },
      ]);
    }
    if (dispositionV1?.groups) {
      verifyV1Marker(body, dispositionV1, 'disposition');
      dispositionsV1.push([
        index,
        {
          engine: dispositionV1.groups['engine'] as SupportedEngine,
          round: parseInt(dispositionV1.groups['round']!, 10),
          head: dispositionV1.groups['head']!,
          fingerprint: dispositionV1.groups['fingerprint']!,
          outcome: dispositionV1.groups['outcome'] as
            | 'fixed'
            | 'dismissed'
            | 'deferred',
        },
      ]);
    }
  }

  if (findingsV1.length > 0 || dispositionsV1.length > 0) {
    if (thread.isResolved !== true) {
      fail('legacy local-review finding thread is unresolved');
    }
    // V1 tracked root causes, not numbered occurrences. A later pass could
    // confirm the correction for earlier passes (including another engine),
    // and repeated confirmations were normal. These are settled historical
    // threads only; v3 pairing and exact-head coverage remain strict.
    for (const [findingIndex, finding] of findingsV1) {
      const settled = dispositionsV1.some(
        ([index, disposition]) =>
          index > findingIndex &&
          disposition.fingerprint === finding.fingerprint,
      );
      if (!settled) {
        fail(
          'legacy local-review finding lacks a later root-cause disposition',
        );
      }
    }
    for (const [dispositionIndex, disposition] of dispositionsV1) {
      if (
        !findingsV1.some(
          ([index, finding]) =>
            index < dispositionIndex &&
            finding.fingerprint === disposition.fingerprint,
        )
      ) {
        fail('legacy local-review ledger contains an orphan disposition');
      }
    }
  }

  return { findingsV3, dispositionsV3, findingsV1, dispositionsV1 };
}

/**
 * Pair every finding in a thread with its single matching disposition.
 *
 * Enforces the three cardinality rules the ledger depends on: exactly one
 * disposition per finding, no disposition claimed by two findings, and no
 * disposition left over. A recurrence may not be opened before the prior
 * occurrence was disposed.
 */
export function pairDispositions<
  F extends FindingV3Match | FindingV1Match,
  D extends DispositionV3Match | DispositionV1Match,
>(
  findings: Array<[number, F]>,
  dispositions: Array<[number, D]>,
): Array<[F, D]> {
  const matched: Array<[F, D]> = [];
  const used = new Set<number>();

  for (const [findingIndex, finding] of findings) {
    const candidates = dispositions.filter(([index, disposition]) => {
      if (index <= findingIndex) return false;
      if (disposition.engine !== finding.engine) return false;
      if (disposition.round !== finding.round) return false;
      if (disposition.fingerprint !== finding.fingerprint) return false;
      if ('occurrence' in finding && 'occurrence' in disposition) {
        if (finding.occurrence !== disposition.occurrence) return false;
      }
      return true;
    });
    if (candidates.length !== 1) {
      fail('local-review finding lacks exactly one matching disposition');
    }
    const [dispositionIndex, disposition] = candidates[0]!;

    const nextFindingIndexes = findings
      .filter(
        ([nextIndex, nextFinding]) =>
          nextIndex > findingIndex &&
          nextFinding.fingerprint === finding.fingerprint,
      )
      .map(([nextIndex]) => nextIndex);
    if (
      nextFindingIndexes.length > 0 &&
      dispositionIndex >= Math.min(...nextFindingIndexes)
    ) {
      fail('local-review recurrence was opened before the prior disposition');
    }

    if (used.has(dispositionIndex)) {
      fail('local-review disposition matches multiple findings');
    }
    used.add(dispositionIndex);
    matched.push([finding, disposition]);
  }

  if (used.size !== dispositions.length) {
    fail('local-review ledger contains an orphan disposition');
  }
  return matched;
}

/**
 * Return the dispositions that could close a given finding.
 */
export function matchingDispositions<
  T extends FindingV3Match | FindingV1Match,
  D extends DispositionV3Match | DispositionV1Match,
>(
  findingIndex: number,
  finding: T,
  dispositions: Array<[number, D]>,
  expectedHead?: string,
): D[] {
  return dispositions
    .filter(([index, disposition]) => {
      if (index <= findingIndex) return false;
      if (disposition.engine !== finding.engine) return false;
      if (disposition.round !== finding.round) return false;
      if (disposition.fingerprint !== finding.fingerprint) return false;
      if ('occurrence' in finding && 'occurrence' in disposition) {
        if (finding.occurrence !== disposition.occurrence) return false;
      }
      if (expectedHead !== undefined && disposition.head !== expectedHead) {
        return false;
      }
      return true;
    })
    .map(([, disposition]) => disposition);
}

/**
 * Assert `after` is strictly ahead of `before` with `before` as the merge base.
 */
export function verifyForwardTransitionOrFail(
  repo: string,
  before: string,
  after: string,
  message: string,
): void {
  if (!compareIsForward(repo, before, after)) {
    fail(message);
  }
}

/**
 * Assert `after` is strictly ahead of `before` with `before` as merge base.
 */
export function verifyForwardTransition(
  repo: string,
  before: string,
  after: string,
): void {
  if (before === after) {
    fail('superseding fixed occurrence is not a forward transition');
  }
  verifyForwardTransitionOrFail(
    repo,
    before,
    after,
    'superseding local-review occurrence is not forward-only',
  );
}

/**
 * Walk every thread on the PR for malformed or unattributable protocol records.
 *
 * Used before writing a new record: a thread that already fails the protocol
 * must stop the pass rather than have a fresh record appended to it.
 */
export function verifyHistoricalThreads(repo: string, pr: number): void {
  for (const thread of reviewThreads(repo, pr)) {
    threadProtocolRecords(thread);
  }
}

/**
 * Require the latest blocking occurrence to be fixed.
 *
 * A fingerprint's occurrences are a sequential history of one root cause, so
 * the highest occurrence is its current state. When a later occurrence clears
 * an earlier unfixed blocker, the recurrence and its fix must form strict
 * forward Git transitions.
 */
export function verifyBlockingNotDeferred(
  repo: string | undefined,
  matched: Array<[FindingV3Match, DispositionV3Match]>,
): void {
  const grouped = new Map<
    string,
    Array<[number, FindingV3Match, DispositionV3Match]>
  >();
  for (const [finding, disposition] of matched) {
    const list = grouped.get(finding.fingerprint) ?? [];
    list.push([finding.occurrence, finding, disposition]);
    grouped.set(finding.fingerprint, list);
  }

  for (const records of grouped.values()) {
    records.sort((a, b) => a[0] - b[0]);
    const [, finding, disposition] = records[records.length - 1]!;
    if (finding.severity === 'blocking' && disposition.outcome !== 'fixed') {
      fail('blocking local-review findings must be fixed');
    }

    const priorUnfixedBlockers: number[] = [];
    for (let index = 0; index < records.length - 1; index++) {
      const [, priorFinding, priorDisposition] = records[index]!;
      if (
        priorFinding.severity === 'blocking' &&
        priorDisposition.outcome !== 'fixed'
      ) {
        priorUnfixedBlockers.push(index);
      }
    }
    if (priorUnfixedBlockers.length === 0) {
      continue;
    }
    if (disposition.outcome !== 'fixed') {
      fail('an unfixed blocker must be cleared by a later fixed occurrence');
    }
    if (repo === undefined) {
      fail(
        'clearing an unfixed blocker requires a repository to verify against',
      );
    }
    const start = priorUnfixedBlockers[priorUnfixedBlockers.length - 1]!;
    for (let index = start; index < records.length - 1; index++) {
      verifyForwardTransition(
        repo,
        records[index]![2].head,
        records[index + 1]![1].head,
      );
    }
    verifyForwardTransition(repo, finding.head, disposition.head);
  }
}

/**
 * Verify that every actor-owned finding thread on the PR is complete: scoped to
 * this PR, resolved, topologically sound, and fully disposed.
 *
 * Returns the matched finding/disposition pairs.
 */
export function verifyThreadDispositions(
  threads: GitHubReviewThreadNode[],
  historicalCommentIds?: Set<number> | undefined,
  options?: { repo?: string | undefined; pr?: number | undefined },
): Array<[FindingV3Match, DispositionV3Match]> {
  if (options?.repo !== undefined && options.pr !== undefined) {
    assertThreadScope(threads, options.repo, options.pr);
  }

  const matched: Array<[FindingV3Match, DispositionV3Match]> = [];
  const topology = new Map<
    string,
    Array<{ threadId: string; findingIndex: number; finding: FindingV3Match }>
  >();

  for (const thread of threads) {
    const { findingsV3, dispositionsV3 } = threadProtocolRecords(
      thread,
      historicalCommentIds,
    );
    if (findingsV3.length === 0 && dispositionsV3.length === 0) {
      continue;
    }
    if (findingsV3.length === 0) {
      fail('local-review ledger contains a disposition without a finding');
    }
    if (typeof thread.id !== 'string' || !thread.id) {
      fail('local-review finding thread has no stable identity');
    }
    for (const [findingIndex, finding] of findingsV3) {
      const list = topology.get(finding.fingerprint) ?? [];
      list.push({ threadId: thread.id, findingIndex, finding });
      topology.set(finding.fingerprint, list);
    }
    if (thread.isResolved !== true) {
      fail(`local-review finding thread ${thread.id} is unresolved`);
    }
    matched.push(...pairDispositions(findingsV3, dispositionsV3));
  }

  for (const records of topology.values()) {
    const threadIds = new Set(records.map((record) => record.threadId));
    const occurrences = records.map((record) => record.finding.occurrence);
    const expected = records.map((_, index) => index + 1);
    const roots = records.filter((record) => record.finding.occurrence === 1);
    if (
      threadIds.size !== 1 ||
      occurrences.length !== expected.length ||
      occurrences.some((value, index) => value !== expected[index]) ||
      roots.length !== 1 ||
      roots[0]!.findingIndex !== 0
    ) {
      fail('local-review fingerprint topology is invalid');
    }
  }

  verifyBlockingNotDeferred(options?.repo, matched);
  return matched;
}

/**
 * Derive this round's fix evidence from the verified finding/disposition pairs.
 *
 * Returns `[fingerprint, hasFix, hasMajorFix, hasNonblockingFix]` per fingerprint, sorted.
 *
 * `hasMajorFix` is reported for callers that summarise a ledger. It is not an
 * input to the `minor` / `material` decision: severity describes a finding and
 * says nothing about what its fix moved, so classification reads the change
 * range instead. See `classifyRangeEffect`.
 */
export function sameRoundDispositions(
  args: { engine: SupportedEngine; round: number; repo?: string | undefined },
  matched: Array<[FindingV3Match, DispositionV3Match]>,
  allowedHeads: Record<string, number>,
): Array<[string, boolean, boolean, boolean]> {
  const evidence = new Map<string, [boolean, boolean, boolean]>();

  for (const [finding, disposition] of matched) {
    if (finding.engine !== args.engine || finding.round !== args.round) {
      continue;
    }
    const findingHead = finding.head;
    const dispositionHead = disposition.head;
    const findingPosition = Object.prototype.hasOwnProperty.call(
      allowedHeads,
      findingHead,
    )
      ? allowedHeads[findingHead]!
      : undefined;

    if (findingPosition === undefined) {
      // Settled historical findings are validated but never become evidence
      // for the current before-to-after transition.
      if (disposition.outcome !== 'fixed') {
        continue;
      }
      if (dispositionHead === findingHead) {
        fail('historical fixed disposition is not a forward transition');
      }
      if (args.repo === undefined) {
        fail('historical fixed disposition is not a forward transition');
      }
      verifyForwardTransitionOrFail(
        args.repo,
        findingHead,
        dispositionHead,
        'historical fixed disposition is not a forward transition',
      );
      continue;
    }

    if (disposition.outcome === 'fixed' && dispositionHead === findingHead) {
      fail('fixed finding was not posted before its disposition head');
    }
    const dispositionPosition = Object.prototype.hasOwnProperty.call(
      allowedHeads,
      dispositionHead,
    )
      ? allowedHeads[dispositionHead]!
      : undefined;
    if (
      dispositionPosition === undefined ||
      dispositionPosition < findingPosition ||
      (disposition.outcome === 'fixed' &&
        dispositionPosition === findingPosition)
    ) {
      fail('same-round finding disposition is outside the observed transition');
    }
    if (finding.severity === 'blocking' && disposition.outcome !== 'fixed') {
      fail('blocking local-review findings must be fixed');
    }

    const fixed = disposition.outcome === 'fixed';
    const fixedMajor =
      fixed &&
      (finding.severity === 'blocking' || finding.severity === 'major');
    const fixedNonblocking = fixed && finding.severity !== 'blocking';
    const previous = evidence.get(finding.fingerprint) ?? [false, false, false];
    evidence.set(finding.fingerprint, [
      previous[0] || fixed,
      previous[1] || fixedMajor,
      previous[2] || fixedNonblocking,
    ]);
  }

  return Array.from(evidence.keys())
    .sort()
    .map(
      (fingerprint) =>
        [fingerprint, ...evidence.get(fingerprint)!] as [
          string,
          boolean,
          boolean,
          boolean,
        ],
    );
}

/**
 * Derive the ordered set of heads the review transition is allowed to span.
 */
export function transitionHeads(params: {
  repo: string;
  before: string;
  head: string;
  resultHead?: string | undefined;
  allowedHeadsFile?: string | undefined;
}): Record<string, number> {
  const target = resultHead(params);
  if (params.allowedHeadsFile !== undefined) {
    return loadAllowedHeads(
      params.allowedHeadsFile,
      params.before,
      target,
      params.repo,
    );
  }
  if (params.before === target) {
    return { [params.before]: 0 };
  }
  const runner = getGitHubRunner();
  const list = runner.gitRevList
    ? runner.gitRevList(params.before, target)
    : execFileSync(
        'git',
        [
          'rev-list',
          '--reverse',
          '--ancestry-path',
          `${params.before}..${target}`,
        ],
        {
          encoding: 'utf8',
          stdio: ['pipe', 'pipe', 'pipe'],
          maxBuffer: SUBPROCESS_MAX_BUFFER,
        },
      )
        .trim()
        .split(/\r?\n/)
        .filter(Boolean);

  const values = [params.before, ...list];
  if (
    values[values.length - 1] !== target ||
    new Set(values).size !== values.length
  ) {
    fail('review result transition is not forward-only');
  }
  const heads: Record<string, number> = {};
  values.forEach((value, index) => {
    heads[value] = index;
  });
  return heads;
}

/**
 * Verify that a review result is exactly backed by this round's ledger evidence.
 */
export function verifyResultEvidence(
  args: {
    repo: string;
    pr: number;
    head: string;
    engine: SupportedEngine;
    round: number;
    base: string;
    before: string;
    resultFile: string;
    resultHead?: string | undefined;
    allowedHeadsFile?: string | undefined;
  },
  threads: GitHubReviewThreadNode[],
  options?: {
    data?: LedgerResult | undefined;
    allowedHeads?: Record<string, number> | undefined;
    historicalCommentIds?: Set<number> | undefined;
  },
): LedgerResult {
  const data =
    options?.data ?? validateResultData(args, readResultBytes(args.resultFile));
  if (data.status !== 'clean' && data.status !== 'changed') {
    fail('ledger result evidence requires a clean or changed review result');
  }

  verifyReviewBase(args.repo, args.pr, args.base, args.before);
  verifyGitTransition(args.before, resultHead(args), args.head);

  const matched = verifyThreadDispositions(
    threads,
    options?.historicalCommentIds,
    { repo: args.repo, pr: args.pr },
  );
  const allowedHeads = options?.allowedHeads ?? transitionHeads(args);
  const evidence = sameRoundDispositions(args, matched, allowedHeads);

  const evidenceFingerprints = evidence.map(([fingerprint]) => fingerprint);
  const expected = [...data.findingFingerprints].sort();
  if (
    evidenceFingerprints.length !== expected.length ||
    evidenceFingerprints.some((value, index) => value !== expected[index])
  ) {
    fail(
      'review result fingerprints do not equal the complete same-round disposition set',
    );
  }

  if (data.status === 'clean') {
    if (evidence.some(([, hasFix]) => hasFix)) {
      fail('clean review results cannot have same-round fixes');
    }
    return data;
  }

  if (evidence.length === 0) {
    fail('changed review results require ledger evidence');
  }
  if (
    data.classification === 'minor' &&
    classifyRangeEffect(args.before, resultHead(args)) === 'behavioral'
  ) {
    fail('minor classification requires a non-behavioral change range');
  }
  if (!evidence.some(([, hasFix]) => hasFix)) {
    fail('changed review results require a fixed ledger finding');
  }
  if (
    args.round >= 3 &&
    evidence.some(([, , , hasNonblockingFix]) => hasNonblockingFix)
  ) {
    fail('convergence review results cannot fix non-blocking findings');
  }

  return data;
}

/**
 * Derive and persist a review result from the verified ledger state.
 */
export function writeResult(params: WriteResultParams): LedgerResult {
  const actor = assertActor(params.actor);
  const historicalSha256 = historicalSnapshotDigest(params);
  verifyReviewBase(params.repo, params.pr, params.base, params.before);
  verifyGitTransition(params.before, params.head, params.head);
  // Result derivation always reads live threads. A snapshot's seal proves only
  // that the file did not change after capture, never where it came from.
  const threads = reviewThreads(params.repo, params.pr);
  const historicalCommentIds = loadHistoricalCommentIds(
    params.historicalCommentIdsFile,
  );

  const matched = verifyThreadDispositions(threads, historicalCommentIds, {
    repo: params.repo,
    pr: params.pr,
  });

  const allowedHeads = transitionHeads({
    repo: params.repo,
    before: params.before,
    head: params.head,
    allowedHeadsFile: params.allowedHeadsFile,
  });

  const dispositions = sameRoundDispositions(params, matched, allowedHeads);

  const changed = params.before !== params.head;
  if (!changed && dispositions.some(([, hasFix]) => hasFix)) {
    fail('clean review results cannot have same-round fixes');
  }
  if (
    changed &&
    params.classification !== 'minor' &&
    params.classification !== 'material'
  ) {
    fail('changed review result requires --classification');
  }
  if (!changed && params.classification !== undefined) {
    fail('clean review result cannot have a classification');
  }
  // A consequence of the convergence rule below, not an independent judgement:
  // a convergence round may only fix a `blocking` finding, and no `blocking`
  // defect can be cleared by a comment or test edit, so any legitimate round-3+
  // fix moves behavior. Re-derive this if the blocking-only rule ever relaxes.
  if (changed && params.round >= 3 && params.classification !== 'material') {
    fail('round 3+ changed review results require material classification');
  }
  if (changed && dispositions.length === 0) {
    fail('changed review results require ledger evidence');
  }
  if (changed && !dispositions.some(([, hasFix]) => hasFix)) {
    fail('changed review results require a fixed ledger finding');
  }
  // This one reads severity on purpose. It enforces a disposition rule — what a
  // convergence round may change — not a classification, so decoupling
  // classification from severity leaves it alone. A convergence round therefore
  // still cannot fix a wrong comment; that is intended, because round 3+ exists
  // to land the change.
  if (
    changed &&
    params.round >= 3 &&
    dispositions.some(([, , , hasNonblockingFix]) => hasNonblockingFix)
  ) {
    fail('convergence review results cannot fix non-blocking findings');
  }
  const value: Record<string, unknown> = {
    version: PROTOCOL_VERSION,
    status: changed ? 'changed' : 'clean',
    engine: params.engine,
    round: params.round,
    baseSha: params.base,
    beforeSha: params.before,
    afterSha: params.head,
    classification: changed ? params.classification : null,
    findingFingerprints: dispositions.map(([fingerprint]) => fingerprint),
    finalLaneComplete: true,
  };

  const candidate = validateResultData(params, JSON.stringify(value));
  try {
    verifyResultEvidence(params, threads, {
      data: candidate,
      allowedHeads,
      historicalCommentIds,
    });
  } catch (error) {
    saveResultRecovery(params, candidate, actor, historicalSha256);
    throw error;
  }
  if (historicalSnapshotDigest(params) !== historicalSha256) {
    fail('pre-pass comment snapshot changed during result derivation');
  }
  writeResultFile(params.resultFile, value);
  archiveResultRecovery(params.resultFile);
  const raw = readResultBytes(params.resultFile);
  const resultSha256 = sha256Bytes(raw);

  return { ...candidate, resultSha256 };
}

/**
 * Retry finalization of a saved completed review without running a reviewer or
 * changing its classification, identity, findings, or round. Ordinary attest
 * still verifies and publishes the result after the caller's validation gates.
 */
export function recoverResult(params: RecoverResultParams): LedgerResult {
  const actor = assertActor(params.actor);
  verifyHead(params.repo, params.pr, params.head);
  const candidate = readResultRecovery(params, actor);
  const threads = reviewThreads(params.repo, params.pr);
  const historicalCommentIds = loadHistoricalCommentIds(
    params.historicalCommentIdsFile,
  );
  verifyResultEvidence(params, threads, {
    data: candidate,
    historicalCommentIds,
  });
  // Recheck the sealed local inputs after network verification, before writing.
  readResultRecovery(params, actor);
  verifyHead(params.repo, params.pr, params.head);
  writeResultFile(params.resultFile, { ...candidate });
  return {
    ...candidate,
    resultSha256: sha256Bytes(readResultBytes(params.resultFile)),
  };
}

/**
 * Assert a finding anchor lands on a real line of the PR diff.
 */
export function preflightAnchor(
  params: PreflightAnchorParams,
): PreflightAnchorResult {
  verifyHead(params.repo, params.pr, params.head);
  const files = getPrFiles(params.repo, params.pr);
  const line = params.fileLevel ? null : params.line;
  const side = params.fileLevel ? null : (params.side ?? 'RIGHT');
  validateAnchor(files, params.path, line, side);
  return {
    anchor: params.fileLevel
      ? 'file'
      : `${params.side ?? 'RIGHT'}:${params.line}`,
    path: params.path,
    verified: true,
  };
}

/**
 * Parse each comment once and keep the rows whose finding marker matches.
 */
function findingRows(
  comments: GitHubReviewCommentNode[],
  fingerprint: string | undefined,
): Array<{ row: GitHubReviewCommentNode; match: FindingV3Match }> {
  const rows: Array<{ row: GitHubReviewCommentNode; match: FindingV3Match }> =
    [];
  for (const row of comments) {
    const match = matchFinding(String(row.body ?? ''));
    if (match !== null && match.fingerprint === fingerprint) {
      rows.push({ row, match });
    }
  }
  return rows;
}

/**
 * Assert exactly one occurrence-1 record exists and that it is `commentId`.
 */
function assertRootComment(
  rows: Array<{ row: GitHubReviewCommentNode; match: FindingV3Match }>,
  commentId: number,
): void {
  const roots = rows.filter((entry) => entry.match.occurrence === 1);
  if (
    roots.length !== 1 ||
    (roots[0]?.row.databaseId ?? roots[0]?.row.id) !== commentId
  ) {
    fail('--comment-id does not identify the fingerprint root comment');
  }
}

/**
 * Post a new finding and verify it landed as written.
 */
export function postFinding(params: PostFindingParams): PostFindingResult {
  const content = resolveContent(params);

  let marker: string;
  let body: string;
  const isV3 = content !== undefined;

  if (isV3) {
    if (
      !params.engine ||
      params.round === undefined ||
      !params.fingerprint ||
      !params.severity ||
      !params.lens
    ) {
      fail(
        'v3 content mode requires --engine, --round, --fingerprint, --severity, --lens',
      );
    }
    const contentStr = content!;
    const built = buildFindingBody({
      engine: params.engine,
      round: params.round,
      head: params.head,
      fingerprint: params.fingerprint,
      occurrence: params.occurrence ?? 1,
      severity: params.severity,
      lens: params.lens,
      content: contentStr,
    });
    marker = built.marker;
    body = built.body;
  } else {
    marker = FINDING_V1;
    body = readLegacyBody(params.bodyFile ?? '-', marker, params.content);
  }

  verifyHead(params.repo, params.pr, params.head);
  const files = getPrFiles(params.repo, params.pr);
  const line = params.fileLevel ? null : params.line;
  const side = params.fileLevel ? null : (params.side ?? 'RIGHT');
  validateAnchor(files, params.path, line, side);

  let commentId: number;
  let replayed = false;

  if (isV3) {
    const comments = getReviewComments(params.repo, params.pr);
    if (rowsHaveHistoricalMarkers(comments)) {
      verifyHistoricalThreads(params.repo, params.pr);
    }
    const existing = findMatchingBody(
      comments as unknown as Array<Record<string, unknown>>,
      marker,
      body,
    );
    const records = findingRows(comments, params.fingerprint);

    if (existing === null && records.length > 0) {
      fail('fingerprint already has a root thread; use reopen-occurrence');
    }
    if ((params.occurrence ?? 1) !== 1) {
      fail('post-finding creates occurrence 1; use reopen-occurrence later');
    }

    const res = postReviewComment(
      params.repo,
      params.pr,
      params.head,
      marker,
      body,
      {
        path: params.path,
        line: params.fileLevel ? undefined : params.line,
        side: params.fileLevel ? undefined : (params.side ?? 'RIGHT'),
        fileLevel: params.fileLevel,
      },
    );
    commentId = res.commentId;
    replayed = res.replayed;
  } else {
    const payload: Record<string, unknown> = {
      body,
      commit_id: params.head,
      path: params.path,
    };
    if (params.fileLevel) {
      payload['subject_type'] = 'file';
    } else {
      payload['line'] = params.line;
      payload['side'] = params.side ?? 'RIGHT';
    }
    const response = jsonOutput(
      ['api', '-X', 'POST', `repos/${params.repo}/pulls/${params.pr}/comments`],
      payload,
    );
    commentId = getPostedCommentId(response);
    verifyComment(params.repo, commentId, body);
    verifyHead(params.repo, params.pr, params.head);
  }

  return {
    comment_id: commentId,
    verified: true,
    ...(isV3 ? { replayed } : {}),
  };
}

/**
 * Reopen a fingerprint as a new occurrence on an existing thread.
 */
export function reopenOccurrence(
  params: ReopenOccurrenceParams,
): ReopenOccurrenceResult {
  const content = resolveContent(params);
  if (content === undefined) {
    fail('reopen-occurrence requires content or content-file');
  }

  const { marker, body } = buildFindingBody({
    engine: params.engine,
    round: params.round,
    head: params.head,
    fingerprint: params.fingerprint,
    occurrence: params.occurrence,
    severity: params.severity,
    lens: params.lens,
    content,
  });

  verifyHead(params.repo, params.pr, params.head);
  const comments = getReviewComments(params.repo, params.pr);
  if (rowsHaveHistoricalMarkers(comments)) {
    verifyHistoricalThreads(params.repo, params.pr);
  }

  const records = findingRows(comments, params.fingerprint);
  const existing = findMatchingBody(
    comments as unknown as Array<Record<string, unknown>>,
    marker,
    body,
  );

  if (params.occurrence < 2) {
    fail('reopen-occurrence requires occurrence 2 or later');
  }

  assertRootComment(records, params.commentId);

  // Verify prior occurrences disposed
  const dispositions = comments
    .map((row) => matchDisposition(String(row.body ?? '')))
    .filter((d): d is DispositionV3Match => d !== null);

  for (const { match: finding } of records) {
    if (finding.occurrence >= params.occurrence) {
      continue;
    }
    const matches = dispositions.filter(
      (disp) =>
        disp.engine === finding.engine &&
        disp.round === finding.round &&
        disp.fingerprint === finding.fingerprint &&
        disp.occurrence === finding.occurrence,
    );
    if (matches.length !== 1) {
      fail('every prior finding occurrence must have exactly one disposition');
    }
  }

  // Checked on the replay path as well: a retry after a lost response is the
  // most likely moment for the occurrence history to be partially written.
  const occurrences = records
    .map((entry) => entry.match.occurrence)
    .sort((a, b) => a - b);
  const expectedLength = params.occurrence - (existing === null ? 1 : 0);
  const matchSeq =
    occurrences.length === expectedLength &&
    occurrences.every((v, i) => v === i + 1);
  if (!matchSeq) {
    fail('finding occurrences are missing, duplicated, or out of sequence');
  }

  const { commentId, replayed } = postReviewComment(
    params.repo,
    params.pr,
    params.head,
    marker,
    body,
    { replyTo: params.commentId },
  );

  const threadReplayed = setThreadState(
    params.threadId,
    false,
    params.commentId,
    { repo: params.repo, pr: params.pr },
  );
  verifyHead(params.repo, params.pr, params.head);

  return {
    comment_id: commentId,
    replayed,
    thread_replayed: threadReplayed,
    resolved: false,
    verified: true,
  };
}

/**
 * Record the outcome of a finding and resolve its thread.
 */
export function dispose(params: DisposeParams): DisposeResult {
  const content = resolveContent(params);
  if (content === undefined) {
    fail('dispose requires content or content-file');
  }

  const occurrence = params.occurrence ?? 1;
  const { marker, body } = buildDispositionBody({
    engine: params.engine,
    round: params.round,
    head: params.head,
    fingerprint: params.fingerprint,
    occurrence,
    outcome: params.outcome,
    content,
  });

  verifyHead(params.repo, params.pr, params.head);
  const comments = getReviewComments(params.repo, params.pr);
  if (rowsHaveHistoricalMarkers(comments)) {
    verifyHistoricalThreads(params.repo, params.pr);
  }

  const records = findingRows(comments, params.fingerprint);
  assertRootComment(records, params.commentId);

  const matches = records
    .map((entry) => entry.match)
    .filter(
      (match) =>
        match.engine === params.engine &&
        match.round === params.round &&
        match.occurrence === occurrence,
    );

  if (matches.length !== 1) {
    fail(
      'disposition does not identify exactly one existing finding occurrence',
    );
  }

  const finding = matches[0]!;
  if (finding.severity === 'blocking' && params.outcome !== 'fixed') {
    fail('blocking local-review findings must be fixed');
  }

  // Consistency check
  const dispMatches = comments.filter((row) => {
    const match = matchDisposition(String(row.body ?? ''));
    return (
      match !== null &&
      match.engine === params.engine &&
      match.round === params.round &&
      match.fingerprint === params.fingerprint &&
      match.occurrence === occurrence
    );
  });
  if (dispMatches.length > 1) {
    fail('disposition identity is duplicated');
  }
  if (dispMatches.length === 1 && dispMatches[0]?.body !== body) {
    fail(
      'disposition identity already exists with conflicting content or outcome',
    );
  }

  const { commentId, replayed } = postReviewComment(
    params.repo,
    params.pr,
    params.head,
    marker,
    body,
    { replyTo: params.commentId },
  );

  const threadReplayed = setThreadState(
    params.threadId,
    true,
    params.commentId,
    { repo: params.repo, pr: params.pr },
  );
  verifyHead(params.repo, params.pr, params.head);

  return {
    comment_id: commentId,
    replayed,
    thread_replayed: threadReplayed,
    resolved: true,
    verified: true,
  };
}

/**
 * Post a plain reply to an existing review comment.
 */
export function reply(params: ReplyParams): {
  comment_id: number;
  verified: true;
} {
  const content = resolveContent(params);
  let body: string;
  if (content !== undefined) {
    if (
      !params.engine ||
      params.round === undefined ||
      !params.fingerprint ||
      !params.outcome
    ) {
      fail(
        'legacy reply content requires engine, round, fingerprint, and outcome',
      );
    }
    // Reuse canonical field validation; only the historical wire layout differs.
    buildDispositionBody({
      engine: params.engine,
      round: params.round,
      head: params.head,
      fingerprint: params.fingerprint,
      outcome: params.outcome,
      occurrence: 1,
      content,
    });
    body = `${DISPOSITION_V1}engine=${params.engine} round=${params.round} head=${params.head} fingerprint=${params.fingerprint} outcome=${params.outcome} -->\n${content}`;
  } else {
    body = readLegacyBody(params.bodyFile ?? '-', DISPOSITION_V1, params.body);
  }
  verifyHead(params.repo, params.pr, params.head);
  if (content !== undefined) {
    const thread = reviewThreads(params.repo, params.pr).find(
      (candidate) =>
        candidate.comments.nodes[0]?.databaseId === params.commentId,
    );
    const actor = currentActor();
    if (
      !thread?.comments.nodes.some((row) => {
        if ((row.author ?? row.user)?.login !== actor) return false;
        const match = FINDING_V1_RE.exec(String(row.body ?? ''));
        if (!match || match.groups?.['fingerprint'] !== params.fingerprint)
          return false;
        verifyV1Marker(String(row.body), match, 'finding');
        return true;
      })
    ) {
      fail(
        'legacy reply must name an actor-owned finding in the target thread',
      );
    }
  }
  const response = jsonOutput(
    [
      'api',
      '-X',
      'POST',
      `repos/${params.repo}/pulls/${params.pr}/comments/${params.commentId}/replies`,
    ],
    { body },
  );
  const commentId = getPostedCommentId(response);
  verifyComment(params.repo, commentId, body);
  verifyHead(params.repo, params.pr, params.head);
  return { comment_id: commentId, verified: true };
}

/**
 * Post a plain conversation comment on the PR.
 */
export function postPrComment(params: PostPrCommentParams): {
  comment_id: number;
  verified: true;
} {
  const body = readLegacyBody(
    params.bodyFile ?? '-',
    PR_V1_MARKERS,
    params.body,
  );
  verifyHead(params.repo, params.pr, params.head);
  const response = jsonOutput(
    ['api', '-X', 'POST', `repos/${params.repo}/issues/${params.pr}/comments`],
    { body },
  );
  const commentId = getPostedCommentId(response);
  verifyIssueComment(params.repo, commentId, body);
  verifyHead(params.repo, params.pr, params.head);
  return { comment_id: commentId, verified: true };
}

/**
 * Publish the round's completion marker once the ledger backs the result.
 */
export function attest(params: AttestParams): AttestResult {
  const raw = readResultBytes(params.resultFile);
  const data = validateResultData(params, raw);
  assertActor(params.actor);
  const resultHash = sha256Bytes(raw);

  if (resultHash !== params.expectedResultSha256) {
    fail('review result changed before attestation');
  }

  if (data.status === 'blocked') {
    fail('blocked review results cannot be attested as complete');
  }

  const threads = reviewThreads(
    params.repo,
    params.pr,
    params.threadsFile,
    params.expectedThreadsSha256,
  );
  const historicalCommentIds = loadHistoricalCommentIds(
    params.historicalCommentIdsFile,
  );

  verifyResultEvidence(params, threads, { data, historicalCommentIds });

  const content =
    resolveContent(params) ??
    (data.status === 'clean'
      ? 'No new material findings.'
      : 'Review fixes completed and ledger dispositions verified.');

  let marker: string;
  if (data.status === 'clean') {
    marker = `<!-- local-review-pass:v3 engine=${params.engine} round=${params.round} base=${params.base} head=${params.head} result-sha256=${resultHash} -->`;
  } else {
    const fingerprints = data.findingFingerprints.join(',');
    marker = `<!-- local-review-complete:v3 engine=${params.engine} round=${params.round} base=${params.base} before=${params.before} head=${params.head} classification=${data.classification} fingerprints=${fingerprints} result-sha256=${resultHash} -->`;
  }

  let body = `${marker}\n${content}`;
  verifyHead(params.repo, params.pr, params.head);

  const issueComments = getIssueComments(params.repo, params.pr);
  const runs = reviewRuns(issueComments);
  const run = runs.at(-1);
  if (run && (run.base !== params.base || params.round > run.maxRounds)) {
    fail(
      'saved review result does not belong to the current run base and round budget',
    );
  }
  const existing = findMatchingAttestation(
    issueComments,
    runs,
    params.engine,
    params.round,
    body,
  );
  let commentId: number;
  let replayed = existing !== null;
  let created = existing === null;

  if (existing === null) {
    try {
      const response = jsonOutput(
        [
          'api',
          '-X',
          'POST',
          `repos/${params.repo}/issues/${params.pr}/comments`,
        ],
        { body },
      );
      commentId = getPostedCommentId(response);
    } catch (error) {
      if (error instanceof LedgerError) {
        const refetched = getIssueComments(params.repo, params.pr);
        const recovered = findMatchingAttestation(
          refetched,
          reviewRuns(refetched),
          params.engine,
          params.round,
          body,
        );
        if (recovered === null) throw error;
        commentId = recovered.id;
        body = recovered.body;
        replayed = true;
        created = false;
      } else {
        throw error;
      }
    }
  } else {
    commentId = existing.id;
    // Explanatory prose is not evidence identity. Preserve it on recovery.
    body = existing.body;
  }

  // An attestation that fails its own read-back must not survive: the marker is
  // what later rounds read to decide whether this round happened.
  try {
    verifyIssueComment(params.repo, commentId, body);
    if (
      reviewRuns(getIssueComments(params.repo, params.pr)).at(-1)?.id !==
      run?.id
    ) {
      fail(
        'review run changed during finalization; retry from the saved result after reconciling the current run',
      );
    }
    verifyReviewBase(params.repo, params.pr, params.base, params.before);
    verifyHead(params.repo, params.pr, params.head);
  } catch (error) {
    if (created) {
      try {
        deleteIssueComment(params.repo, params.pr, commentId);
      } catch (rollbackError) {
        throw new LedgerError(
          `attestation verification failed and rollback could not be verified: ${
            (rollbackError as { message?: string }).message ??
            String(rollbackError)
          }`,
          { cause: error },
        );
      }
    }
    throw error;
  }

  return {
    comment_id: commentId,
    replayed,
    result_sha256: resultHash,
    verified: true,
  };
}

/**
 * Finish an interrupted pass from its original result, without another model
 * invocation or manually copied SHAs/digests. All attest checks still apply.
 */
export function finalize(params: FinalizeParams): AttestResult {
  const result = readResult(params.resultFile);
  return attest({
    ...params,
    head: result.afterSha,
    engine: result.engine,
    round: result.round,
    base: result.baseSha,
    before: result.beforeSha,
    expectedResultSha256: result.resultSha256,
  });
}

/**
 * Resolve a single review thread.
 */
export function resolve(params: ResolveParams): ResolveResult {
  verifyHead(params.repo, params.pr, params.head);
  assertThreadIdsInScope(params.repo, params.pr, [params.threadId]);
  setThreadState(params.threadId, true, undefined, {
    repo: params.repo,
    pr: params.pr,
  });
  verifyHead(params.repo, params.pr, params.head);
  return { thread_id: params.threadId, resolved: true };
}

/**
 * Resolve several review threads and report what changed.
 */
export function resolveThreads(params: {
  repo: string;
  pr: number;
  head: string;
  threadIds: string[];
}): { threadIds: string[]; resolved: true } {
  verifyHead(params.repo, params.pr, params.head);
  assertThreadIdsInScope(params.repo, params.pr, params.threadIds);
  for (const threadId of params.threadIds) {
    setThreadState(threadId, true, undefined, {
      repo: params.repo,
      pr: params.pr,
    });
  }
  verifyHead(params.repo, params.pr, params.head);
  return { threadIds: params.threadIds, resolved: true };
}

function assertThreadIdsInScope(
  repo: string,
  pr: number,
  threadIds: string[],
): void {
  const scopedIds = new Set(reviewThreads(repo, pr).map((thread) => thread.id));
  if (threadIds.some((threadId) => !scopedIds.has(threadId))) {
    fail('review thread does not belong to the requested PR');
  }
}

/**
 * Report the ledger state of one fingerprint and the next valid action.
 */
export function reconcile(params: ReconcileParams): ReconcileResult {
  verifyHead(params.repo, params.pr, params.head);
  const comments = getReviewComments(params.repo, params.pr);
  if (rowsHaveHistoricalMarkers(comments)) {
    verifyHistoricalThreads(params.repo, params.pr);
  }

  const findingRows: Array<Record<string, unknown>> = [];
  const dispositionRows: Array<Record<string, unknown>> = [];

  for (const row of comments) {
    const body = String(row.body ?? '');
    const finding = matchFinding(body);
    const disposition = matchDisposition(body);
    if (finding && finding.fingerprint === params.fingerprint) {
      findingRows.push({ id: row.databaseId ?? row.id, ...finding });
    }
    if (disposition && disposition.fingerprint === params.fingerprint) {
      dispositionRows.push({ id: row.databaseId ?? row.id, ...disposition });
    }
  }

  const occurrences = findingRows
    .map((row) => row['occurrence'] as number)
    .sort((a, b) => a - b);
  // An empty occurrence list is a valid sequence: it is what a fingerprint that
  // has never been posted looks like, which is the case reconcile exists for.
  const sequenceValid = occurrences.every((val, idx) => val === idx + 1);

  const identity = (row: Record<string, unknown>): string =>
    `${String(row['engine'])}|${String(row['round'])}|${String(row['fingerprint'])}|${String(row['occurrence'])}`;
  const findingKeys = new Set(findingRows.map(identity));
  const dispositionKeys = dispositionRows.map(identity);

  const disposed = new Set(
    dispositionRows.map((row) => row['occurrence'] as number),
  );
  const ledgerValid =
    sequenceValid &&
    new Set(dispositionKeys).size === dispositionKeys.length &&
    dispositionKeys.every((key) => findingKeys.has(key));

  const undisposed = occurrences.filter((occ) => !disposed.has(occ));
  const nextAction = !ledgerValid
    ? 'repair-sequence'
    : undisposed.length > 0
      ? 'dispose'
      : occurrences.length > 0
        ? 'reopen-occurrence'
        : 'post-finding';

  // reconcile is the recovery path after an uncertain mutation, so it has to
  // return something the caller can act on: resolving or replying to the thread
  // needs its node id, and whether it is already resolved decides which of
  // those two is correct. Deriving it here is what keeps recovery from
  // improvising an API call against a thread it has not identified.
  let threadId: string | null = null;
  let threadResolved: boolean | null = null;
  const rootIds = findingRows
    .filter((row) => (row['occurrence'] as number) === 1)
    .map((row) => row['id'])
    .filter((id): id is number => typeof id === 'number');
  if (ledgerValid && rootIds.length === 1) {
    const candidate = findRootThread(params.repo, params.pr, rootIds[0]!);
    if (candidate === null) {
      fail('could not identify exactly one root review thread');
    }
    if (
      typeof candidate.id !== 'string' ||
      typeof candidate.isResolved !== 'boolean'
    ) {
      fail('root review thread has an unexpected shape');
    }
    threadId = candidate.id;
    threadResolved = candidate.isResolved;
  }

  return {
    findings: findingRows,
    dispositions: dispositionRows,
    sequenceValid,
    ledgerValid,
    nextOccurrence: sequenceValid ? occurrences.length + 1 : null,
    undisposedOccurrences: undisposed,
    nextAction,
    threadId,
    threadResolved,
    verified: true,
  };
}

/**
 * Verify the complete ledger state of a PR, optionally against a review result.
 */
export function verifyLedger(params: VerifyLedgerParams): VerifyLedgerResult {
  const actor = assertActor(params.actor);
  verifyHead(params.repo, params.pr, params.head);

  const threads = reviewThreads(
    params.repo,
    params.pr,
    params.threadsFile,
    params.expectedThreadsSha256,
  );
  const historicalCommentIds = loadHistoricalCommentIds(
    params.historicalCommentIdsFile,
  );
  const matched = verifyThreadDispositions(threads, historicalCommentIds, {
    repo: params.repo,
    pr: params.pr,
  });

  if (params.resultFile !== undefined) {
    if (
      !params.engine ||
      params.round === undefined ||
      !params.base ||
      !params.before
    ) {
      fail(
        'verify-ledger result evidence requires --engine, --round, --base, --before, and --result-file',
      );
    }
    verifyResultEvidence(
      {
        repo: params.repo,
        pr: params.pr,
        head: params.head,
        engine: params.engine,
        round: params.round,
        base: params.base,
        before: params.before,
        resultFile: params.resultFile,
        resultHead: params.resultHead,
        allowedHeadsFile: params.allowedHeadsFile,
      },
      threads,
      { historicalCommentIds },
    );
  }

  // Re-check the head after verification: a head that moved mid-verification
  // means the state just verified is no longer the state on the PR.
  verifyHead(params.repo, params.pr, params.head);

  return { actor, dispositions: matched.length, verified: true };
}
