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
  readdirSync,
  readSync,
  closeSync,
  readFileSync,
  writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

const SNAPSHOT_VERSION = 2;

/**
 * Emission is opt-in while the extraction is being proven on a single
 * repository. The gate is read here rather than decided by the model so that
 * one place governs whether a pass emits at all, and so widening it later is a
 * one-line change in a synced file rather than an edit in every skill.
 */
function gateState() {
  const raw = process.env['LOOM_REVIEW_TELEMETRY'];
  if (raw === undefined || raw === '') {
    return { enabled: false, reason: 'LOOM_REVIEW_TELEMETRY is unset' };
  }
  if (raw === 'on') {
    return { enabled: true, reason: null };
  }
  if (raw === 'off') {
    return { enabled: false, reason: 'LOOM_REVIEW_TELEMETRY is off' };
  }
  return {
    enabled: false,
    reason: 'LOOM_REVIEW_TELEMETRY must be exactly "on" or "off"',
  };
}

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

function sessionId(meta) {
  return meta
    ? (safeToken(meta['session_id']) ?? safeToken(meta['id']) ?? null)
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
  for (const field of REPORTED_FIELDS) {
    const endValue = end?.[field] ?? null;
    if (endValue === null) {
      delta[field] = null;
      continue;
    }
    const startValue = start?.[field] ?? null;
    if (startValue === null && !zeroBaseline) {
      delta[field] = null;
      continue;
    }
    const difference = endValue - (startValue ?? 0);
    if (difference < 0) {
      regressed = true;
    }
    delta[field] = difference;
  }
  return { delta, regressed };
}

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
  if (reportedInput !== null) {
    bucket.providerBuckets = { reported_input_tokens: reportedInput };
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
    for (const field of REPORTED_FIELDS) {
      const left = existing.totals[field];
      const right = bucket.totals[field];
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
  let events = 0;

  const flush = () => {
    if (current === null || lastTotals === null) {
      return;
    }
    let { delta, regressed: wentBackwards } = subtract(
      lastTotals,
      intervalStart,
      zeroBaseline,
    );
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
      return;
    }
    mergeRawBuckets(buckets, [
      { model: current.model, effort: current.effort, totals: delta },
    ]);
  };

  for (const line of text.split('\n')) {
    const event = parseLine(line);
    if (event === null) {
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

  return { buckets: [...buckets.values()], regressed, lastTimestamp, events };
}

function writeJson(path, value) {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
}

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
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

function readSnapshot(path) {
  if (!path) {
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    return null;
  }
  if (
    !parsed ||
    typeof parsed !== 'object' ||
    parsed['version'] !== SNAPSHOT_VERSION ||
    typeof parsed['sessionLog'] !== 'string' ||
    typeof parsed['offset'] !== 'number' ||
    typeof parsed['sessionId'] !== 'string'
  ) {
    return null;
  }
  return parsed;
}

function runDelta(args) {
  if (!args.outDir) {
    throw new Error('delta requires --out-dir');
  }
  mkdirSync(args.outDir, { recursive: true, mode: 0o700 });

  const snapshot = readSnapshot(args.start);
  const sessionLog = snapshot ? snapshot.sessionLog : discoverSessionLog(args);

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
    return unavailable(null);
  }
  const chunk = readCompleteWindow(sessionLog, snapshot ? snapshot.offset : 0);
  if (chunk === null) {
    return unavailable(null);
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
        childIncomplete = true;
        continue;
      }
      mergeRawBuckets(aggregateBuckets, childCollected.buckets);
      regressed ||= childCollected.regressed;
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
  if (tokens.length === 0 || childIncomplete || invalidProjection) {
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

  const gate = gateState();
  if (!gate.enabled) {
    return emit({
      mode: args.mode ?? null,
      enabled: false,
      reason: gate.reason,
      tokenSource: null,
      tokensFile: null,
      lanesFile: null,
      error: null,
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
    return emit({
      mode: args.mode ?? null,
      enabled: true,
      tokenSource: 'unavailable',
      tokensFile: null,
      lanesFile: null,
      engineVersion: null,
      durationSeconds: null,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

process.exitCode = main(process.argv.slice(2));
