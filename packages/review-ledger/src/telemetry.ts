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
  TELEMETRY_V1_MARKER,
  TELEMETRY_VERSION,
  TOKEN_RE,
  UTC_TIMESTAMP_RE,
} from './constants.js';
import { fail, LedgerError } from './errors.js';
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

/**
 * Report whether a comment body is a telemetry record of any version.
 *
 * The check is on the shared prefix rather than on a known marker, so a record
 * type added later is excluded from reviewer context by default instead of
 * leaking into it until a reader is taught to filter it.
 */
export function isTelemetryComment(body: string): boolean {
  return body.includes(TELEMETRY_MARKER_PREFIX);
}

/**
 * Drop telemetry records from a set of comment rows.
 *
 * A pass must never read prior telemetry. Visible history and a readable trend
 * are what turn a cost measurement into a target to optimise toward, and an
 * agent optimising its own measured cost stops optimising the review.
 */
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

/**
 * Read a count that is allowed to be absent, where absent means unmeasured.
 *
 * `null` and `0` are different answers and this is the boundary that keeps them
 * apart. `undefined` is rejected rather than coerced: `JSON.stringify` drops an
 * undefined value entirely, so accepting one here would let a missing
 * measurement serialise into a record that simply lacks the field, which the
 * next reader would have no way to tell from a zero.
 */
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

function validateTokenBucket(value: unknown): TelemetryTokenBucket {
  const source = requireObject(value, 'tokens[]');
  const model = source['model'];
  if (typeof model !== 'string' || !TOKEN_RE.test(model)) {
    fail('telemetry tokens[].model must be a protocol token');
  }
  return {
    model,
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
  };
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

/**
 * Derive the key aggregation dedupes on.
 *
 * A retried or replayed marker must not double-count, and a metric that
 * silently double-counts on a network retry fails in the direction of looking
 * more expensive than it was.
 *
 * `passType` is part of the identity because a refactor pass that changed
 * nothing leaves the head where it was, so the same engine's next review round
 * at that head would otherwise collide with it and one of the two records would
 * be dropped as a replay.
 */
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

/**
 * Validate an unknown value as a telemetry record.
 *
 * Unknown top-level keys are preserved rather than rejected. The payload is
 * versioned JSON precisely so a field added later is additive: a reader that
 * fails closed on an unrecognised key would force a package release and a
 * fleet-wide re-vendor for every new field, which is the rigidity this record
 * exists to avoid. Writers do not rely on that tolerance — they go through
 * `buildTelemetryRecord`, which only ever emits known fields.
 */
export function validateTelemetryRecord(value: unknown): TelemetryRecord {
  const source = requireObject(value, 'record');

  if (source['version'] !== TELEMETRY_VERSION) {
    fail(`telemetry record version must be ${TELEMETRY_VERSION}`);
  }
  const emittedAt = source['emittedAt'];
  if (
    typeof emittedAt !== 'string' ||
    !UTC_TIMESTAMP_RE.test(emittedAt) ||
    Number.isNaN(Date.parse(emittedAt)) ||
    new Date(emittedAt).toISOString().replace('.000Z', 'Z') !== emittedAt
  ) {
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
  const tokens = rawTokens.map(validateTokenBucket);
  const models = tokens.map((bucket) =>
    JSON.stringify([bucket.model, bucket.effort]),
  );
  if (new Set(models).size !== models.length) {
    fail('telemetry tokens must carry one bucket per model and effort');
  }
  // An engine that reported nothing must not serialise as zero tokens. A zero
  // would make that engine look free and skew every fleet average in its
  // favour — the kind of defect that survives because the dashboard still
  // looks plausible.
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
  // A skip still burns tokens reading and classifying the pull request, so it
  // is recorded; what it cannot have is reviewable work, since that is what
  // made it a skip.
  if (status === 'skipped' && changeset.reviewSignificantFiles > 0) {
    fail('a skipped pass cannot carry review-significant files');
  }

  const idempotencyKey = source['idempotencyKey'];
  if (typeof idempotencyKey !== 'string' || !TOKEN_RE.test(idempotencyKey)) {
    fail('telemetry idempotencyKey must be a protocol token');
  }

  const validated: Record<string, unknown> = {
    ...source,
    version: TELEMETRY_VERSION,
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
  // The cast is over the unknown-keyed carrier, not over the contract: every
  // field the interface declares has just been checked above, and the extras
  // that survive are the forward-compatible ones a newer writer added.
  return validated as unknown as TelemetryRecord;
}

function tokenBucketFrom(
  bucket: TelemetryTokenBucketInput,
): TelemetryTokenBucket {
  return validateTokenBucket({
    model: bucket.model,
    effort: bucket.effort ?? null,
    input: bucket.input ?? null,
    output: bucket.output ?? null,
    cacheRead: bucket.cacheRead ?? null,
    cacheWrite: bucket.cacheWrite ?? null,
    reasoning: bucket.reasoning ?? null,
    providerBuckets: bucket.providerBuckets ?? {},
  });
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

/**
 * Assemble a validated telemetry record from already-computed numbers.
 *
 * Every measurement arrives as an argument. This helper never reads a session
 * transcript, a home directory, or any other ambient state: a package vendored
 * into every consumer that read transcripts would be a materially different
 * trust proposition, since those transcripts hold every file read and command
 * run. Each engine extracts its own usage and passes it in.
 *
 * Finding counts default to zero because a pass genuinely posts zero findings;
 * token counts never default, because an unmeasured bucket is not a zero.
 */
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
    version: TELEMETRY_VERSION,
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

/** Project a validated reader record onto the public-safe v1 writer schema. */
function knownTelemetryRecord(value: unknown): TelemetryRecord {
  const record = validateTelemetryRecord(value);
  return {
    version: record.version,
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
}

/**
 * Render a record as the comment body that carries it.
 *
 * The payload travels as JSON in a fenced block rather than as inline marker
 * attributes. Regex-parsed attributes would need a parser release and a fleet
 * re-vendor for every new field; JSON additions are additive and an older
 * reader ignores what it does not know.
 */
export function buildTelemetryBody(record: TelemetryRecord): string {
  const safeRecord = knownTelemetryRecord(record);
  return [
    TELEMETRY_V1_MARKER,
    '',
    '```json',
    JSON.stringify(safeRecord, null, 2),
    '```',
  ].join('\n');
}

/**
 * Parse the telemetry record out of a comment body, or return null.
 *
 * A direct parse of a body carrying a telemetry marker fails rather than
 * reading as absent. Sinks may skip malformed replay candidates so an older
 * bad record cannot prevent a new pass from emitting valid telemetry.
 */
export function matchTelemetry(body: string): TelemetryRecord | null {
  if (!isTelemetryComment(body)) {
    return null;
  }
  const prefixIndex = body.indexOf(TELEMETRY_MARKER_PREFIX);
  if (prefixIndex !== body.lastIndexOf(TELEMETRY_MARKER_PREFIX)) {
    fail('a comment carries more than one local-review telemetry marker');
  }
  const markerIndex = body.indexOf(TELEMETRY_V1_MARKER);
  if (markerIndex === -1) {
    fail('local-review telemetry record is of an unsupported version');
  }
  const payload = body
    .slice(markerIndex + TELEMETRY_V1_MARKER.length)
    .replace(/^\s*```(?:json)?\s*\n/, '')
    .replace(/\n```\s*$/, '')
    .trim();
  if (payload === '') {
    fail('local-review telemetry record carries no payload');
  }
  return validateTelemetryRecord(
    parseJsonOrFail(
      payload,
      'local-review telemetry payload is not valid JSON',
    ),
  );
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

/**
 * Describe a thrown value without assuming it is shaped like an `Error`.
 *
 * A cast to `{ message?: string }` throws a `TypeError` on a thrown `null`,
 * which would destroy the error being reported from inside its own catch.
 */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * The reference sink: one new comment per pass on the pull request.
 *
 * The pull request is the emission point because it is the only store this
 * protocol can assume. Requiring an external service to record a data point
 * would put an account, an API key, and a push path in front of adoption;
 * external sinks are consumers of these markers, never dependencies for
 * writing them.
 *
 * One comment per pass rather than one edited comment per engine keeps the
 * immutable history readable. Malformed replay candidates are ignored so they
 * cannot poison later emission.
 */
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
        if (!existing.includes(TELEMETRY_V1_MARKER)) {
          continue;
        }
        let parsed: TelemetryRecord | null;
        try {
          parsed = matchTelemetry(existing);
        } catch {
          continue;
        }
        if (parsed?.idempotencyKey === record.idempotencyKey) {
          if (replayFingerprint(parsed) !== replayFingerprint(record)) {
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
          // This is the one branch that leaves GitHub durably mutated, so the
          // message must name the residue and why verification failed. The
          // cause chain alone is not enough: `emitTelemetry` reports
          // `error.message` and never walks it.
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

/**
 * Emit a record through a sink, reporting failure instead of raising it.
 *
 * A telemetry write that fails is logged and skipped. It must never block or
 * fail a review that found real defects, which is also why this record is a
 * separate marker rather than an extension of the attestation whose body is
 * byte-verified and hash-checked.
 */
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
