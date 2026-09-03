#!/usr/bin/env node
// Extract this engine's own token usage for one review pass.
//
// The ledger package takes numbers as arguments and never reads a session
// transcript or any path under a home directory: a package vendored into every
// consumer that read transcripts would be a materially different trust
// proposition, since those transcripts hold every file read and every command
// run. Engine-specific extraction therefore lives here, in the engine's own
// skill, where it can break on a CLI release without dragging a sha512-pinned
// bundle with it.
//
// Two modes:
//
//   snapshot --out <file>
//       Record where the session log stands before the pass starts.
//
//   delta --start <file> --out-dir <dir>
//       Re-read the log from that point and write the token buckets and the
//       measurement provenance for `emit-telemetry`.
//
// Both modes always exit 0 and always print one JSON object. A telemetry
// defect must never fail a review that found real defects, so every error on
// this path is reported in the payload rather than raised.

import {
  mkdirSync,
  openSync,
  fstatSync,
  fchmodSync,
  chmodSync,
  readdirSync,
  readSync,
  closeSync,
  readFileSync,
  writeFileSync,
  constants,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const { O_WRONLY, O_CREAT, O_TRUNC, O_NOFOLLOW } = constants;

const SNAPSHOT_VERSION = 2;

/**
 * Two gates, because extraction and emission are different decisions.
 *
 * `LOOM_REVIEW_TELEMETRY` keeps its name and its meaning: it governs whether a
 * pass **emits** a record to a pull request, which is the thing that warrants
 * an opt-in rollout. `LOOM_REVIEW_TELEMETRY_EXTRACT` governs whether this
 * helper **measures** at all, and defaults to the emission gate so no existing
 * configuration changes meaning.
 *
 * Splitting them is what makes measurement usable without publication: a cost
 * join, an offline run, or any local analysis can set the extraction gate on
 * and leave the emission gate off, and emission is then structurally
 * unreachable rather than merely unrequested. One variable meaning both
 * forecloses that combination entirely.
 *
 * Both gates are read here and nowhere else, so widening either is a one-line
 * change in a synced file rather than an edit in every skill, and no model
 * decides the question by reading the environment itself.
 *
 * A misconfigured value is not an opt-out: the helper stays disabled and says
 * why, so a typo that switches the rollout off cannot pass for a deliberate
 * `off`.
 */
const EMISSION_GATE = 'LOOM_REVIEW_TELEMETRY';
const EXTRACTION_GATE = 'LOOM_REVIEW_TELEMETRY_EXTRACT';

function readGate(name) {
  const raw = process.env[name];
  if (raw === undefined || raw === '') {
    return { set: false, enabled: false, reason: `${name} is unset` };
  }
  if (raw === 'on') {
    return { set: true, enabled: true, reason: null };
  }
  if (raw === 'off') {
    return { set: true, enabled: false, reason: `${name} is off` };
  }
  return {
    set: true,
    enabled: false,
    reason: `${name} must be exactly "on" or "off"`,
  };
}

function resolveGates() {
  const emission = readGate(EMISSION_GATE);
  const declared = readGate(EXTRACTION_GATE);
  // An unset extraction gate inherits the emission gate rather than defaulting
  // to on. Reading session transcripts on a repository that never opted in
  // would be a new behaviour for every existing consumer, and the reason it
  // reports stays the one that is actually true.
  const extraction = declared.set ? declared : emission;
  return { emission, extraction };
}

const GATES = resolveGates();

function parseArgs(argv) {
  const args = { mode: undefined };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith('--') && args.mode === undefined) {
      args.mode = arg;
      continue;
    }
    const next = argv[i + 1];
    const take = (name) => {
      if (next === undefined || next.startsWith('--')) {
        throw new Error(`missing argument for ${name}`);
      }
      i += 1;
      return next;
    };
    switch (arg) {
      case '--out':
        args.out = take(arg);
        break;
      case '--start':
        args.start = take(arg);
        break;
      case '--out-dir':
        args.outDir = take(arg);
        break;
      case '--session-log':
        args.sessionLog = take(arg);
        break;
      case '--session-id':
        args.sessionId = take(arg);
        break;
      case '--sessions-dir':
        args.sessionsDir = take(arg);
        break;
      case '--cwd':
        args.cwd = take(arg);
        break;
      default:
        throw new Error(`unknown argument ${arg}`);
    }
  }
  return args;
}

function sessionsRoot(args) {
  return (
    args.sessionsDir ??
    process.env['CODEX_SESSIONS_DIR'] ??
    join(homedir(), '.codex', 'sessions')
  );
}

/**
 * Rollout logs are filed under a `YYYY/MM/DD` tree beneath the sessions root.
 *
 * The list is deliberately unordered. Every consumer either counts matches to
 * decide whether discovery is unambiguous or walks the whole set, so ranking
 * the logs by recency here would only suggest a "most recent wins" rule that
 * discovery specifically does not apply.
 */
function listRolloutLogs(root) {
  const logs = [];
  const walk = (dir, depth) => {
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const path = join(dir, entry.name);
      if (entry.isDirectory() && depth < 3) {
        walk(path, depth + 1);
        continue;
      }
      if (
        entry.isFile() &&
        entry.name.startsWith('rollout-') &&
        entry.name.endsWith('.jsonl')
      ) {
        logs.push(path);
      }
    }
  };
  walk(root, 0);
  return logs;
}

function parseLine(line) {
  const trimmed = line.trim();
  if (trimmed === '') {
    return null;
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
}

function sessionMetaFromText(text) {
  for (const line of text.split('\n')) {
    const event = parseLine(line);
    if (event && event['type'] === 'session_meta') {
      return event['payload'] ?? null;
    }
  }
  return null;
}

const sessionMetaCache = new Map();

/**
 * Read the session header, which sits at the head of the file and therefore
 * outside the delta window.
 *
 * A header is written once at session start, so a successful read is cached
 * for the life of this one-shot process: discovery reads the header of every
 * rollout log under the sessions root, and a pass walks that tree two or three
 * times. A failed read is not cached — a log still being created must stay
 * re-readable rather than being pinned to the miss.
 */
function sessionMeta(path) {
  const cached = sessionMetaCache.get(path);
  if (cached !== undefined) {
    return cached;
  }
  let fd;
  let meta = null;
  try {
    fd = openSync(path, 'r');
    const buffer = Buffer.allocUnsafe(65536);
    const read = readSync(fd, buffer, 0, buffer.length, 0);
    meta = sessionMetaFromText(buffer.subarray(0, read).toString('utf8'));
  } catch {
    meta = null;
  } finally {
    if (fd !== undefined) {
      closeSync(fd);
    }
  }
  if (meta !== null) {
    sessionMetaCache.set(path, meta);
  }
  return meta;
}

/**
 * A rollout log's own identity.
 *
 * `id` is the log's own id on every header shape the CLI writes. `session_id`
 * is not: on a child log it repeats `parent_thread_id`, so reading it first
 * gives a child the parent's identity — which makes the descendant walk reject
 * every child as already-seen, and makes discovery by host session id
 * ambiguous as soon as one child shares the working directory. Descent is
 * carried by `parent_thread_id` alone; `session_id` is only a fallback for a
 * header that omits `id`.
 */
function sessionId(meta) {
  return meta
    ? (safeToken(meta['id']) ?? safeToken(meta['session_id']) ?? null)
    : null;
}

function sessionDescriptors(root, cwd) {
  const expectedCwd = resolve(cwd);
  return listRolloutLogs(root).flatMap((path) => {
    const meta = sessionMeta(path);
    if (
      !meta ||
      typeof meta['cwd'] !== 'string' ||
      resolve(meta['cwd']) !== expectedCwd
    ) {
      return [];
    }
    const id = sessionId(meta);
    if (id === null) {
      return [];
    }
    return [{ path, id, parentId: safeToken(meta['parent_thread_id']) }];
  });
}

function descendantDescriptors(descriptors, rootId) {
  const descendants = [];
  const knownParents = new Set([rootId]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const descriptor of descriptors) {
      if (
        descriptor.parentId !== null &&
        knownParents.has(descriptor.parentId) &&
        !knownParents.has(descriptor.id)
      ) {
        knownParents.add(descriptor.id);
        descendants.push(descriptor);
        changed = true;
      }
    }
  }
  return descendants;
}

/**
 * Resolve the rollout log for the session this pass is running in.
 *
 * The header records the working directory, so discovery can require a match
 * rather than assuming the most recent session anywhere on the machine is this
 * one. Discovery happens at snapshot time; `delta` reads the path the snapshot
 * recorded, so a second session becoming the most recent one mid-pass cannot
 * silently retarget the measurement.
 */
function discoverSessionLog(args) {
  const explicit = args.sessionLog ?? process.env['CODEX_SESSION_LOG'];
  if (explicit) {
    return resolve(explicit);
  }
  const cwd = resolve(args.cwd ?? process.cwd());
  const candidates = sessionDescriptors(sessionsRoot(args), cwd);
  const requestedId = safeToken(
    args.sessionId ??
      process.env['CODEX_SESSION_ID'] ??
      process.env['CODEX_THREAD_ID'],
  );
  if (requestedId !== null) {
    const matches = candidates.filter(
      (candidate) => candidate.id === requestedId,
    );
    return matches.length === 1 ? matches[0].path : null;
  }
  return candidates.length === 1 ? candidates[0].path : null;
}

function readCompleteWindow(path, requestedOffset = 0) {
  let fd;
  try {
    fd = openSync(path, 'r');
    const size = fstatSync(fd).size;
    const rewound = requestedOffset > size;
    const offset = rewound ? 0 : requestedOffset;
    const length = size - offset;
    const buffer = Buffer.allocUnsafe(length);
    let read = 0;
    while (read < length) {
      const got = readSync(fd, buffer, read, length - read, offset + read);
      if (got === 0) {
        break;
      }
      read += got;
    }
    const bytes = buffer.subarray(0, read);
    const newline = bytes.lastIndexOf(0x0a);
    const completeLength = newline === -1 ? 0 : newline + 1;
    return {
      text: bytes.subarray(0, completeLength).toString('utf8'),
      offset: offset + completeLength,
      rewound,
      trailingIncomplete: completeLength !== bytes.length,
    };
  } catch {
    return null;
  } finally {
    if (fd !== undefined) {
      closeSync(fd);
    }
  }
}

const TOKEN_RE = /^[A-Za-z0-9._:/-]+$/;

function safeToken(value) {
  return typeof value === 'string' && TOKEN_RE.test(value) ? value : null;
}

function nonNegativeInteger(value) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

const REPORTED_FIELDS = [
  'input_tokens',
  'cached_input_tokens',
  'cache_write_input_tokens',
  'output_tokens',
  'reasoning_output_tokens',
];

/**
 * The canonical bucket names the record uses, as distinct from the CLI field
 * names above that `toCanonical` projects onto them.
 */
const CANONICAL_BUCKET_NAMES = new Set([
  'input',
  'output',
  'cacheRead',
  'cacheWrite',
  'reasoning',
]);

/**
 * Provider bucket names this helper mints itself, which an incoming CLI field
 * of the same name must not be allowed to overwrite.
 */
const RESERVED_PROVIDER_BUCKETS = new Set(['reported_input_tokens']);

/**
 * Provider-specific integer buckets, preserved rather than dropped.
 *
 * `REPORTED_FIELDS` is what this CLI reports today, and everything else in a
 * usage object used to be discarded. A bucket the provider reported and this
 * helper threw away is the same "looks measured, isn't" failure the canonical
 * buckets go to such lengths to avoid, one level up: the record reads as a
 * complete account of the pass and silently is not. The record schema has
 * always allowed an open integer key space alongside the canonical set; the
 * gap was here, in the extractor.
 *
 * Only the token buckets carry these. The lane rows in the record have no
 * provider key space at all — and this engine reports no lanes regardless.
 *
 * Provider keys are transcript-derived strings heading for a comment on a
 * public repository, so they are bounded the same way model and effort are.
 * The pattern is the record's own key grammar; the length bound is this side's,
 * because the grammar alone puts no limit on what a key can carry. Restating a
 * canonical bucket under a provider key is refused by the record, so the source
 * fields are excluded here and the extractor cannot produce one.
 */
const PROVIDER_BUCKET_KEY_RE = /^[a-z0-9_]+$/;
const PROVIDER_BUCKET_KEY_MAX_LENGTH = 64;

function providerBucketKey(key) {
  return key.length <= PROVIDER_BUCKET_KEY_MAX_LENGTH &&
    PROVIDER_BUCKET_KEY_RE.test(key) &&
    !REPORTED_FIELDS.includes(key) &&
    !CANONICAL_BUCKET_NAMES.has(key) &&
    !RESERVED_PROVIDER_BUCKETS.has(key)
    ? key
    : null;
}

/**
 * The fields one totals object spans: the canonical five, plus every provider
 * key any of the given objects carries.
 *
 * Provider keys are sorted so the serialised record does not depend on the
 * order a JSONL event happened to list them in.
 */
function totalsFields(...sources) {
  const extra = new Set();
  for (const source of sources) {
    if (!source) {
      continue;
    }
    for (const key of Object.keys(source)) {
      if (!REPORTED_FIELDS.includes(key)) {
        extra.add(key);
      }
    }
  }
  return [...REPORTED_FIELDS, ...[...extra].sort()];
}

/**
 * Read a cumulative usage object, keeping "never reported" apart from
 * "reported zero".
 *
 * A field the CLI never sent stays null all the way to the record. Zero would
 * make that bucket look measured and free, which is the kind of defect that
 * survives for a year because the dashboard still looks plausible.
 */
function readCumulative(source) {
  if (!source || typeof source !== 'object') {
    return null;
  }
  const totals = {};
  let any = false;
  for (const field of REPORTED_FIELDS) {
    const value = nonNegativeInteger(source[field]);
    totals[field] = value;
    if (value !== null) {
      any = true;
    }
  }
  // Unknown integer keys ride along as provider buckets. `any` deliberately
  // stays canonical-only: an object carrying nothing but unrecognised keys is
  // not a usage report this helper can price, and treating it as one would put
  // a bucket set with no canonical content into the record.
  for (const [rawKey, rawValue] of Object.entries(source)) {
    const key = providerBucketKey(rawKey);
    if (key === null) {
      continue;
    }
    const value = nonNegativeInteger(rawValue);
    if (value === null) {
      // Absence is not zero here either. A key the CLI never sent, or sent as
      // something other than a count, stays out of the object entirely.
      continue;
    }
    totals[key] = value;
  }
  return any ? totals : null;
}

function tokenCountTotals(event) {
  const payload = event['payload'];
  if (
    !payload ||
    typeof payload !== 'object' ||
    payload['type'] !== 'token_count'
  ) {
    return null;
  }
  const info = payload['info'];
  if (!info || typeof info !== 'object') {
    return null;
  }
  // The cumulative total is authoritative. Summing the per-turn `last_token_usage`
  // over-counts, because a `token_count` event can restate the previous turn's
  // usage when it is emitted for a rate-limit refresh rather than a new turn.
  return readCumulative(info['total_token_usage']);
}

function turnModel(event) {
  if (event['type'] !== 'turn_context') {
    return null;
  }
  const payload = event['payload'];
  if (!payload || typeof payload !== 'object') {
    return null;
  }
  const model = safeToken(payload['model']);
  if (model === null) {
    return null;
  }
  return { model, effort: safeToken(payload['effort']) };
}

function subtract(end, start, zeroBaseline = false) {
  const delta = {};
  let regressed = false;
  let unmeasurable = false;
  for (const field of totalsFields(end, start)) {
    const endValue = end?.[field] ?? null;
    if (endValue === null) {
      delta[field] = null;
      continue;
    }
    const startValue = start?.[field] ?? null;
    if (startValue === null && !zeroBaseline) {
      // Reported at the end of the interval but not at its start, so how much
      // of it accrued inside the interval is unknown.
      unmeasurable = true;
      delta[field] = null;
      continue;
    }
    const difference = endValue - (startValue ?? 0);
    if (difference < 0) {
      regressed = true;
    }
    delta[field] = difference;
  }
  return { delta, regressed, unmeasurable };
}

/**
 * Emptiness is judged on the canonical fields alone, deliberately.
 *
 * An interval whose canonical side is unmeasurable is dropped, and dropping it
 * is what keeps `unmeasurable` able to flag the record as incomplete. If a
 * provider bucket could keep such an interval alive, the record would carry a
 * bucket set with no canonical content and no downgrade — measured-looking and
 * missing a stretch of the pass, which is the exact failure the flag exists to
 * prevent. Provider counts therefore ride along with intervals that had
 * canonical content rather than standing an interval up on their own.
 */
function isEmpty(totals) {
  return REPORTED_FIELDS.every(
    (field) => totals[field] === null || totals[field] === 0,
  );
}

/**
 * Project the CLI's usage fields onto the canonical, mutually exclusive buckets.
 *
 * The CLI reports `input_tokens` as the whole prompt side, with the cached and
 * cache-write portions as subsets of it, while the canonical buckets are
 * disjoint so that each can be priced separately. Subtracting the subsets is
 * what makes an `input` count here mean the same thing it means for an engine
 * that reports the buckets disjointly, which is the whole point of a shared
 * schema. The reported figure travels alongside as a provider bucket, so the
 * projection stays checkable rather than lossy.
 */
function toCanonical(totals) {
  const reportedInput = totals['input_tokens'];
  const cacheRead = totals['cached_input_tokens'];
  const cacheWrite = totals['cache_write_input_tokens'];

  let input = null;
  let invalidProjection = false;
  if (reportedInput !== null && cacheRead !== null && cacheWrite !== null) {
    input = reportedInput - cacheRead - cacheWrite;
    if (input < 0) {
      input = null;
      invalidProjection = true;
    }
  }

  const bucket = {
    input,
    output: totals['output_tokens'],
    cacheRead,
    cacheWrite,
    reasoning: totals['reasoning_output_tokens'],
  };
  const providerBuckets = {};
  if (reportedInput !== null) {
    providerBuckets['reported_input_tokens'] = reportedInput;
  }
  for (const field of totalsFields(totals)) {
    if (REPORTED_FIELDS.includes(field)) {
      continue;
    }
    const value = totals[field];
    if (value !== null && value !== undefined) {
      providerBuckets[field] = value;
    }
  }
  if (Object.keys(providerBuckets).length > 0) {
    bucket.providerBuckets = providerBuckets;
  }
  return { bucket, invalidProjection };
}

function mergeRawBuckets(target, source) {
  for (const bucket of source) {
    const key = `${bucket.model} ${bucket.effort ?? ''}`;
    const existing = target.get(key);
    if (existing === undefined) {
      target.set(key, {
        model: bucket.model,
        effort: bucket.effort,
        totals: { ...bucket.totals },
      });
      continue;
    }
    for (const field of totalsFields(existing.totals, bucket.totals)) {
      // A provider key one side never reported is absent, not zero: summing it
      // as zero would turn a partial observation into a measured total.
      const left = existing.totals[field] ?? null;
      const right = bucket.totals[field] ?? null;
      existing.totals[field] =
        left === null || right === null ? null : left + right;
    }
  }
}

/**
 * Walk the pass window, attributing each stretch of cumulative growth to the
 * model that was in effect while it accrued.
 *
 * A pass can span models, and a single scalar per pass would be unattributable
 * afterwards. Closing an interval at every `turn_context` change keeps each
 * bucket exact without depending on per-turn events that can repeat.
 */
function collect(text, snapshot, zeroBaseline = false) {
  const buckets = new Map();
  let current = snapshot?.model
    ? { model: snapshot.model, effort: snapshot.effort ?? null }
    : null;
  let intervalStart = snapshot?.totals ?? null;
  let lastTotals = intervalStart;
  let lastTimestamp = null;
  let regressed = false;
  let incomplete = false;
  let unparsed = 0;
  let events = 0;

  const flush = () => {
    if (current === null || lastTotals === null) {
      return;
    }
    let {
      delta,
      regressed: wentBackwards,
      unmeasurable,
    } = subtract(lastTotals, intervalStart, zeroBaseline);
    if (wentBackwards) {
      // The cumulative counter went backwards, so the baseline no longer
      // describes this session and the difference is meaningless — it would
      // serialise as a negative count. Fall back to the totals as they stand,
      // which over-counts the pass but stays a true upper bound. The
      // `regressed` flag downgrades the whole record to `unscoped-session` so
      // nothing downstream reads it as a measurement.
      regressed = true;
      delta = subtract(lastTotals, null, true).delta;
    }
    if (isEmpty(delta)) {
      // An interval with nothing measurable in it is about to be dropped, and
      // `intervalStart` then advances past it, so its usage leaves the record
      // entirely rather than joining the next bucket. Dropping it is the right
      // call — an unmeasurable start must not be assumed to be zero — but the
      // record can no longer describe itself as an exact, pass-scoped
      // measurement, because the total it reports is now missing a stretch of
      // the pass. Without this flag a single surviving bucket hides the loss.
      if (unmeasurable) {
        incomplete = true;
      }
      return;
    }
    mergeRawBuckets(buckets, [
      { model: current.model, effort: current.effort, totals: delta },
    ]);
  };

  for (const line of text.split('\n')) {
    const event = parseLine(line);
    if (event === null) {
      if (line.trim() !== '') {
        // A complete line that is not valid JSON. `parseLine` cannot say what
        // it was, so it may have been the `token_count` that closes this
        // window or a `turn_context` that moves a model boundary. Either way
        // the window is no longer a complete account of the pass, and unlike a
        // truncated final line — which is already refused — nothing else
        // downstream would notice.
        unparsed += 1;
      }
      continue;
    }
    events += 1;
    const timestamp = event['timestamp'];
    if (typeof timestamp === 'string' && !Number.isNaN(Date.parse(timestamp))) {
      if (lastTimestamp === null || timestamp > lastTimestamp) {
        lastTimestamp = timestamp;
      }
    }

    const totals = tokenCountTotals(event);
    if (totals !== null) {
      lastTotals = totals;
      continue;
    }

    const context = turnModel(event);
    if (context === null) {
      continue;
    }
    if (
      current !== null &&
      (current.model !== context.model || current.effort !== context.effort)
    ) {
      flush();
      intervalStart = lastTotals;
    }
    current = context;
  }
  flush();

  return {
    buckets: [...buckets.values()],
    regressed,
    incomplete,
    unparsed,
    lastTimestamp,
    events,
  };
}

/**
 * Write an owner-only JSON file.
 *
 * The `mode` option on `writeFileSync` applies only when the file is created,
 * so a path that already exists keeps whatever mode it had. These files record
 * the session log's location and the session id, so the mode is enforced with
 * an explicit `fchmodSync` instead of being left to the create path. `O_NOFOLLOW`
 * refuses a symlink outright: the destination directory is not always one this
 * process created, and following a planted link would write through it.
 */
function writeJson(path, value) {
  const fd = openSync(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0o600);
  try {
    fchmodSync(fd, 0o600);
    writeFileSync(fd, `${JSON.stringify(value, null, 2)}\n`);
  } finally {
    closeSync(fd);
  }
}

/**
 * Every payload reports the emission gate, in every mode and on every error
 * path. The caller decides whether to invoke `emit-telemetry` from this field
 * alone; a payload that omitted it on the failure branches would push the
 * decision back into the model, which is what having one reader avoids.
 */
function emit(payload) {
  const decorated = {
    ...payload,
    emit: GATES.emission.enabled,
    emitReason: GATES.emission.enabled ? null : GATES.emission.reason,
  };
  process.stdout.write(`${JSON.stringify(decorated, null, 2)}\n`);
  return 0;
}

/** Read the cumulative totals and model in effect at the end of a fixed prefix. */
function tailState(text) {
  let totals = null;
  let model = null;
  let effort = null;
  for (const line of text.split('\n')) {
    const event = parseLine(line);
    if (event === null) {
      continue;
    }
    const seen = tokenCountTotals(event);
    if (seen !== null) {
      totals = seen;
      continue;
    }
    const context = turnModel(event);
    if (context !== null) {
      model = context.model;
      effort = context.effort;
    }
  }
  return { totals, model, effort };
}

function runSnapshot(args) {
  if (!args.out) {
    throw new Error('snapshot requires --out');
  }
  const sessionLog = discoverSessionLog(args);
  const prefix = sessionLog ? readCompleteWindow(sessionLog) : null;
  const state = prefix
    ? tailState(prefix.text)
    : { totals: null, model: null, effort: null };
  const meta = prefix ? sessionMetaFromText(prefix.text) : null;
  const rootSessionId = sessionId(meta);
  const cwd = resolve(
    args.cwd ??
      (meta && typeof meta['cwd'] === 'string' ? meta['cwd'] : process.cwd()),
  );
  const root = resolve(sessionsRoot(args));
  const descriptors = sessionDescriptors(root, cwd);
  const knownDescendantSessionIds = rootSessionId
    ? descendantDescriptors(descriptors, rootSessionId).map(
        (descriptor) => descriptor.id,
      )
    : [];
  const snapshot = {
    version: SNAPSHOT_VERSION,
    engine: 'codex',
    sessionLog,
    sessionId: rootSessionId,
    engineVersion: meta ? (safeToken(meta['cli_version']) ?? null) : null,
    startedAt: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
    offset: prefix ? prefix.offset : 0,
    totals: state.totals,
    model: state.model,
    effort: state.effort,
    cwd,
    sessionsRoot: root,
    knownDescendantSessionIds,
  };
  // `delta` creates its `--out-dir`; the documented snapshot invocation writes
  // into the same directory one step earlier, when nothing has created it yet.
  mkdirSync(dirname(resolve(args.out)), { recursive: true, mode: 0o700 });
  writeJson(args.out, snapshot);
  return emit({
    mode: 'snapshot',
    enabled: true,
    sessionLog,
    snapshotFile: resolve(args.out),
    // A pass that starts with no discoverable log can still emit; it just
    // cannot claim a scoped measurement.
    scoped: prefix !== null && rootSessionId !== null,
    error: null,
  });
}

/**
 * Read a start snapshot, reporting why it was refused.
 *
 * The reason matters because the caller must not treat "no `--start` was
 * given" and "the `--start` file was rejected" the same way. The first is a
 * standalone pass, which legitimately falls back to discovery; the second is a
 * scoped pass whose baseline is gone, and re-running discovery there is
 * exactly the silent retarget this file's discovery contract rules out.
 */
function readSnapshot(path) {
  if (!path) {
    return { snapshot: null, rejected: null };
  }
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    return { snapshot: null, rejected: 'the start snapshot could not be read' };
  }
  if (!parsed || typeof parsed !== 'object') {
    return { snapshot: null, rejected: 'the start snapshot was not an object' };
  }
  if (parsed['version'] !== SNAPSHOT_VERSION) {
    return {
      snapshot: null,
      rejected: `the start snapshot is version ${JSON.stringify(parsed['version'])}, not ${SNAPSHOT_VERSION}`,
    };
  }
  if (
    (parsed['sessionLog'] !== null &&
      typeof parsed['sessionLog'] !== 'string') ||
    typeof parsed['offset'] !== 'number' ||
    (parsed['sessionId'] !== null && typeof parsed['sessionId'] !== 'string')
  ) {
    return {
      snapshot: null,
      rejected: 'the start snapshot is missing a required field',
    };
  }
  // `runSnapshot` only ever writes values that passed these guards, but the
  // file is read back from disk in a directory this process does not own
  // exclusively. `model` and `effort` become the bucket identity in the emitted
  // record and `engineVersion` becomes a record field, all of which reach a
  // comment on a public repository — so the boundary is re-applied on read
  // rather than trusted from the write.
  parsed['model'] = safeToken(parsed['model']);
  parsed['effort'] = safeToken(parsed['effort']);
  parsed['engineVersion'] = safeToken(parsed['engineVersion']);
  parsed['totals'] = readCumulative(parsed['totals']);
  return { snapshot: parsed, rejected: null };
}

function runDelta(args) {
  if (!args.outDir) {
    throw new Error('delta requires --out-dir');
  }
  mkdirSync(args.outDir, { recursive: true, mode: 0o700 });
  chmodSync(args.outDir, 0o700);

  const { snapshot, rejected } = readSnapshot(args.start);
  const sessionLog = snapshot
    ? snapshot.sessionLog
    : rejected === null
      ? discoverSessionLog(args)
      : null;

  const unavailable = (error) =>
    emit({
      mode: 'delta',
      enabled: true,
      // No usable log. This must never serialise as zero tokens: an engine
      // that reported nothing would otherwise look free and skew every average
      // in its favour.
      tokenSource: 'unavailable',
      tokensFile: null,
      lanesFile: null,
      engineVersion: null,
      durationSeconds: null,
      events: 0,
      error: error ?? null,
    });

  if (sessionLog === null) {
    return unavailable(rejected);
  }
  const chunk = readCompleteWindow(sessionLog, snapshot ? snapshot.offset : 0);
  if (chunk === null) {
    return unavailable('the session log could not be read');
  }

  const meta = sessionMeta(sessionLog);
  if (snapshot && sessionId(meta) !== snapshot.sessionId) {
    return unavailable('session identity changed after the snapshot');
  }
  if (chunk.trailingIncomplete) {
    return unavailable('session log ended with an incomplete JSONL event');
  }

  // A rewound log was re-read from byte zero, so the recorded totals are no
  // longer a baseline for what follows; keeping them would subtract a stale
  // figure from a fresh counter.
  const collected = collect(
    chunk.text,
    chunk.rewound && snapshot ? { ...snapshot, totals: null } : snapshot,
    !snapshot || chunk.rewound,
  );
  const aggregateBuckets = new Map();
  mergeRawBuckets(aggregateBuckets, collected.buckets);

  let regressed = collected.regressed;
  let unmeasurableInterval = collected.incomplete;
  let unparsed = collected.unparsed;
  let events = collected.events;
  let lastTimestamp = collected.lastTimestamp;
  let childIncomplete = false;
  if (snapshot) {
    const descriptors = sessionDescriptors(
      snapshot.sessionsRoot ?? sessionsRoot(args),
      snapshot.cwd ?? process.cwd(),
    );
    const known = new Set(snapshot.knownDescendantSessionIds ?? []);
    const children = descendantDescriptors(
      descriptors,
      snapshot.sessionId,
    ).filter((descriptor) => !known.has(descriptor.id));
    for (const child of children) {
      const childWindow = readCompleteWindow(child.path);
      if (childWindow === null || childWindow.trailingIncomplete) {
        childIncomplete = true;
        continue;
      }
      const childCollected = collect(childWindow.text, null, true);
      if (childCollected.buckets.length === 0) {
        // A descendant that has not reported usage yet contributes nothing.
        // That is an ordinary state — a session spawned late in the pass, one
        // that used no tokens, one that was cancelled — and is not the same as
        // a window that could not be read, which is handled above. Treating it
        // as incomplete data would discard the parent's exact measurement.
        continue;
      }
      mergeRawBuckets(aggregateBuckets, childCollected.buckets);
      regressed ||= childCollected.regressed;
      unmeasurableInterval ||= childCollected.incomplete;
      unparsed += childCollected.unparsed;
      events += childCollected.events;
      if (
        childCollected.lastTimestamp !== null &&
        (lastTimestamp === null || childCollected.lastTimestamp > lastTimestamp)
      ) {
        lastTimestamp = childCollected.lastTimestamp;
      }
    }
  }

  const canonical = [...aggregateBuckets.values()].map((bucket) => {
    const projected = toCanonical(bucket.totals);
    return {
      invalidProjection: projected.invalidProjection,
      token: {
        model: bucket.model,
        effort: bucket.effort,
        ...projected.bucket,
      },
    };
  });
  const invalidProjection = canonical.some((item) => item.invalidProjection);
  const tokens = canonical.map((item) => item.token);
  const engineVersion =
    (meta ? safeToken(meta['cli_version']) : null) ??
    (snapshot ? (snapshot.engineVersion ?? null) : null);

  let tokenSource;
  if (
    tokens.length === 0 ||
    childIncomplete ||
    invalidProjection ||
    unmeasurableInterval ||
    unparsed > 0
  ) {
    tokenSource = 'unavailable';
  } else if (!snapshot || chunk.rewound || regressed) {
    // Without a start snapshot, or after the log rewound or the counter reset
    // under us, the numbers are a truthful upper bound on the pass rather than
    // a measurement of it.
    tokenSource = 'unscoped-session';
  } else {
    tokenSource = 'session-log-delta';
  }

  let tokensFile = null;
  if (tokenSource !== 'unavailable') {
    tokensFile = join(args.outDir, 'telemetry-tokens.json');
    writeJson(tokensFile, tokens);
  }

  let durationSeconds = null;
  if (
    tokenSource === 'session-log-delta' &&
    snapshot &&
    typeof snapshot.startedAt === 'string' &&
    lastTimestamp !== null
  ) {
    const elapsed =
      (Date.parse(lastTimestamp) - Date.parse(snapshot.startedAt)) / 1000;
    if (Number.isFinite(elapsed) && elapsed >= 0) {
      durationSeconds = Math.round(elapsed);
    }
  }

  return emit({
    mode: 'delta',
    enabled: true,
    tokenSource,
    tokensFile,
    // This engine reports no per-lane attribution, and `lanes` is absent rather
    // than empty when unattributable.
    lanesFile: null,
    engineVersion,
    durationSeconds,
    events,
    error: childIncomplete
      ? 'a descendant session had incomplete usage data'
      : invalidProjection
        ? 'provider token subsets did not reconcile'
        : unmeasurableInterval
          ? 'an interval had no measurable baseline'
          : unparsed > 0
            ? 'the session log window contained an unparseable event'
            : null,
  });
}

function main(argv) {
  let args;
  try {
    args = parseArgs(argv);
  } catch (error) {
    return emit({
      mode: null,
      enabled: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }

  if (!GATES.extraction.enabled) {
    if (args.mode === 'snapshot') {
      return emit({
        mode: 'snapshot',
        enabled: false,
        reason: GATES.extraction.reason,
        sessionLog: null,
        snapshotFile: null,
        scoped: false,
        error: GATES.extraction.error ?? GATES.emission.error ?? null,
      });
    }
    return emit({
      mode: args.mode ?? null,
      enabled: false,
      reason: GATES.extraction.reason,
      // A pass whose extraction is off but whose emission is on still emits,
      // and `unavailable` is the truthful provenance for a measurement that
      // was never taken. Reporting null here would leave the caller to
      // substitute a value by hand, which is the one thing `tokenSource` may
      // never allow.
      tokenSource: args.mode === 'delta' ? 'unavailable' : null,
      tokensFile: null,
      lanesFile: null,
      engineVersion: null,
      durationSeconds: null,
      events: args.mode === 'delta' ? 0 : null,
      error: GATES.extraction.error ?? GATES.emission.error ?? null,
    });
  }

  try {
    if (args.mode === 'snapshot') {
      return runSnapshot(args);
    }
    if (args.mode === 'delta') {
      return runDelta(args);
    }
    throw new Error('mode must be "snapshot" or "delta"');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (args.mode === 'snapshot') {
      // A caller reads `scoped` and `snapshotFile` to find out whether the
      // snapshot succeeded. Emitting the delta shape here would leave both
      // undefined and hide the failure behind fields this mode never sets.
      return emit({
        mode: 'snapshot',
        enabled: true,
        sessionLog: null,
        snapshotFile: null,
        scoped: false,
        error: message,
      });
    }
    return emit({
      mode: args.mode ?? null,
      enabled: true,
      tokenSource: 'unavailable',
      tokensFile: null,
      lanesFile: null,
      engineVersion: null,
      durationSeconds: null,
      error: message,
    });
  }
}

process.exitCode = main(process.argv.slice(2));
