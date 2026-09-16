/**
 * CLI interface for @loomantix/review-ledger.
 * Create, reconcile, and disposition deterministic local-review ledger entries.
 */

import { readFileSync } from 'node:fs';
import {
  PACKAGE_VERSION,
  PROTOCOL_VERSION,
  SHA_64_RE,
  SUPPORTED_CLASSIFICATIONS,
  SUPPORTED_ENGINES,
  SUPPORTED_OUTCOMES,
  SUPPORTED_SEVERITIES,
  SUPPORTED_SIDES,
  TELEMETRY_PASS_TYPES,
  TELEMETRY_REVIEW_TIERS,
  TELEMETRY_STANCES,
  TELEMETRY_STATUSES,
  TELEMETRY_TOKEN_SOURCES,
  TELEMETRY_TRIGGERS,
} from './constants.js';
import { fail } from './errors.js';
import { assertRegularFile, parseJsonOrFail } from './io.js';
import { readContent } from './protocol.js';
import { requireSha } from './hash.js';
import {
  attest,
  finalize,
  dispose,
  postFinding,
  postPrComment,
  preflightAnchor,
  reconcile,
  reopenOccurrence,
  reply,
  resolve,
  verifyLedger,
  writeResult,
  recoverResult,
} from './ledger.js';
import { readResult, validateResult, writeBlockedResult } from './result.js';
import { classifyFiles, classifyRange, parseDiffPatch } from './changeset.js';
import {
  buildTelemetryBody,
  buildTelemetryRecord,
  emitTelemetry,
  prCommentSink,
  validateFindings,
} from './telemetry.js';
import {
  coverage,
  parseReviewers,
  postRoster,
  readRoster,
  verifyCoverage,
} from './roster.js';
import { formatFindings } from './format.js';
import { resetGitHubRunner } from './github.js';
import type {
  Changeset,
  ChangesetReport,
  EmitTelemetryResult,
  ReviewFinding,
  TelemetryLaneInput,
  TelemetryPassType,
  TelemetryReviewTier,
  TelemetryStance,
  TelemetryStatus,
  TelemetryTokenBucketInput,
  TelemetryTokenSource,
  TelemetryTrigger,
  SupportedClassification,
  SupportedEngine,
  SupportedOutcome,
  SupportedSeverity,
  SupportedSide,
} from './types.js';

interface CliArgs {
  command?: string | undefined;
  protocolVersion?: boolean | undefined;
  version?: boolean | undefined;
  repo?: string | undefined;
  pr?: number | undefined;
  head?: string | undefined;
  base?: string | undefined;
  before?: string | undefined;
  engine?: SupportedEngine | undefined;
  round?: number | undefined;
  fingerprint?: string | undefined;
  occurrence?: number | undefined;
  severity?: SupportedSeverity | undefined;
  lens?: string | undefined;
  outcome?: SupportedOutcome | undefined;
  commentId?: number | undefined;
  threadId?: string | undefined;
  path?: string | undefined;
  line?: number | undefined;
  fileLevel?: boolean | undefined;
  side?: SupportedSide | undefined;
  contentFile?: string | undefined;
  bodyFile?: string | undefined;
  resultFile?: string | undefined;
  resultHead?: string | undefined;
  allowedHeadsFile?: string | undefined;
  threadsFile?: string | undefined;
  actor?: string | undefined;
  historicalCommentIdsFile?: string | undefined;
  expectedResultSha256?: string | undefined;
  expectedRecoverySha256?: string | undefined;
  expectedThreadsSha256?: string | undefined;
  blockerFile?: string | undefined;
  classification?: SupportedClassification | undefined;
  jsonFile?: string | undefined;
  author?: SupportedEngine | undefined;
  reviewers?: string | undefined;
  file?: string | undefined;
  /**
   * The raw `--engine` value.
   *
   * Telemetry accepts an open engine token while every other command accepts
   * only the closed protocol enum, and the command is not reliably known until
   * the whole argument vector has been read.
   */
  engineRaw?: string | undefined;
  engineVersion?: string | undefined;
  passType?: TelemetryPassType | undefined;
  reviewTier?: TelemetryReviewTier | undefined;
  trigger?: TelemetryTrigger | undefined;
  stance?: TelemetryStance | undefined;
  status?: TelemetryStatus | undefined;
  tokenSource?: TelemetryTokenSource | undefined;
  tokensFile?: string | undefined;
  lanesFile?: string | undefined;
  findingsFile?: string | undefined;
  changesetFile?: string | undefined;
  diffFile?: string | undefined;
  promptStackSha256?: string | undefined;
  promptStackVersion?: string | undefined;
  repoInstructionsSha256?: string | undefined;
  durationSeconds?: string | undefined;
  emittedAt?: string | undefined;
  idempotencyKey?: string | undefined;
  telemetryRunId?: string | undefined;
  promptSurfaces?: string[] | undefined;
  truncated?: boolean | undefined;
  dryRun?: boolean | undefined;
}

function writeSortedJson(value: unknown): void {
  const source = value as Record<string, unknown>;
  const sorted: Record<string, unknown> = {};
  for (const key of Object.keys(source).sort()) {
    sorted[key] = source[key];
  }
  process.stdout.write(JSON.stringify(sorted) + '\n');
}

function readJsonFile(pathValue: string, label: string): unknown {
  assertRegularFile(pathValue, `${label} must be a regular non-symlink file`);
  return parseJsonOrFail(
    readFileSync(pathValue, 'utf8'),
    `${label} must contain valid UTF-8 JSON`,
  );
}

function readJsonArray(pathValue: string, label: string): unknown[] {
  const parsed = readJsonFile(pathValue, label);
  if (!Array.isArray(parsed)) {
    fail(`${label} must contain a JSON array`);
  }
  return parsed;
}

/**
 * Resolve the classified changeset for the pinned review range.
 *
 * A caller that already classified the range passes the result back rather than
 * paying for it twice; otherwise the range is read from the local clone. The
 * working tree is never the source: a classifier reading uncommitted state
 * would report a changeset no reviewer saw.
 */
function resolveChangesetReport(args: CliArgs): ChangesetReport {
  const options =
    args.promptSurfaces === undefined
      ? undefined
      : { promptSurfaces: args.promptSurfaces };
  if (args.diffFile) {
    assertRegularFile(
      args.diffFile,
      'diff file must be a regular non-symlink file',
    );
    return classifyFiles(
      parseDiffPatch(readFileSync(args.diffFile, 'utf8')),
      options,
    );
  }
  if (!args.base || !args.head) {
    fail('changeset classification requires --diff-file or --base and --head');
  }
  return classifyRange({ base: args.base, head: args.head, options });
}

function parseDurationSeconds(raw: string | undefined): number | null {
  if (raw === undefined) {
    return null;
  }
  if (!/^\d+(\.\d+)?$/.test(raw)) {
    fail('--duration-seconds must be a non-negative number');
  }
  return Number(raw);
}

/**
 * Accept either a bare changeset or the classifier's full report.
 *
 * `classify-changeset` prints the report, so a caller that stored its output
 * can hand the same file straight back rather than editing a sub-object out of
 * it first.
 */
function normalizeChangesetInput(value: unknown): Changeset {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    fail('changeset file must contain a JSON object');
  }
  const source = value as Record<string, unknown>;
  const nested = source['changeset'];
  if (typeof nested === 'object' && nested !== null && !Array.isArray(nested)) {
    return nested as Changeset;
  }
  return source as unknown as Changeset;
}

/**
 * Current time as the record's RFC 3339 UTC second.
 *
 * Sub-second precision is noise a review pass cannot support, and trimming it
 * here keeps every emitted timestamp one shape for the join to a dated rate
 * table downstream.
 */
function nowUtcSecond(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function parsePositiveInteger(value: string, name: string): number {
  if (!/^[1-9]\d*$/.test(value)) {
    fail(`${name} must be a positive integer`);
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) {
    fail(`${name} must be a positive safe integer`);
  }
  return parsed;
}

function parseEnum<T extends string>(
  arg: string,
  value: string,
  allowed: readonly T[],
): T {
  if (!allowed.includes(value as T)) {
    fail(`${arg} must be one of: ${allowed.join(', ')}`);
  }
  return value as T;
}

function parseCliArgs(argv: string[]): CliArgs {
  const args: CliArgs = {};
  let i = 0;

  while (i < argv.length) {
    const arg = argv[i]!;

    if (arg === '--protocol-version') {
      args.protocolVersion = true;
      i++;
      continue;
    }

    if (arg === '--version') {
      args.version = true;
      i++;
      continue;
    }

    if (!arg.startsWith('-') && args.command === undefined) {
      args.command = arg;
      i++;
      continue;
    }

    if (arg === '--file-level') {
      args.fileLevel = true;
      i++;
      continue;
    }

    if (arg === '--truncated') {
      args.truncated = true;
      i++;
      continue;
    }

    if (arg === '--dry-run') {
      args.dryRun = true;
      i++;
      continue;
    }

    const next = argv[i + 1];
    const parseVal = (name: string): string => {
      if (next === undefined || next.startsWith('--')) {
        fail(`missing argument for ${name}`);
      }
      i += 2;
      return next;
    };

    switch (arg) {
      case '--repo':
        args.repo = parseVal(arg);
        break;
      case '--pr':
        args.pr = parsePositiveInteger(parseVal(arg), arg);
        break;
      case '--head':
        args.head = parseVal(arg);
        break;
      case '--base':
        args.base = parseVal(arg);
        break;
      case '--before':
        args.before = parseVal(arg);
        break;
      case '--engine':
        args.engineRaw = parseVal(arg);
        break;
      case '--engine-version':
        args.engineVersion = parseVal(arg);
        break;
      case '--telemetry-run-id':
        args.telemetryRunId = parseVal(arg);
        break;
      case '--pass-type':
        args.passType = parseEnum(arg, parseVal(arg), TELEMETRY_PASS_TYPES);
        break;
      case '--review-tier':
        args.reviewTier = parseEnum(arg, parseVal(arg), TELEMETRY_REVIEW_TIERS);
        break;
      case '--trigger':
        args.trigger = parseEnum(arg, parseVal(arg), TELEMETRY_TRIGGERS);
        break;
      case '--stance':
        args.stance = parseEnum(arg, parseVal(arg), TELEMETRY_STANCES);
        break;
      case '--status':
        args.status = parseEnum(arg, parseVal(arg), TELEMETRY_STATUSES);
        break;
      case '--token-source':
        args.tokenSource = parseEnum(
          arg,
          parseVal(arg),
          TELEMETRY_TOKEN_SOURCES,
        );
        break;
      case '--tokens-file':
        args.tokensFile = parseVal(arg);
        break;
      case '--lanes-file':
        args.lanesFile = parseVal(arg);
        break;
      case '--findings-file':
        args.findingsFile = parseVal(arg);
        break;
      case '--changeset-file':
        args.changesetFile = parseVal(arg);
        break;
      case '--diff-file':
        args.diffFile = parseVal(arg);
        break;
      case '--prompt-stack-sha256':
        args.promptStackSha256 = parseVal(arg);
        break;
      case '--prompt-stack-version':
        args.promptStackVersion = parseVal(arg);
        break;
      case '--repo-instructions-sha256':
        args.repoInstructionsSha256 = parseVal(arg);
        break;
      case '--duration-seconds':
        args.durationSeconds = parseVal(arg);
        break;
      case '--emitted-at':
        args.emittedAt = parseVal(arg);
        break;
      case '--idempotency-key':
        args.idempotencyKey = parseVal(arg);
        break;
      case '--prompt-surface':
        args.promptSurfaces = [...(args.promptSurfaces ?? []), parseVal(arg)];
        break;
      case '--round':
        args.round = parsePositiveInteger(parseVal(arg), arg);
        break;
      case '--fingerprint':
        args.fingerprint = parseVal(arg);
        break;
      case '--occurrence':
        args.occurrence = parsePositiveInteger(parseVal(arg), arg);
        break;
      case '--severity':
        args.severity = parseEnum(arg, parseVal(arg), SUPPORTED_SEVERITIES);
        break;
      case '--lens':
        args.lens = parseVal(arg);
        break;
      case '--outcome':
        args.outcome = parseEnum(arg, parseVal(arg), SUPPORTED_OUTCOMES);
        break;
      case '--comment-id':
        args.commentId = parsePositiveInteger(parseVal(arg), arg);
        break;
      case '--thread-id':
        args.threadId = parseVal(arg);
        break;
      case '--path':
        args.path = parseVal(arg);
        break;
      case '--line':
        args.line = parsePositiveInteger(parseVal(arg), arg);
        break;
      case '--side':
        args.side = parseEnum(arg, parseVal(arg), SUPPORTED_SIDES);
        break;
      case '--content-file':
        args.contentFile = parseVal(arg);
        break;
      case '--body-file':
        args.bodyFile = parseVal(arg);
        break;
      case '--result-file':
        args.resultFile = parseVal(arg);
        break;
      case '--result-head':
        args.resultHead = parseVal(arg);
        break;
      case '--allowed-heads-file':
        args.allowedHeadsFile = parseVal(arg);
        break;
      case '--threads-file':
        args.threadsFile = parseVal(arg);
        break;
      case '--actor':
        args.actor = parseVal(arg);
        break;
      case '--historical-comment-ids-file':
        args.historicalCommentIdsFile = parseVal(arg);
        break;
      case '--expected-result-sha256':
        args.expectedResultSha256 = parseVal(arg);
        break;
      case '--expected-recovery-sha256':
        args.expectedRecoverySha256 = parseVal(arg);
        break;
      case '--expected-threads-sha256':
        args.expectedThreadsSha256 = parseVal(arg);
        break;
      case '--blocker-file':
        args.blockerFile = parseVal(arg);
        break;
      case '--classification':
        args.classification = parseEnum(
          arg,
          parseVal(arg),
          SUPPORTED_CLASSIFICATIONS,
        );
        break;
      case '--file':
        args.file = parseVal(arg);
        break;
      case '--json-file':
        args.jsonFile = parseVal(arg);
        break;
      case '--author':
        args.author = parseEnum(arg, parseVal(arg), SUPPORTED_ENGINES);
        break;
      case '--reviewers': {
        args.reviewers = parseVal(arg);
        break;
      }
      default:
        fail(`unknown argument: ${arg}`);
    }
  }

  return args;
}

function validateArgs(args: CliArgs): void {
  if (args.protocolVersion) {
    return;
  }
  if (!args.command) {
    fail('subcommand required');
  }

  // Telemetry records an open engine token; every other command is bound to the
  // protocol enum, where an unknown engine claiming a clean pass would be a
  // forgery vector rather than a junk row.
  if (args.engineRaw !== undefined && args.command !== 'emit-telemetry') {
    args.engine = parseEnum('--engine', args.engineRaw, SUPPORTED_ENGINES);
  }

  for (const name of ['head', 'base', 'before', 'resultHead'] as const) {
    const val = args[name];
    if (val !== undefined) {
      requireSha(val, name === 'resultHead' ? 'result-head' : name);
    }
  }

  if (
    args.expectedResultSha256 !== undefined &&
    !SHA_64_RE.test(args.expectedResultSha256)
  ) {
    fail('--expected-result-sha256 must be a lowercase SHA-256 digest');
  }

  if (
    args.contentFile &&
    (args.command === 'post-finding' ||
      args.command === 'reopen-occurrence' ||
      args.command === 'dispose')
  ) {
    const required: Array<keyof CliArgs> = ['engine', 'round', 'fingerprint'];
    if (args.command === 'post-finding') {
      required.push('severity', 'lens');
    }
    const missing = required.filter((r) => args[r] === undefined);
    if (missing.length > 0) {
      const names = missing.map(
        (m) =>
          `--${String(m)
            .replace(/([A-Z])/g, '-$1')
            .toLowerCase()}`,
      );
      fail(`v3 content mode requires ${names.join(', ')}`);
    }
  }

  if (args.command === 'verify-ledger') {
    const resultFields: Array<keyof CliArgs> = [
      'engine',
      'round',
      'base',
      'before',
      'resultFile',
    ];
    const present = resultFields.map((f) => args[f] !== undefined);
    if (present.some(Boolean) && !present.every(Boolean)) {
      fail(
        'verify-ledger result evidence requires --engine, --round, --base, --before, and --result-file',
      );
    }
  }
}

/**
 * Parse argv, dispatch the requested subcommand, and return an exit code.
 */
function telemetryFailure(error: unknown): EmitTelemetryResult {
  return {
    emitted: false,
    sink: null,
    reference: null,
    idempotencyKey: null,
    error: error instanceof Error ? error.message : String(error),
  };
}

function runCliCommand(argv: string[]): number {
  const args = parseCliArgs(argv);

  if (args.version) {
    process.stdout.write(`${PACKAGE_VERSION}\n`);
    return 0;
  }

  if (args.protocolVersion) {
    process.stdout.write(`${PROTOCOL_VERSION}\n`);
    return 0;
  }

  validateArgs(args);

  switch (args.command) {
    case 'preflight-anchor': {
      if (!args.repo || args.pr === undefined || !args.head || !args.path) {
        fail('preflight-anchor requires --repo, --pr, --head, and --path');
      }
      if (args.line === undefined && !args.fileLevel) {
        fail('preflight-anchor requires --line or --file-level');
      }
      const out = preflightAnchor({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        path: args.path,
        line: args.line,
        fileLevel: args.fileLevel,
        side: args.side,
      });
      process.stdout.write(JSON.stringify(out) + '\n');
      break;
    }
    case 'post-finding': {
      if (!args.repo || args.pr === undefined || !args.head || !args.path) {
        fail('post-finding requires --repo, --pr, --head, and --path');
      }
      if (args.line === undefined && !args.fileLevel) {
        fail('post-finding requires --line or --file-level');
      }
      if (!args.contentFile && !args.bodyFile) {
        fail('post-finding requires --content-file or --body-file');
      }
      const out = postFinding({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        path: args.path,
        line: args.line,
        fileLevel: args.fileLevel,
        side: args.side,
        contentFile: args.contentFile,
        bodyFile: args.bodyFile,
        engine: args.engine,
        round: args.round,
        fingerprint: args.fingerprint,
        occurrence: args.occurrence,
        severity: args.severity,
        lens: args.lens,
      });
      process.stdout.write(JSON.stringify(out) + '\n');
      break;
    }
    case 'reopen-occurrence': {
      if (
        !args.repo ||
        args.pr === undefined ||
        !args.head ||
        !args.engine ||
        args.round === undefined ||
        !args.fingerprint ||
        args.occurrence === undefined ||
        !args.severity ||
        !args.lens ||
        args.commentId === undefined ||
        !args.threadId ||
        !args.contentFile
      ) {
        fail('reopen-occurrence missing required parameters');
      }
      const out = reopenOccurrence({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        engine: args.engine,
        round: args.round,
        fingerprint: args.fingerprint,
        occurrence: args.occurrence,
        severity: args.severity,
        lens: args.lens,
        commentId: args.commentId,
        threadId: args.threadId,
        contentFile: args.contentFile,
      });
      process.stdout.write(JSON.stringify(out) + '\n');
      break;
    }
    case 'dispose': {
      if (
        !args.repo ||
        args.pr === undefined ||
        !args.head ||
        !args.engine ||
        args.round === undefined ||
        !args.fingerprint ||
        !args.outcome ||
        args.commentId === undefined ||
        !args.threadId ||
        !args.contentFile
      ) {
        fail('dispose missing required parameters');
      }
      const out = dispose({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        engine: args.engine,
        round: args.round,
        fingerprint: args.fingerprint,
        occurrence: args.occurrence ?? 1,
        outcome: args.outcome,
        commentId: args.commentId,
        threadId: args.threadId,
        contentFile: args.contentFile,
      });
      process.stdout.write(JSON.stringify(out) + '\n');
      break;
    }
    case 'reply': {
      if (
        !args.repo ||
        args.pr === undefined ||
        !args.head ||
        args.commentId === undefined ||
        (!args.bodyFile && !args.contentFile)
      ) {
        fail(
          'reply requires --repo, --pr, --head, --comment-id, and --body-file or --content-file',
        );
      }
      const out = reply({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        commentId: args.commentId,
        bodyFile: args.bodyFile,
        contentFile: args.contentFile,
        engine: args.engine,
        round: args.round,
        fingerprint: args.fingerprint,
        outcome: args.outcome,
      });
      process.stdout.write(JSON.stringify(out) + '\n');
      break;
    }
    case 'post-pr-comment': {
      if (!args.repo || args.pr === undefined || !args.head || !args.bodyFile) {
        fail('post-pr-comment requires --repo, --pr, --head, and --body-file');
      }
      const out = postPrComment({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        bodyFile: args.bodyFile,
      });
      process.stdout.write(JSON.stringify(out) + '\n');
      break;
    }
    case 'validate-result': {
      if (
        !args.head ||
        !args.engine ||
        args.round === undefined ||
        !args.base ||
        !args.before ||
        !args.resultFile
      ) {
        fail('validate-result missing required arguments');
      }
      const out = validateResult({
        head: args.head,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultFile: args.resultFile,
        resultHead: args.resultHead,
      });
      writeSortedJson(out);
      break;
    }
    case 'recover-result':
    case 'write-result': {
      if (
        !args.head ||
        !args.engine ||
        args.round === undefined ||
        !args.base ||
        !args.before ||
        !args.resultFile ||
        !args.repo ||
        args.pr === undefined
      ) {
        fail('write-result missing required arguments');
      }
      const params = {
        head: args.head,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultFile: args.resultFile,
        repo: args.repo,
        pr: args.pr,
        allowedHeadsFile: args.allowedHeadsFile,
        actor: args.actor,
        historicalCommentIdsFile: args.historicalCommentIdsFile,
        classification: args.classification,
      };
      if (
        args.command === 'recover-result' &&
        (!args.expectedRecoverySha256 || args.classification !== undefined)
      ) {
        fail(
          'recover-result requires --expected-recovery-sha256 and reads classification from the saved candidate',
        );
      }
      const out =
        args.command === 'recover-result'
          ? recoverResult({
              ...params,
              expectedRecoverySha256: args.expectedRecoverySha256!,
            })
          : writeResult(params);
      writeSortedJson(out);
      break;
    }
    case 'write-blocked-result': {
      if (
        !args.head ||
        !args.engine ||
        args.round === undefined ||
        !args.base ||
        !args.before ||
        !args.resultFile ||
        !args.blockerFile
      ) {
        fail('write-blocked-result missing required arguments');
      }
      const out = writeBlockedResult({
        head: args.head,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultFile: args.resultFile,
        blockerFile: args.blockerFile,
      });
      writeSortedJson(out);
      break;
    }
    case 'finalize': {
      if (!args.repo || args.pr === undefined || !args.resultFile) {
        fail(
          'finalize requires --repo, --pr, and --result-file; reuse the original pre-pass historical-comment-ids file when present',
        );
      }
      if (
        args.head ||
        args.base ||
        args.before ||
        args.engine ||
        args.round !== undefined ||
        args.expectedResultSha256
      ) {
        fail(
          'finalize reads identity and digest from the saved result; use attest for explicit identity checks',
        );
      }
      const out = finalize({
        repo: args.repo,
        pr: args.pr,
        resultFile: args.resultFile,
        threadsFile: args.threadsFile,
        allowedHeadsFile: args.allowedHeadsFile,
        actor: args.actor,
        historicalCommentIdsFile: args.historicalCommentIdsFile,
        expectedThreadsSha256: args.expectedThreadsSha256,
        contentFile: args.contentFile,
      });
      process.stdout.write(JSON.stringify(out) + '\n');
      break;
    }
    case 'attest': {
      if (
        !args.repo ||
        args.pr === undefined ||
        !args.head ||
        !args.engine ||
        args.round === undefined ||
        !args.base ||
        !args.before ||
        !args.resultFile ||
        !args.expectedResultSha256
      ) {
        fail(
          'attest requires --repo, --pr, --head, --engine, --round, --base, --before, --result-file, and --expected-result-sha256',
        );
      }
      const out = attest({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultFile: args.resultFile,
        threadsFile: args.threadsFile,
        allowedHeadsFile: args.allowedHeadsFile,
        actor: args.actor,
        historicalCommentIdsFile: args.historicalCommentIdsFile,
        expectedResultSha256: args.expectedResultSha256,
        expectedThreadsSha256: args.expectedThreadsSha256,
        contentFile: args.contentFile,
      });
      process.stdout.write(JSON.stringify(out) + '\n');
      break;
    }
    case 'resolve': {
      if (!args.repo || args.pr === undefined || !args.head || !args.threadId) {
        fail('resolve requires --repo, --pr, --head, and --thread-id');
      }
      const out = resolve({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        threadId: args.threadId,
      });
      process.stdout.write(JSON.stringify(out) + '\n');
      break;
    }
    case 'reconcile': {
      if (
        !args.repo ||
        args.pr === undefined ||
        !args.head ||
        !args.fingerprint
      ) {
        fail('reconcile requires --repo, --pr, --head, and --fingerprint');
      }
      const out = reconcile({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        fingerprint: args.fingerprint,
      });
      writeSortedJson(out);
      break;
    }
    case 'verify-ledger': {
      if (!args.repo || args.pr === undefined || !args.head) {
        fail('verify-ledger requires --repo, --pr, and --head');
      }
      const out = verifyLedger({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        threadsFile: args.threadsFile,
        actor: args.actor,
        historicalCommentIdsFile: args.historicalCommentIdsFile,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultHead: args.resultHead,
        resultFile: args.resultFile,
        allowedHeadsFile: args.allowedHeadsFile,
        expectedThreadsSha256: args.expectedThreadsSha256,
      });
      writeSortedJson(out);
      break;
    }
    case 'post-roster': {
      if (
        !args.repo ||
        args.pr === undefined ||
        !args.head ||
        !args.author ||
        args.reviewers === undefined ||
        !args.contentFile
      ) {
        fail(
          'post-roster requires --repo, --pr, --head, --author, --reviewers, and --content-file',
        );
      }
      const out = postRoster({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        actor: args.actor,
        author: args.author,
        reviewers: parseReviewers(args.reviewers, args.author),
        content: readContent(args.contentFile),
      });
      writeSortedJson(out);
      break;
    }
    case 'read-roster': {
      if (!args.repo || args.pr === undefined) {
        fail('read-roster requires --repo and --pr');
      }
      const out = readRoster({
        repo: args.repo,
        pr: args.pr,
        actor: args.actor,
      });
      writeSortedJson(out);
      break;
    }
    case 'coverage':
    case 'verify-coverage': {
      if (!args.repo || args.pr === undefined || !args.head) {
        fail(`${args.command} requires --repo, --pr, and --head`);
      }
      const run = args.command === 'coverage' ? coverage : verifyCoverage;
      const out = run({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        actor: args.actor,
      });
      writeSortedJson(out);
      break;
    }
    case 'classify-changeset': {
      const report = resolveChangesetReport(args);
      writeSortedJson({
        ...report.changeset,
        skip: report.skip,
        reviewSignificantFiles: report.reviewSignificantFiles,
        classifications: report.classifications,
      });
      break;
    }
    case 'emit-telemetry': {
      // Emission never fails the pass that produced the record. A telemetry
      // defect must not block a review that found real defects, so every error
      // on this path is reported on stdout and the command still exits 0.
      let outcome: EmitTelemetryResult;
      try {
        if (!args.repo || args.pr === undefined || !args.engineRaw) {
          fail('emit-telemetry requires --repo, --pr, and --engine');
        }
        if (
          !args.passType ||
          !args.trigger ||
          !args.stance ||
          !args.status ||
          !args.tokenSource ||
          args.round === undefined ||
          !args.base ||
          !args.head
        ) {
          fail(
            'emit-telemetry requires --pass-type, --trigger, --stance, --status, --token-source, --round, --base, and --head',
          );
        }
        if (!args.findingsFile) {
          fail(
            'emit-telemetry requires --findings-file; omitted measurements are not zero findings',
          );
        }
        const changeset = args.changesetFile
          ? (readJsonFile(args.changesetFile, 'changeset file') as {
              changeset?: unknown;
            })
          : resolveChangesetReport(args).changeset;
        const record = buildTelemetryRecord({
          emittedAt: args.emittedAt ?? nowUtcSecond(),
          repo: args.repo,
          pr: args.pr,
          idempotencyKey: args.idempotencyKey,
          runId: args.telemetryRunId,
          engine: args.engineRaw,
          engineVersion: args.engineVersion ?? null,
          passType: args.passType,
          reviewTier: args.reviewTier ?? null,
          trigger: args.trigger,
          round: args.round,
          stance: args.stance,
          status: args.status,
          baseSha: args.base,
          headSha: args.head,
          promptStackSha256: args.promptStackSha256 ?? null,
          promptStackVersion: args.promptStackVersion ?? null,
          repoInstructionsSha256: args.repoInstructionsSha256 ?? null,
          tokenSource: args.tokenSource,
          tokens: args.tokensFile
            ? (readJsonArray(
                args.tokensFile,
                'tokens file',
              ) as TelemetryTokenBucketInput[])
            : [],
          lanes: args.lanesFile
            ? (readJsonArray(
                args.lanesFile,
                'lanes file',
              ) as TelemetryLaneInput[])
            : undefined,
          truncated: args.truncated === true,
          durationSeconds: parseDurationSeconds(args.durationSeconds),
          changeset: normalizeChangesetInput(changeset),
          findings: validateFindings(
            readJsonFile(args.findingsFile, 'findings file'),
          ),
        });

        if (args.dryRun) {
          process.stdout.write(buildTelemetryBody(record) + '\n');
          return 0;
        }

        outcome = emitTelemetry({
          record,
          sink: prCommentSink({
            repo: args.repo,
            pr: args.pr,
            actor: args.actor,
          }),
        });
      } catch (error) {
        outcome = telemetryFailure(error);
      }
      writeSortedJson(outcome);
      break;
    }
    case 'read-result': {
      const targetFile = args.file ?? args.resultFile;
      if (!targetFile) {
        fail('read-result requires --file or --result-file');
      }
      const out = readResult(targetFile);
      process.stdout.write(JSON.stringify(out, null, 2) + '\n');
      break;
    }
    case 'format-findings': {
      const jsonPath = args.jsonFile ?? args.file;
      if (!jsonPath) {
        fail('format-findings requires --json-file or --file');
      }
      assertRegularFile(
        jsonPath,
        'findings file must be a regular non-symlink file',
      );
      const parsed = parseJsonOrFail(
        readFileSync(jsonPath, 'utf8'),
        'findings file must contain valid UTF-8 JSON',
      );
      if (
        !Array.isArray(parsed) ||
        parsed.some(
          (row) =>
            typeof row !== 'object' || row === null || Array.isArray(row),
        )
      ) {
        fail('findings file must contain a JSON array of finding objects');
      }
      const formatted = formatFindings(parsed as ReviewFinding[]);
      process.stdout.write(formatted + '\n');
      break;
    }
    default:
      fail(`unknown command: ${args.command}`);
  }

  return 0;
}

/** Locate the command token without validating command-specific arguments. */
function commandFromArgv(argv: readonly string[]): string | undefined {
  const booleanFlags = new Set([
    '--protocol-version',
    '--version',
    '--file-level',
    '--truncated',
    '--dry-run',
  ]);
  for (let i = 0; i < argv.length; ) {
    const arg = argv[i]!;
    if (!arg.startsWith('-')) {
      return arg;
    }
    i += booleanFlags.has(arg) ? 1 : 2;
  }
  return undefined;
}

/** Parse argv, dispatch the requested subcommand, and return an exit code. */
export function runCli(argv: string[] = process.argv.slice(2)): number {
  resetGitHubRunner();
  if (commandFromArgv(argv) !== 'emit-telemetry') {
    return runCliCommand(argv);
  }
  try {
    return runCliCommand(argv);
  } catch (error) {
    writeSortedJson(telemetryFailure(error));
    return 0;
  }
}
