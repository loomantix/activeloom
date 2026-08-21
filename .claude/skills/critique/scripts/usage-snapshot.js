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
//       Re-read the log from that point and write the token buckets, the
//       lanes, and the measurement provenance for `emit-telemetry`.
//
// Both modes always exit 0 and always print one JSON object. A telemetry
// defect must never fail a review that found real defects, so every error on
// this path is reported in the payload rather than raised.

import { createHash } from 'node:crypto';
import {
  mkdirSync,
  openSync,
  readdirSync,
  readSync,
  closeSync,
  statSync,
  readFileSync,
  writeFileSync,
  chmodSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';

const SNAPSHOT_VERSION = 1;

/**
 * Emission is opt-in while the extraction is being proven on a single
 * repository. The gate is read here rather than decided by the model so that
 * one place governs whether a pass emits at all, and so widening it later is a
 * one-line change in a synced file rather than an edit in every skill.
 */
function gateState() {
  const raw = process.env['LOOM_REVIEW_TELEMETRY'];
  if (raw === undefined || raw.trim() === '') {
    return { enabled: false, reason: 'LOOM_REVIEW_TELEMETRY is unset' };
  }
  const value = raw.trim().toLowerCase();
  if (value === 'on') {
    return { enabled: true, reason: null };
  }
  if (value === 'off') {
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
      case '--projects-dir':
        args.projectsDir = take(arg);
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

/**
 * Claude Code stores a session under a directory named for the working
 * directory with every separator replaced by a dash.
 */
function projectSlug(cwd) {
  return resolve(cwd).split(sep).join('-');
}

function projectsRoot(args) {
  return (
    args.projectsDir ??
    process.env['CLAUDE_PROJECTS_DIR'] ??
    join(homedir(), '.claude', 'projects')
  );
}

function listSessionLogs(dir) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }
  const logs = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.jsonl')) {
      continue;
    }
    const path = join(dir, entry.name);
    try {
      logs.push({ path, mtimeMs: statSync(path).mtimeMs });
    } catch {
      // A log that vanished between listing and stat is simply not a
      // candidate; it is never the session we are running in.
    }
  }
  return logs.sort((left, right) => right.mtimeMs - left.mtimeMs);
}

function findSessionLogById(root, preferredDir, sessionId) {
  const name = `${sessionId}.jsonl`;
  const preferred = join(preferredDir, name);
  if (fileSize(preferred) !== null) {
    return preferred;
  }
  let projects;
  try {
    projects = readdirSync(root, { withFileTypes: true });
  } catch {
    return null;
  }
  const matches = projects
    .filter((entry) => entry.isDirectory())
    .map((entry) => join(root, entry.name, name))
    .filter((path) => fileSize(path) !== null);
  return matches.length === 1 ? matches[0] : null;
}

/**
 * Resolve the log for the session this pass is running in.
 *
 * Prefer the harness's exact session id. Newest-by-mtime remains a compatibility
 * fallback, but it is never strong enough to claim pass-scoped provenance when
 * another session can use the same working directory. `delta` never re-targets
 * a valid identity-bound snapshot.
 */
function discoverSessionLog(args) {
  const explicit = args.sessionLog ?? process.env['CLAUDE_SESSION_LOG'];
  if (explicit) {
    return { path: resolve(explicit), identityBound: true };
  }
  const root = projectsRoot(args);
  const dir = join(root, projectSlug(args.cwd ?? process.cwd()));
  const sessionId = process.env['CLAUDE_CODE_SESSION_ID'];
  if (sessionId && /^[A-Za-z0-9._-]+$/.test(sessionId)) {
    const identityBound = findSessionLogById(root, dir, sessionId);
    if (identityBound !== null) {
      return { path: identityBound, identityBound: true };
    }
  }
  const [newest] = listSessionLogs(dir);
  return newest ? { path: newest.path, identityBound: false } : null;
}

function sessionIdFor(sessionLogPath) {
  const name = sessionLogPath.split(sep).pop() ?? '';
  return name.endsWith('.jsonl') ? name.slice(0, -'.jsonl'.length) : name;
}

/**
 * Subagent turns are written to their own files beside the session log rather
 * than inline in it, so a sum over the session log alone would omit every lane
 * a fanned-out review ran — the bulk of a deep pass.
 */
function subagentLogs(sessionLogPath) {
  const dir = join(sessionLogPath.slice(0, -'.jsonl'.length), 'subagents');
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }
  return entries
    .filter((entry) => entry.isFile() && entry.name.endsWith('.jsonl'))
    .map((entry) => join(dir, entry.name))
    .sort();
}

function passLogs(sessionLogPath) {
  return [sessionLogPath, ...subagentLogs(sessionLogPath)];
}

function fileSize(path) {
  try {
    return statSync(path).size;
  } catch {
    return null;
  }
}

/** Read a byte range without pulling a multi-megabyte transcript into memory. */
function readFrom(path, offset, size) {
  if (offset > size) {
    // The log was truncated or rotated under us, so the recorded offset no
    // longer names the point the pass began at.
    return { text: readFileSync(path, 'utf8'), rewound: true };
  }
  if (offset === size) {
    return { text: '', rewound: false };
  }
  const length = size - offset;
  const buffer = Buffer.allocUnsafe(length);
  const fd = openSync(path, 'r');
  try {
    let read = 0;
    while (read < length) {
      const got = readSync(fd, buffer, read, length - read, offset + read);
      if (got === 0) {
        break;
      }
      read += got;
    }
    return { text: buffer.subarray(0, read).toString('utf8'), rewound: false };
  } finally {
    closeSync(fd);
  }
}

function parseEntries(text) {
  const entries = [];
  let degraded = false;
  const lines = text.split('\n');
  for (const [index, line] of lines.entries()) {
    const trimmed = line.trim();
    if (trimmed === '') {
      continue;
    }
    try {
      entries.push(JSON.parse(trimmed));
    } catch {
      // A partially flushed final line is expected when reading a log that is
      // still being appended to. Skipping it under-counts by at most one turn;
      // failing here would cost the whole record. Only the final fragment is a
      // normal boundary condition; corruption between complete lines makes a
      // scoped measurement untrustworthy.
      if (index < lines.length - 1 || text.endsWith('\n')) {
        degraded = true;
      }
    }
  }
  return { entries, degraded };
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

/**
 * Accumulate one bucket set, keeping "never reported" apart from "reported
 * zero".
 *
 * A bucket the provider never sent stays null all the way to the record. Zero
 * would make that bucket look measured and free, which is the kind of defect
 * that survives for a year because the dashboard still looks plausible.
 */
function makeCounters() {
  return {
    input: null,
    output: null,
    cacheRead: null,
    cacheWrite: null,
    reasoning: null,
  };
}

function add(counters, key, value) {
  const amount = nonNegativeInteger(value);
  if (amount === null) {
    return;
  }
  counters[key] = (counters[key] ?? 0) + amount;
}

function accumulate(counters, usage) {
  add(counters, 'input', usage['input_tokens']);
  add(counters, 'output', usage['output_tokens']);
  add(counters, 'cacheRead', usage['cache_read_input_tokens']);
  add(counters, 'cacheWrite', usage['cache_creation_input_tokens']);
  const details = usage['output_tokens_details'];
  if (details && typeof details === 'object') {
    add(counters, 'reasoning', details['thinking_tokens']);
  }
}

/**
 * A turn's stable identity, so a turn written more than once is counted once.
 *
 * A streaming turn is appended repeatedly as it is produced, and its `usage`
 * grows with it: the input and cache buckets stay fixed while `output_tokens`
 * climbs from a partial count to the final one. Counting every occurrence
 * multiplies the input side; keeping the first silently records a fraction of
 * the output. The resolution is to keep exactly one occurrence per turn and to
 * make it the completed one — see `retain` below.
 */
function turnKey(entry) {
  const message = entry['message'];
  const messageId =
    message && typeof message === 'object' ? message['id'] : undefined;
  return (
    entry['requestId'] ??
    messageId ??
    entry['uuid'] ??
    createHash('sha256').update(JSON.stringify(entry)).digest('hex')
  );
}

function usageOf(entry) {
  if (entry['type'] !== 'assistant') {
    return null;
  }
  const message = entry['message'];
  if (!message || typeof message !== 'object') {
    return null;
  }
  const usage = message['usage'];
  if (!usage || typeof usage !== 'object') {
    return null;
  }
  const details = usage['output_tokens_details'];
  const values = [
    usage['input_tokens'],
    usage['output_tokens'],
    usage['cache_read_input_tokens'],
    usage['cache_creation_input_tokens'],
    details && typeof details === 'object'
      ? details['thinking_tokens']
      : undefined,
  ];
  if (!values.some((value) => nonNegativeInteger(value) !== null)) {
    return null;
  }
  return { usage, model: message['model'] };
}

/**
 * Choose between two occurrences of the same turn.
 *
 * The completed occurrence is the one carrying the most output, since output
 * is the only bucket that grows while a turn streams. Preferring the later
 * occurrence on a tie keeps the choice deterministic without depending on the
 * order the log happened to be flushed in.
 */
function retain(previous, candidate) {
  if (previous === undefined) {
    return candidate;
  }
  const before = nonNegativeInteger(previous.usage['output_tokens']) ?? 0;
  const after = nonNegativeInteger(candidate.usage['output_tokens']) ?? 0;
  return after >= before ? candidate : previous;
}

function boundaryFor(path) {
  try {
    const stat = statSync(path);
    return { path, size: stat.size, dev: stat.dev, ino: stat.ino };
  } catch {
    return null;
  }
}

function boundaryUnchanged(boundary) {
  const current = boundaryFor(boundary.path);
  return (
    current !== null &&
    current.size === boundary.size &&
    current.dev === boundary.dev &&
    current.ino === boundary.ino
  );
}

function collect(sessionLog, offsets) {
  const turnsByKey = new Map();
  let rewound = false;
  let degraded = false;
  const logs = passLogs(sessionLog);
  const boundaries = logs.map(boundaryFor).filter((value) => value !== null);

  for (const boundary of boundaries) {
    const chunk = readFrom(
      boundary.path,
      offsets[boundary.path] ?? 0,
      boundary.size,
    );
    if (chunk === null) {
      degraded = true;
      continue;
    }
    if (chunk.rewound) {
      rewound = true;
    }
    const parsed = parseEntries(chunk.text);
    degraded ||= parsed.degraded;
    for (const entry of parsed.entries) {
      const found = usageOf(entry);
      if (found === null) {
        continue;
      }
      const key = turnKey(entry);
      turnsByKey.set(
        key,
        retain(turnsByKey.get(key), {
          usage: found.usage,
          model: safeToken(found.model),
          effort: safeToken(entry['effort']),
          lens:
            entry['isSidechain'] === true
              ? safeToken(entry['attributionAgent'])
              : null,
          version: safeToken(entry['version']),
          timestamp: entry['timestamp'],
        }),
      );
    }
  }

  const finalLogs = passLogs(sessionLog);
  if (
    finalLogs.length !== logs.length ||
    finalLogs.some((path, index) => path !== logs[index]) ||
    boundaries.length !== logs.length ||
    boundaries.some((boundary) => !boundaryUnchanged(boundary))
  ) {
    degraded = true;
  }

  const byModel = new Map();
  const byLens = new Map();
  let engineVersion = null;
  let lastTimestamp = null;
  let turns = 0;

  for (const turn of turnsByKey.values()) {
    turns += 1;

    if (turn.model !== null) {
      const bucketKey = `${turn.model} ${turn.effort ?? ''}`;
      let bucket = byModel.get(bucketKey);
      if (bucket === undefined) {
        bucket = {
          model: turn.model,
          effort: turn.effort,
          counters: makeCounters(),
        };
        byModel.set(bucketKey, bucket);
      }
      accumulate(bucket.counters, turn.usage);
    }

    if (turn.lens !== null) {
      let lane = byLens.get(turn.lens);
      if (lane === undefined) {
        lane = { lens: turn.lens, models: new Set(), counters: makeCounters() };
        byLens.set(turn.lens, lane);
      }
      if (turn.model !== null) {
        lane.models.add(turn.model);
      }
      accumulate(lane.counters, turn.usage);
    }

    if (turn.version !== null) {
      engineVersion = turn.version;
    }
    if (
      typeof turn.timestamp === 'string' &&
      !Number.isNaN(Date.parse(turn.timestamp)) &&
      (lastTimestamp === null || turn.timestamp > lastTimestamp)
    ) {
      lastTimestamp = turn.timestamp;
    }
  }

  const tokens = [...byModel.values()].map((bucket) => ({
    model: bucket.model,
    effort: bucket.effort,
    ...bucket.counters,
  }));
  const lanes = [...byLens.values()].map((lane) => ({
    lens: lane.lens,
    // A lane that spanned models has no single model id to report, and naming
    // one of them would be a guess presented as a measurement.
    model: lane.models.size === 1 ? [...lane.models][0] : null,
    ...lane.counters,
  }));

  return {
    tokens,
    lanes,
    rewound,
    degraded,
    engineVersion,
    lastTimestamp,
    turns,
  };
}

function writeJson(path, value) {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  chmodSync(path, 0o600);
}

function ensureOwnerOnlyDir(path) {
  mkdirSync(path, { recursive: true, mode: 0o700 });
  chmodSync(path, 0o700);
}

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  return 0;
}

function runSnapshot(args) {
  if (!args.out) {
    throw new Error('snapshot requires --out');
  }
  ensureOwnerOnlyDir(dirname(resolve(args.out)));
  const discovered = discoverSessionLog(args);
  const sessionLog = discovered?.path ?? null;
  const snapshot = {
    version: SNAPSHOT_VERSION,
    engine: 'claude',
    sessionLog,
    sessionId: sessionLog ? sessionIdFor(sessionLog) : null,
    startedAt: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
    offsets: {},
    identityBound: discovered?.identityBound ?? false,
  };
  if (sessionLog !== null) {
    for (const path of passLogs(sessionLog)) {
      const size = fileSize(path);
      if (size !== null) {
        snapshot.offsets[path] = size;
      }
    }
  }
  writeJson(args.out, snapshot);
  return emit({
    mode: 'snapshot',
    enabled: true,
    sessionLog,
    snapshotFile: resolve(args.out),
    // A pass that starts with no discoverable log can still emit; it just
    // cannot claim a scoped measurement.
    scoped:
      sessionLog !== null &&
      discovered?.identityBound === true &&
      snapshot.offsets[sessionLog] !== undefined,
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
    typeof parsed['offsets'] !== 'object' ||
    parsed['offsets'] === null ||
    parsed['identityBound'] !== true ||
    nonNegativeInteger(parsed['offsets'][parsed['sessionLog']]) === null
  ) {
    return null;
  }
  return parsed;
}

function runDelta(args) {
  if (!args.outDir) {
    throw new Error('delta requires --out-dir');
  }
  ensureOwnerOnlyDir(args.outDir);

  const snapshot = readSnapshot(args.start);
  const discovered = snapshot ? null : discoverSessionLog(args);
  const sessionLog = snapshot
    ? snapshot.sessionLog
    : (discovered?.path ?? null);

  if (sessionLog === null || fileSize(sessionLog) === null) {
    // No usable log. This must never serialise as zero tokens: an engine that
    // reported nothing would otherwise look free and skew every average in its
    // favour.
    return emit({
      mode: 'delta',
      enabled: true,
      tokenSource: 'unavailable',
      tokensFile: null,
      lanesFile: null,
      engineVersion: null,
      durationSeconds: null,
      turns: 0,
      error: null,
    });
  }

  const offsets = snapshot ? snapshot.offsets : {};
  const collected = collect(sessionLog, offsets);

  let tokenSource;
  if (collected.tokens.length === 0 || collected.degraded) {
    tokenSource = 'unavailable';
  } else if (!snapshot || collected.rewound) {
    // Without a start snapshot, or after the log rewound under us, the numbers
    // are a truthful upper bound on the pass rather than a measurement of it.
    tokenSource = 'unscoped-session';
  } else {
    tokenSource = 'session-log-delta';
  }

  let tokensFile = null;
  let lanesFile = null;
  if (tokenSource !== 'unavailable') {
    tokensFile = join(args.outDir, 'telemetry-tokens.json');
    writeJson(tokensFile, collected.tokens);
    if (collected.lanes.length > 0) {
      lanesFile = join(args.outDir, 'telemetry-lanes.json');
      writeJson(lanesFile, collected.lanes);
    }
  }

  let durationSeconds = null;
  if (
    tokenSource === 'session-log-delta' &&
    snapshot &&
    typeof snapshot.startedAt === 'string' &&
    collected.lastTimestamp !== null
  ) {
    const elapsed =
      (Date.parse(collected.lastTimestamp) - Date.parse(snapshot.startedAt)) /
      1000;
    if (Number.isFinite(elapsed) && elapsed >= 0) {
      durationSeconds = Math.round(elapsed);
    }
  }

  return emit({
    mode: 'delta',
    enabled: true,
    tokenSource,
    tokensFile,
    lanesFile,
    engineVersion: collected.engineVersion,
    durationSeconds,
    turns: collected.turns,
    error: null,
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
