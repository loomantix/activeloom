import { createHash } from 'node:crypto';
import {
  CANONICAL_TOKEN_BUCKETS,
  OPEN_TOKEN_RE,
  PROVIDER_BUCKET_KEY_RE,
  REPO_RE,
  SHA_64_RE,
  SHA_RE,
  SUPPORTED_SEVERITIES,
  TELEMETRY_MARKER_PREFIX,
  TELEMETRY_PASS_TYPES,
  TELEMETRY_REVIEW_TIERS,
  TELEMETRY_STANCES,
  TELEMETRY_STATUSES,
  TELEMETRY_TOKEN_SOURCES,
  TELEMETRY_TRIGGERS,
  TELEMETRY_VERSION,
  TOKEN_RE,
  isCanonicalUtcTimestamp,
} from './constants.js';
import { errorMessage, fail, LedgerError } from './errors.js';
import {
  assertActor,
  assertLiveActor,
  deleteIssueComment,
  getIssueComments,
  getPostedCommentId,
  jsonOutput,
  verifyIssueComment,
} from './github.js';
import { parseJsonOrFail } from './io.js';
import type {
  AggregateTelemetryTokenBucket,
  BuildTelemetryParams,
  Changeset,
  ChangesetLines,
  EmitTelemetryResult,
  SupportedSeverity,
  TelemetryFindings,
  TelemetryFindingsInput,
  TelemetryLane,
  TelemetryLaneInput,
  TelemetryOutcomeCounts,
  TelemetryRecord,
  TelemetrySink,
  TelemetryTokenBucket,
  TelemetryTokenBucketInput,
  TelemetryPassType,
} from './types.js';

type ValidatedTelemetryTokenBucket =
  | TelemetryTokenBucket
  | AggregateTelemetryTokenBucket;

/** Report whether a comment body is a telemetry record of any version. */
export function isTelemetryComment(body: string): boolean {
  return body.includes(TELEMETRY_MARKER_PREFIX);
}

/** Drop telemetry records from a set of comment rows. */
export function excludeTelemetryComments<T extends { body?: unknown }>(
  rows: readonly T[],
): T[] {
  return rows.filter((row) => !isTelemetryComment(String(row.body ?? '')));
}

function requireEnum<T extends string>(
  value: unknown,
  allowed: readonly T[],
  field: string,
): T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) {
    fail(`telemetry ${field} must be one of: ${allowed.join(', ')}`);
  }
  return value as T;
}

function requireCount(value: unknown, field: string): number {
  if (
    typeof value !== 'number' ||
    !Number.isSafeInteger(value) ||
    value < 0 ||
    Object.is(value, -0)
  ) {
    fail(`telemetry ${field} must be a non-negative integer`);
  }
  return value;
}

/** Read a count that is allowed to be null (unmeasured). */
function requireNullableCount(value: unknown, field: string): number | null {
  if (value === null) {
    return null;
  }
  return requireCount(value, field);
}

function requireNullableToken(value: unknown, field: string): string | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== 'string' || !TOKEN_RE.test(value)) {
    fail(`telemetry ${field} must be a protocol token or null`);
  }
  return value;
}

function requireNullableSha256(value: unknown, field: string): string | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== 'string' || !SHA_64_RE.test(value)) {
    fail(`telemetry ${field} must be a lowercase SHA-256 digest or null`);
  }
  return value;
}

function requireObject(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    fail(`telemetry ${field} must be an object`);
  }
  return value as Record<string, unknown>;
}

function validateProviderBuckets(value: unknown): Record<string, number> {
  const source = requireObject(value, 'providerBuckets');
  const buckets: Record<string, number> = {};
  for (const [key, raw] of Object.entries(source)) {
    if (!PROVIDER_BUCKET_KEY_RE.test(key)) {
      fail(
        `telemetry providerBuckets key must match ${PROVIDER_BUCKET_KEY_RE}`,
      );
    }
    if (CANONICAL_TOKEN_BUCKETS.includes(key)) {
      fail(
        `telemetry providerBuckets must not restate the canonical bucket ${key}`,
      );
    }
    buckets[key] = requireCount(raw, `providerBuckets.${key}`);
  }
  return buckets;
}

function validateTokenBucket(
  value: unknown,
  allowUnknownModel = false,
): ValidatedTelemetryTokenBucket {
  const source = requireObject(value, 'tokens[]');
  const model = source['model'];
  if (
    !(allowUnknownModel && model === null) &&
    (typeof model !== 'string' || !TOKEN_RE.test(model))
  ) {
    fail('telemetry tokens[].model must be a protocol token');
  }
  return {
    model: model as string | null,
    effort: requireNullableToken(source['effort'], 'tokens[].effort'),
    input: requireNullableCount(source['input'], 'tokens[].input'),
    output: requireNullableCount(source['output'], 'tokens[].output'),
    cacheRead: requireNullableCount(source['cacheRead'], 'tokens[].cacheRead'),
    cacheWrite: requireNullableCount(
      source['cacheWrite'],
      'tokens[].cacheWrite',
    ),
    reasoning: requireNullableCount(source['reasoning'], 'tokens[].reasoning'),
    providerBuckets: validateProviderBuckets(source['providerBuckets']),
  } as ValidatedTelemetryTokenBucket;
}

function validateLane(value: unknown): TelemetryLane {
  const source = requireObject(value, 'lanes[]');
  const lens = source['lens'];
  if (typeof lens !== 'string' || !TOKEN_RE.test(lens)) {
    fail('telemetry lanes[].lens must be a protocol token');
  }
  return {
    lens,
    model: requireNullableToken(source['model'], 'lanes[].model'),
    input: requireNullableCount(source['input'], 'lanes[].input'),
    output: requireNullableCount(source['output'], 'lanes[].output'),
    cacheRead: requireNullableCount(source['cacheRead'], 'lanes[].cacheRead'),
    cacheWrite: requireNullableCount(
      source['cacheWrite'],
      'lanes[].cacheWrite',
    ),
    reasoning: requireNullableCount(source['reasoning'], 'lanes[].reasoning'),
  };
}

function validateChangeset(value: unknown): Changeset {
  const source = requireObject(value, 'changeset');
  const files = requireObject(source['files'], 'changeset.files');
  const lines = requireObject(source['linesChanged'], 'changeset.linesChanged');
  const byLanguage = requireObject(
    source['linesByLanguage'],
    'changeset.linesByLanguage',
  );

  const linesChanged: ChangesetLines = {
    app: requireCount(lines['app'], 'changeset.linesChanged.app'),
    test: requireCount(lines['test'], 'changeset.linesChanged.test'),
    comment: requireNullableCount(
      lines['comment'],
      'changeset.linesChanged.comment',
    ),
    docsConfig: requireCount(
      lines['docsConfig'],
      'changeset.linesChanged.docsConfig',
    ),
    generated: requireCount(
      lines['generated'],
      'changeset.linesChanged.generated',
    ),
    blank: requireCount(lines['blank'], 'changeset.linesChanged.blank'),
  };

  const languages: Record<string, number> = {};
  for (const [key, raw] of Object.entries(byLanguage)) {
    if (!OPEN_TOKEN_RE.test(key)) {
      fail(
        `telemetry changeset.linesByLanguage key must match ${OPEN_TOKEN_RE}`,
      );
    }
    languages[key] = requireCount(raw, `changeset.linesByLanguage.${key}`);
  }

  const validated: Changeset = {
    classifierVersion: requireCount(
      source['classifierVersion'],
      'changeset.classifierVersion',
    ),
    reviewSignificantFiles: requireCount(
      source['reviewSignificantFiles'],
      'changeset.reviewSignificantFiles',
    ),
    files: {
      app: requireCount(files['app'], 'changeset.files.app'),
      test: requireCount(files['test'], 'changeset.files.test'),
      docsConfig: requireCount(
        files['docsConfig'],
        'changeset.files.docsConfig',
      ),
      generated: requireCount(files['generated'], 'changeset.files.generated'),
    },
    linesChanged,
    linesByLanguage: languages,
  };
  const totalFiles = Object.values(validated.files).reduce(
    (total, count) => total + count,
    0,
  );
  const requiredReviewFiles = validated.files.app + validated.files.test;
  if (
    !Number.isSafeInteger(totalFiles) ||
    !Number.isSafeInteger(requiredReviewFiles)
  ) {
    fail('telemetry changeset file totals must be safe integers');
  }
  if (
    validated.reviewSignificantFiles < requiredReviewFiles ||
    validated.reviewSignificantFiles > totalFiles
  ) {
    fail(
      'telemetry changeset.reviewSignificantFiles must cover app/test files and not exceed total files',
    );
  }
  return validated;
}

/** Validate a complete finding measurement without supplying missing counts. */
export function validateFindings(value: unknown): TelemetryFindings {
  const source = requireObject(value, 'findings');
  const ladder = requireObject(
    source['bySeverityAndOutcome'],
    'findings.bySeverityAndOutcome',
  );
  const unknown = Object.keys(ladder).filter(
    (key) => !SUPPORTED_SEVERITIES.includes(key as SupportedSeverity),
  );
  if (unknown.length > 0) {
    fail(
      `telemetry findings.bySeverityAndOutcome must use the severity ladder: ${SUPPORTED_SEVERITIES.join(', ')}`,
    );
  }

  const bySeverityAndOutcome = {} as Record<
    SupportedSeverity,
    TelemetryOutcomeCounts
  >;
  let counted = 0;
  for (const severity of SUPPORTED_SEVERITIES) {
    const row = requireObject(
      ladder[severity],
      `findings.bySeverityAndOutcome.${severity}`,
    );
    const outcomes: TelemetryOutcomeCounts = {
      validFixed: requireCount(
        row['validFixed'],
        `findings.bySeverityAndOutcome.${severity}.validFixed`,
      ),
      validDeferred: requireCount(
        row['validDeferred'],
        `findings.bySeverityAndOutcome.${severity}.validDeferred`,
      ),
      invalidDismissed: requireCount(
        row['invalidDismissed'],
        `findings.bySeverityAndOutcome.${severity}.invalidDismissed`,
      ),
    };
    counted +=
      outcomes.validFixed + outcomes.validDeferred + outcomes.invalidDismissed;
    bySeverityAndOutcome[severity] = outcomes;
  }

  const posted = requireCount(source['posted'], 'findings.posted');
  if (counted > posted) {
    fail('telemetry findings dispositions exceed the findings posted');
  }

  return {
    posted,
    bySeverityAndOutcome,
    chainInducedRegressions: requireCount(
      source['chainInducedRegressions'],
      'findings.chainInducedRegressions',
    ),
  };
}

/** Derive the idempotency key for telemetry deduplication. */
export function telemetryIdempotencyKey(fields: {
  repo: string;
  pr: number;
  engine: string;
  passType: TelemetryPassType;
  round: number;
  headSha: string;
  runId?: string | undefined;
}): string {
  const fieldsForKey = [
    fields.repo,
    String(fields.pr),
    fields.engine,
    fields.passType,
    String(fields.round),
    fields.headSha,
  ];
  if (fields.runId !== undefined) {
    if (!SHA_64_RE.test(fields.runId))
      fail('telemetry runId must be a lowercase SHA-256 digest');
    return (
      'run:' +
      createHash('sha256')
        .update(JSON.stringify([fields.runId, ...fieldsForKey]))
        .digest('hex')
    );
  }
  return fieldsForKey.join(':');
}

/** Validate an unknown value as a telemetry record. */
export function validateTelemetryRecord(value: unknown): TelemetryRecord {
  const source = requireObject(value, 'record');

  if (source['version'] !== TELEMETRY_VERSION && source['version'] !== 3) {
    fail('telemetry record version must be 1 or 3');
  }
  const emittedAt = source['emittedAt'];
  if (typeof emittedAt !== 'string' || !isCanonicalUtcTimestamp(emittedAt)) {
    fail('telemetry emittedAt must be an RFC 3339 UTC timestamp');
  }
  const repo = source['repo'];
  if (typeof repo !== 'string' || !REPO_RE.test(repo)) {
    fail('telemetry repo must be owner/name');
  }
  const pr = source['pr'];
  if (typeof pr !== 'number' || !Number.isSafeInteger(pr) || pr < 1) {
    fail('telemetry pr must be a positive integer');
  }
  const engine = source['engine'];
  if (typeof engine !== 'string' || !OPEN_TOKEN_RE.test(engine)) {
    fail(`telemetry engine must match ${OPEN_TOKEN_RE}`);
  }
  const round = source['round'];
  if (typeof round !== 'number' || !Number.isSafeInteger(round) || round < 1) {
    fail('telemetry round must be a positive integer');
  }
  const baseSha = source['baseSha'];
  const headSha = source['headSha'];
  if (
    typeof baseSha !== 'string' ||
    !SHA_RE.test(baseSha) ||
    typeof headSha !== 'string' ||
    !SHA_RE.test(headSha)
  ) {
    fail('telemetry baseSha and headSha must be full commit SHAs');
  }
  const truncated = source['truncated'];
  if (typeof truncated !== 'boolean') {
    fail('telemetry truncated must be a boolean');
  }
  const durationSeconds = source['durationSeconds'];
  if (
    durationSeconds !== null &&
    (typeof durationSeconds !== 'number' ||
      !Number.isFinite(durationSeconds) ||
      durationSeconds < 0)
  ) {
    fail('telemetry durationSeconds must be a non-negative number or null');
  }

  const passType = requireEnum(
    source['passType'],
    TELEMETRY_PASS_TYPES,
    'passType',
  );
  const status = requireEnum(source['status'], TELEMETRY_STATUSES, 'status');
  const tokenSource = requireEnum(
    source['tokenSource'],
    TELEMETRY_TOKEN_SOURCES,
    'tokenSource',
  );

  const rawTokens = source['tokens'];
  if (!Array.isArray(rawTokens)) {
    fail('telemetry tokens must be an array');
  }
  const tokens = rawTokens.map((bucket) =>
    validateTokenBucket(bucket, source['version'] === 3),
  );
  if (
    source['version'] === 3 &&
    (tokens.length !== 1 ||
      tokens[0]?.model !== null ||
      tokens[0].effort !== null)
  ) {
    fail(
      'unattributed telemetry must be one aggregate bucket with unknown effort',
    );
  }
  const models = tokens.map((bucket) =>
    JSON.stringify([bucket.model, bucket.effort]),
  );
  if (new Set(models).size !== models.length) {
    fail('telemetry tokens must carry one bucket per model and effort');
  }
  if (tokenSource === 'unavailable' && tokens.length > 0) {
    fail('telemetry tokenSource unavailable cannot carry token buckets');
  }
  if (tokenSource !== 'unavailable' && tokens.length === 0) {
    fail('telemetry with no token buckets must record tokenSource unavailable');
  }

  const rawLanes = source['lanes'];
  if (rawLanes !== undefined && !Array.isArray(rawLanes)) {
    fail('telemetry lanes must be an array when present');
  }
  if (Array.isArray(rawLanes) && rawLanes.length === 0) {
    fail('telemetry lanes must be absent rather than empty');
  }
  const lanes = Array.isArray(rawLanes)
    ? rawLanes.map(validateLane)
    : undefined;

  const changeset = validateChangeset(source['changeset']);
  if (status === 'skipped' && changeset.reviewSignificantFiles > 0) {
    fail('a skipped pass cannot carry review-significant files');
  }

  const idempotencyKey = source['idempotencyKey'];
  if (typeof idempotencyKey !== 'string' || !TOKEN_RE.test(idempotencyKey)) {
    fail('telemetry idempotencyKey must be a protocol token');
  }

  const validated: Record<string, unknown> = {
    ...source,
    version: source['version'],
    emittedAt,
    repo,
    pr,
    idempotencyKey,
    engine,
    engineVersion: requireNullableToken(
      source['engineVersion'],
      'engineVersion',
    ),
    passType,
    reviewTier:
      source['reviewTier'] === null
        ? null
        : requireEnum(
            source['reviewTier'],
            TELEMETRY_REVIEW_TIERS,
            'reviewTier',
          ),
    trigger: requireEnum(source['trigger'], TELEMETRY_TRIGGERS, 'trigger'),
    round,
    stance: requireEnum(source['stance'], TELEMETRY_STANCES, 'stance'),
    status,
    baseSha,
    headSha,
    promptStackSha256: requireNullableSha256(
      source['promptStackSha256'],
      'promptStackSha256',
    ),
    promptStackVersion: requireNullableToken(
      source['promptStackVersion'],
      'promptStackVersion',
    ),
    repoInstructionsSha256: requireNullableSha256(
      source['repoInstructionsSha256'],
      'repoInstructionsSha256',
    ),
    tokenSource,
    tokens,
    ...(lanes === undefined ? {} : { lanes }),
    truncated,
    durationSeconds: durationSeconds as number | null,
    changeset,
    findings: validateFindings(source['findings']),
  };
  return validated as unknown as TelemetryRecord;
}

function tokenBucketFrom(
  bucket: TelemetryTokenBucketInput,
): ValidatedTelemetryTokenBucket {
  return validateTokenBucket(
    {
      model: bucket.model,
      effort: bucket.effort ?? null,
      input: bucket.input ?? null,
      output: bucket.output ?? null,
      cacheRead: bucket.cacheRead ?? null,
      cacheWrite: bucket.cacheWrite ?? null,
      reasoning: bucket.reasoning ?? null,
      providerBuckets: bucket.providerBuckets ?? {},
    },
    true,
  );
}

function laneFrom(lane: TelemetryLaneInput): TelemetryLane {
  return validateLane({
    lens: lane.lens,
    model: lane.model ?? null,
    input: lane.input ?? null,
    output: lane.output ?? null,
    cacheRead: lane.cacheRead ?? null,
    cacheWrite: lane.cacheWrite ?? null,
    reasoning: lane.reasoning ?? null,
  });
}

function findingsFrom(
  findings: TelemetryFindingsInput | undefined,
): TelemetryFindings {
  const ladder: Record<string, TelemetryOutcomeCounts> = {};
  for (const severity of SUPPORTED_SEVERITIES) {
    const row = findings?.bySeverityAndOutcome?.[severity];
    ladder[severity] = {
      validFixed: row?.validFixed ?? 0,
      validDeferred: row?.validDeferred ?? 0,
      invalidDismissed: row?.invalidDismissed ?? 0,
    };
  }
  return validateFindings({
    posted: findings?.posted ?? 0,
    bySeverityAndOutcome: ladder,
    chainInducedRegressions: findings?.chainInducedRegressions ?? 0,
  });
}

/** Assemble a validated telemetry record from computed metrics. */
export function buildTelemetryRecord(
  params: BuildTelemetryParams,
): TelemetryRecord {
  const tokens = (params.tokens ?? []).map(tokenBucketFrom);
  const lanes = params.lanes?.map(laneFrom);
  const idempotencyKey =
    params.idempotencyKey ??
    telemetryIdempotencyKey({
      repo: params.repo,
      pr: params.pr,
      engine: params.engine,
      passType: params.passType,
      round: params.round,
      headSha: params.headSha,
      runId: params.runId,
    });

  return validateTelemetryRecord({
    version: tokens.some((bucket) => bucket.model === null)
      ? 3
      : TELEMETRY_VERSION,
    emittedAt: params.emittedAt,
    repo: params.repo,
    pr: params.pr,
    idempotencyKey,
    engine: params.engine,
    engineVersion: params.engineVersion ?? null,
    passType: params.passType,
    reviewTier: params.reviewTier ?? null,
    trigger: params.trigger,
    round: params.round,
    stance: params.stance,
    status: params.status,
    baseSha: params.baseSha,
    headSha: params.headSha,
    promptStackSha256: params.promptStackSha256 ?? null,
    promptStackVersion: params.promptStackVersion ?? null,
    repoInstructionsSha256: params.repoInstructionsSha256 ?? null,
    tokenSource: params.tokenSource,
    tokens,
    ...(lanes === undefined ? {} : { lanes }),
    truncated: params.truncated,
    durationSeconds: params.durationSeconds ?? null,
    changeset: params.changeset,
    findings: findingsFrom(params.findings),
  });
}

/** Project a validated reader record onto its public-safe writer schema. */
function knownTelemetryRecord(value: unknown): TelemetryRecord {
  const record = validateTelemetryRecord(value);
  const fields = {
    emittedAt: record.emittedAt,
    repo: record.repo,
    pr: record.pr,
    idempotencyKey: record.idempotencyKey,
    engine: record.engine,
    engineVersion: record.engineVersion,
    passType: record.passType,
    reviewTier: record.reviewTier,
    trigger: record.trigger,
    round: record.round,
    stance: record.stance,
    status: record.status,
    baseSha: record.baseSha,
    headSha: record.headSha,
    promptStackSha256: record.promptStackSha256,
    promptStackVersion: record.promptStackVersion,
    repoInstructionsSha256: record.repoInstructionsSha256,
    tokenSource: record.tokenSource,
    tokens: record.tokens,
    ...(!record.lanes ? {} : { lanes: record.lanes }),
    truncated: record.truncated,
    durationSeconds: record.durationSeconds,
    changeset: record.changeset,
    findings: record.findings,
  };
  return record.version === 1
    ? { ...fields, version: 1, tokens: record.tokens }
    : { ...fields, version: 3, tokens: record.tokens };
}

/** Render a record as the comment body that carries it. */
export function buildTelemetryBody(record: TelemetryRecord): string {
  const safeRecord = knownTelemetryRecord(record);
  return [
    `<!-- local-review-telemetry:v${safeRecord.version} -->`,
    '',
    '```json',
    JSON.stringify(safeRecord, null, 2),
    '```',
  ].join('\n');
}

/** Parse the telemetry record out of a comment body, or return null. */
export function matchTelemetry(body: string): TelemetryRecord | null {
  if (!isTelemetryComment(body)) {
    return null;
  }
  const prefixIndex = body.indexOf(TELEMETRY_MARKER_PREFIX);
  if (prefixIndex !== body.lastIndexOf(TELEMETRY_MARKER_PREFIX)) {
    fail('a comment carries more than one local-review telemetry marker');
  }
  const marker = body.match(/<!-- local-review-telemetry:v([13]) -->/);
  if (!marker || marker.index === undefined) {
    fail('local-review telemetry record is of an unsupported version');
  }
  const payload = body
    .slice(marker.index + marker[0].length)
    .replace(/^\s*```(?:json)?\s*\n/, '')
    .replace(/\n```\s*$/, '')
    .trim();
  if (payload === '') {
    fail('local-review telemetry record carries no payload');
  }
  const record = validateTelemetryRecord(
    parseJsonOrFail(
      payload,
      'local-review telemetry payload is not valid JSON',
    ),
  );
  if (record.version !== Number(marker[1]))
    fail('telemetry marker and payload versions must match');
  return record;
}

function canonicalJson(value: unknown): string {
  if (value === undefined) {
    return 'undefined';
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(',')}]`;
  }
  if (typeof value === 'object' && value !== null) {
    const source = value as Record<string, unknown>;
    return `{${Object.keys(source)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(source[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
}

function replayFingerprint(record: TelemetryRecord): string {
  const known = knownTelemetryRecord(record);
  return canonicalJson({
    ...known,
    emittedAt: null,
    tokens: [...known.tokens].sort((left, right) =>
      canonicalJson(left).localeCompare(canonicalJson(right)),
    ),
    lanes: known.lanes
      ? [...known.lanes].sort((left, right) =>
          canonicalJson(left).localeCompare(canonicalJson(right)),
        )
      : undefined,
  });
}

/** Give a record with no measured duration the other record's duration. */
function withDurationFrom(
  record: TelemetryRecord,
  other: TelemetryRecord,
): TelemetryRecord {
  return record.durationSeconds === null
    ? { ...record, durationSeconds: other.durationSeconds }
    : record;
}

/**
 * Report whether two stored records are replays of one pass rather than
 * conflicting claims. An unmeasured duration matches any measured one.
 */
export function isTelemetryReplay(
  left: TelemetryRecord,
  right: TelemetryRecord,
): boolean {
  return (
    replayFingerprint(withDurationFrom(left, right)) ===
    replayFingerprint(withDurationFrom(right, left))
  );
}

/** PR comment sink: one comment per review pass. */
export function prCommentSink(target: {
  repo: string;
  pr: number;
  actor?: string;
}): TelemetrySink {
  return {
    name: 'pr-comment',
    emit({ record, body }) {
      const actor = assertActor(target.actor);
      const rows = getIssueComments(target.repo, target.pr, actor);
      for (const row of rows) {
        const existing = String(row['body'] ?? '');
        if (!isTelemetryComment(existing)) {
          continue;
        }
        let parsed: TelemetryRecord | null;
        try {
          parsed = matchTelemetry(existing);
        } catch {
          continue;
        }
        if (parsed?.idempotencyKey === record.idempotencyKey) {
          const replay = withDurationFrom(record, parsed);
          if (replayFingerprint(parsed) !== replayFingerprint(replay)) {
            fail('telemetry idempotency key conflicts with an existing record');
          }
          return { sink: 'pr-comment', reference: String(row['id'] ?? '') };
        }
      }

      assertLiveActor(actor);
      const response = jsonOutput(
        [
          'api',
          '-X',
          'POST',
          `repos/${target.repo}/issues/${target.pr}/comments`,
        ],
        { body },
      );
      const commentId = getPostedCommentId(response);
      try {
        verifyIssueComment(target.repo, commentId, body, actor);
      } catch (error) {
        try {
          deleteIssueComment(target.repo, target.pr, commentId);
        } catch (rollbackError) {
          throw new LedgerError(
            `telemetry verification failed and rollback could not be verified: ` +
              `${errorMessage(rollbackError)}; ` +
              `comment ${commentId} on ${target.repo}#${target.pr} may remain ` +
              `on the pull request; verification failed with: ${errorMessage(error)}`,
            { cause: error },
          );
        }
        throw error;
      }
      return { sink: 'pr-comment', reference: String(commentId) };
    },
  };
}

/** Emit a record through a sink, reporting failure instead of raising it. */
export function emitTelemetry(params: {
  record: TelemetryRecord;
  sink: TelemetrySink;
}): EmitTelemetryResult {
  const { record, sink } = params;
  try {
    const body = buildTelemetryBody(record);
    const result = sink.emit({ record, body });
    return {
      emitted: true,
      sink: result.sink,
      reference: result.reference,
      idempotencyKey: record.idempotencyKey,
      error: null,
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return {
      emitted: false,
      sink: sink.name,
      reference: null,
      idempotencyKey: record.idempotencyKey,
      error: message,
    };
  }
}

/** Add an observed launcher duration without replacing a pass's other measurements. */
export function enrichTelemetryDuration(params: {
  repo: string;
  pr: number;
  actor?: string | undefined;
  idempotencyKey: string;
  engine: string;
  round: number;
  base: string;
  head: string;
  durationSeconds: number;
}): EmitTelemetryResult {
  const result = {
    sink: 'pr-comment',
    reference: null,
    idempotencyKey: params.idempotencyKey,
  };
  try {
    if (
      !Number.isFinite(params.durationSeconds) ||
      params.durationSeconds < 0
    ) {
      fail('telemetry duration must be a finite non-negative number');
    }
    const actor = assertActor(params.actor);
    const matches = getIssueComments(params.repo, params.pr, actor).flatMap(
      (row) => {
        const body = String(row['body'] ?? '');
        let record: TelemetryRecord | null;
        try {
          record = matchTelemetry(body);
        } catch {
          return [];
        }
        return record?.idempotencyKey === params.idempotencyKey
          ? [{ row, body, record }]
          : [];
      },
    );
    if (matches.length !== 1)
      fail(
        'duration enrichment requires exactly one existing telemetry record',
      );
    const { row, body: original, record } = matches[0]!;
    if (
      record.repo !== params.repo ||
      record.pr !== params.pr ||
      record.engine !== params.engine ||
      record.passType !== 'review' ||
      record.round !== params.round ||
      record.baseSha !== params.base ||
      record.headSha !== params.head
    ) {
      fail('duration enrichment does not match the recorded pass identity');
    }
    const commentId = getPostedCommentId(row);
    if (record.durationSeconds === null) {
      // Preserve unknown additive payload fields; change only the missing measurement.
      const marker = `<!-- local-review-telemetry:v${record.version} -->`;
      const prefix = original.slice(
        0,
        original.indexOf(marker) + marker.length,
      );
      const payload = JSON.parse(
        original
          .slice(prefix.length)
          .replace(/^\s*```(?:json)?\s*\n/, '')
          .replace(/\n```\s*$/, '')
          .trim(),
      ) as Record<string, unknown>;
      payload['durationSeconds'] = params.durationSeconds;
      validateTelemetryRecord(payload);
      const body = `${prefix}\n\n\`\`\`json\n${JSON.stringify(payload, null, 2)}\n\`\`\``;
      verifyIssueComment(params.repo, commentId, original, actor);
      assertLiveActor(actor);
      jsonOutput(
        [
          'api',
          '-X',
          'PATCH',
          `repos/${params.repo}/issues/comments/${commentId}`,
        ],
        { body },
      );
      verifyIssueComment(params.repo, commentId, body, actor);
    }
    return {
      ...result,
      emitted: true,
      reference: String(commentId),
      error: null,
    };
  } catch (error) {
    return { ...result, emitted: false, error: errorMessage(error) };
  }
}
