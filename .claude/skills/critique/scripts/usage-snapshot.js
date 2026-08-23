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
  unlinkSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';

const SNAPSHOT_VERSION = 1;

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
 */
const EMISSION_GATE = 'LOOM_REVIEW_TELEMETRY';
const EXTRACTION_GATE = 'LOOM_REVIEW_TELEMETRY_EXTRACT';

function readGate(name) {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') {
    return { set: false, enabled: false, reason: `${name} is unset` };
  }
  const value = raw.trim().toLowerCase();
  if (value === 'on') {
    return { set: true, enabled: true, reason: null };
  }
  if (value === 'off') {
    return { set: true, enabled: false, reason: `${name} is off` };
  }
  // A misconfigured value is not an opt-out. Reporting it only as a reason
  // would make a typo that disables the whole rollout indistinguishable from a
  // deliberate `off`.
  return {
    set: true,
    enabled: false,
    reason: `${name} must be exactly "on" or "off"`,
    error: `${name} must be exactly "on" or "off"`,
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
 * directory with every non-alphanumeric character replaced by a dash.
 *
 * Replacing only the separator is not enough: a working directory containing a
 * dot — a hostname-style repository name, or a worktree under a dotted
 * directory — resolves to a directory that does not exist, and discovery then
 * falls all the way through to no log at all.
 */
function projectSlug(cwd) {
  return resolve(cwd).replace(/[^A-Za-z0-9]/g, '-');
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
  let degradedReason = null;
  const lines = text.split('\n');
  for (const [index, line] of lines.entries()) {
    const trimmed = line.trim();
    if (trimmed === '') {
      continue;
    }
    try {
      entries.push(JSON.parse(trimmed));
    } catch {
      // A partial final record is a normal concurrency condition, but omitting
      // it still makes the captured total incomplete. Mid-log corruption and a
      // partial tail have different diagnostics; neither may claim scoped
      // provenance.
      degradedReason ??=
        index === lines.length - 1 && !text.endsWith('\n')
          ? 'partial-record'
          : 'parse-corruption';
    }
  }
  return { entries, degradedReason };
}

const TOKEN_RE = /^[A-Za-z0-9._:/-]+$/;

/**
 * The accepted alphabet is a base64url superset, so a shape check alone puts no
 * bound on what a transcript-derived string can carry into a public record.
 * Model, effort, lane, and engine version all pass through here.
 */
const TOKEN_MAX_LENGTH = 128;

function safeToken(value) {
  return typeof value === 'string' &&
    value.length <= TOKEN_MAX_LENGTH &&
    TOKEN_RE.test(value)
    ? value
    : null;
}

function nonNegativeInteger(value) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

/**
 * The canonical buckets, as counter key and the reader that pulls the bucket
 * out of a provider `usage` object.
 *
 * One table drives the presence check in `usageOf`, the null initialisation in
 * `makeCounters`, and the summation in `accumulate`. Enumerating the same five
 * fields separately in each is how a bucket added later ends up summed but not
 * counted toward presence — the two answers drift apart silently, and the
 * record still looks plausible.
 */
const USAGE_FIELDS = [
  ['input', (usage) => usage['input_tokens']],
  ['output', (usage) => usage['output_tokens']],
  ['cacheRead', (usage) => usage['cache_read_input_tokens']],
  ['cacheWrite', (usage) => usage['cache_creation_input_tokens']],
  [
    'reasoning',
    (usage) => {
      const details = usage['output_tokens_details'];
      return details && typeof details === 'object'
        ? details['thinking_tokens']
        : undefined;
    },
  ],
];

/**
 * Provider-specific integer buckets, preserved rather than dropped.
 *
 * `USAGE_FIELDS` above is the canonical five, and everything else in a
 * provider's `usage` object used to be discarded. A bucket the provider
 * reported and this helper threw away is the same "looks measured, isn't"
 * failure the canonical buckets go to such lengths to avoid, one level up: the
 * record reads as a complete account of the pass and silently is not. The
 * record schema has always allowed an open integer key space alongside the
 * canonical set; the gap was here, in the extractor.
 *
 * Only the token buckets carry these. The lane rows in the record have no
 * provider key space at all, so attaching one there would be dropped on
 * validation — measured, serialised, and then silently gone.
 *
 * The two exclusion sets below are what keep a canonical count from arriving
 * twice under two names — once as itself and once as a provider bucket, which
 * the record refuses outright and which would read as corroboration if it did
 * not. Excluding both the canonical names and the provider keys they are read
 * from makes the extractor incapable of producing that record rather than
 * merely unlikely to.
 */
const CANONICAL_BUCKET_NAMES = new Set(USAGE_FIELDS.map(([key]) => key));

/** The provider keys `USAGE_FIELDS` already reads. */

const CANONICAL_USAGE_KEYS = new Set([
  'input_tokens',
  'output_tokens',
  'cache_read_input_tokens',
  'cache_creation_input_tokens',
  'output_tokens_details',
]);

/**
 * Provider keys are transcript-derived strings heading for a comment on a
 * public repository, so they are bounded the same way `safeToken` bounds model
 * and lens. The pattern is the record's own key grammar; the length bound is
 * this side's, because the grammar alone puts no limit on what a key can
 * carry.
 */
const PROVIDER_BUCKET_KEY_RE = /^[a-z0-9_]+$/;
const PROVIDER_BUCKET_KEY_MAX_LENGTH = 64;

function providerBucketKey(key) {
  return key.length <= PROVIDER_BUCKET_KEY_MAX_LENGTH &&
    PROVIDER_BUCKET_KEY_RE.test(key) &&
    !CANONICAL_USAGE_KEYS.has(key) &&
    !CANONICAL_BUCKET_NAMES.has(key)
    ? key
    : null;
}

/**
 * A key that cannot be represented safely is dropped, and dropping it does not
 * downgrade the record.
 *
 * An unrecognised model id degrades because it makes two aggregates in one
 * record disagree. An unrepresentable extra key does no such thing: the
 * canonical buckets are still exact. Throwing away a valid measurement over a
 * key the schema could never have carried would cost more than it protects.
 */
function accumulateProvider(provider, usage) {
  for (const [rawKey, rawValue] of Object.entries(usage)) {
    const key = providerBucketKey(rawKey);
    if (key === null) {
      continue;
    }
    const amount = nonNegativeInteger(rawValue);
    if (amount === null) {
      // Absence is not zero here either. A key the provider never sent stays
      // out of the object entirely rather than arriving as a measured nothing.
      continue;
    }
    provider[key] = (provider[key] ?? 0) + amount;
  }
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
  return Object.fromEntries(USAGE_FIELDS.map(([key]) => [key, null]));
}

function add(counters, key, value) {
  const amount = nonNegativeInteger(value);
  if (amount === null) {
    return;
  }
  counters[key] = (counters[key] ?? 0) + amount;
}

function accumulate(counters, usage) {
  for (const [key, read] of USAGE_FIELDS) {
    add(counters, key, read(usage));
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
  const measured = USAGE_FIELDS.some(
    ([, read]) => nonNegativeInteger(read(usage)) !== null,
  );
  if (!measured) {
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

/**
 * A boundary holds when the file is still the same file and still contains at
 * least the bytes that were read.
 *
 * Growth is not instability: `readFrom` only ever reads the prefix
 * `[offset, boundary.size)` captured before the read, so bytes appended
 * afterwards cannot change what was counted. Requiring an exact size would
 * discard a whole valid measurement whenever anything appended during
 * collection — and `delta` runs inside the session it measures, so a
 * still-finishing subagent or a hook is enough. Only shrinkage or a
 * replacement means the recorded end offset no longer names the same content.
 */
function boundaryUnchanged(boundary) {
  const current = boundaryFor(boundary.path);
  return (
    current !== null &&
    current.size >= boundary.size &&
    current.dev === boundary.dev &&
    current.ino === boundary.ino
  );
}

function collect(sessionLog, offsets, identities = {}) {
  const turnsByKey = new Map();
  let rewound = false;
  let degraded = false;
  let degradedReason = null;
  const degrade = (reason) => {
    degraded = true;
    degradedReason ??= reason;
  };
  const logs = passLogs(sessionLog);
  const boundaries = logs.map(boundaryFor).filter((value) => value !== null);

  // A log recorded at snapshot time and absent now is not simply "not a
  // candidate": its measured cost silently leaves a record that still claims to
  // be scoped and complete. The delta-time re-check below compares the log set
  // against itself and cannot see this.
  const present = new Set(logs);
  if (Object.keys(offsets).some((path) => !present.has(path))) {
    degrade('log-missing-since-snapshot');
  }

  // The device/inode pin only guards against replacement within one `collect`
  // unless it also crosses the snapshot. Without this, a file swapped for a
  // different, longer one between snapshot and delta reads as ordinary growth.
  for (const boundary of boundaries) {
    const recorded = identities[boundary.path];
    if (
      recorded &&
      (recorded.dev !== boundary.dev || recorded.ino !== boundary.ino)
    ) {
      degrade('log-replaced-since-snapshot');
    }
  }

  for (const boundary of boundaries) {
    const chunk = readFrom(
      boundary.path,
      offsets[boundary.path] ?? 0,
      boundary.size,
    );
    if (chunk.rewound) {
      rewound = true;
    }
    const parsed = parseEntries(chunk.text);
    if (parsed.degradedReason !== null) {
      degrade(parsed.degradedReason);
    }
    for (const entry of parsed.entries) {
      const found = usageOf(entry);
      if (found === null) {
        continue;
      }
      // An id the shape gate rejects would otherwise be dropped from the
      // per-model buckets while still reaching the lane buckets, leaving the
      // two aggregates in one record disagreeing with no downgrade.
      if (safeToken(found.model) === null) {
        degrade('unrecognized-model-id');
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
    degrade('boundary-moved');
  }

  const byModel = new Map();
  const byLens = new Map();
  let engineVersion = null;
  let lastTimestamp = null;
  let lastAt = null;
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
          provider: {},
        };
        byModel.set(bucketKey, bucket);
      }
      accumulate(bucket.counters, turn.usage);
      accumulateProvider(bucket.provider, turn.usage);
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

    // Both of these describe the newest turn in the range, so both must be
    // chosen by instant. Map iteration is insertion order — the session log,
    // then subagent logs sorted by filename — so an unordered assignment
    // reports whichever turn happened to land last, and a lexical comparison
    // of timestamps only agrees with time while every one is UTC at identical
    // sub-second precision.
    const at =
      typeof turn.timestamp === 'string' ? Date.parse(turn.timestamp) : NaN;
    if (!Number.isNaN(at) && (lastAt === null || at > lastAt)) {
      lastAt = at;
      lastTimestamp = turn.timestamp;
      if (turn.version !== null) {
        engineVersion = turn.version;
      }
    }
    if (engineVersion === null && turn.version !== null) {
      engineVersion = turn.version;
    }
  }

  const tokens = [...byModel.values()].map((bucket) => {
    const record = {
      model: bucket.model,
      effort: bucket.effort,
      ...bucket.counters,
    };
    // Omitted rather than emitted empty: the record defaults an absent
    // `providerBuckets` to `{}` itself, and an empty object on every row would
    // read as "asked and answered nothing" instead of "this provider reports
    // nothing beyond the canonical set".
    if (Object.keys(bucket.provider).length > 0) {
      record.providerBuckets = bucket.provider;
    }
    return record;
  });
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
    degradedReason,
    engineVersion,
    lastTimestamp,
    turns,
  };
}

/**
 * Write owner-only without following a symlink already sitting at the path.
 *
 * `writeFileSync` follows one, so a link planted in the telemetry directory
 * would have its target overwritten and chmodded. Unlinking first makes the
 * write land on a file this run created, and the creation mode closes the
 * window the trailing chmod would otherwise leave open.
 */
function writeJson(path, value) {
  try {
    unlinkSync(path);
  } catch {
    // Nothing there, or nothing we may remove; the exclusive create below is
    // what actually decides whether the write happens.
  }
  const fd = openSync(path, 'wx', 0o600);
  try {
    writeFileSync(fd, `${JSON.stringify(value, null, 2)}\n`);
  } finally {
    closeSync(fd);
  }
  chmodSync(path, 0o600);
}

/**
 * Narrow only what this run created. `mkdirSync` with `recursive` is a no-op on
 * an existing directory, so an unconditional chmod would strip group and other
 * access from a caller-named directory the run does not own.
 */
function ensureOwnerOnlyDir(path) {
  const created = mkdirSync(path, { recursive: true, mode: 0o700 });
  if (created !== undefined) {
    chmodSync(path, 0o700);
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
    // Recorded alongside the offsets so `delta` can tell a file that grew from
    // a different file that replaced it. Without this the identity pin never
    // crosses the snapshot boundary.
    identities: {},
    identityBound: discovered?.identityBound ?? false,
  };
  if (sessionLog !== null) {
    for (const path of passLogs(sessionLog)) {
      const boundary = boundaryFor(path);
      if (boundary !== null) {
        snapshot.offsets[path] = boundary.size;
        snapshot.identities[path] = { dev: boundary.dev, ino: boundary.ino };
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
  const isRecord = (value) =>
    value !== null && typeof value === 'object' && !Array.isArray(value);
  if (
    !parsed ||
    typeof parsed !== 'object' ||
    parsed['version'] !== SNAPSHOT_VERSION ||
    parsed['engine'] !== 'claude' ||
    typeof parsed['sessionLog'] !== 'string' ||
    !isRecord(parsed['offsets']) ||
    !isRecord(parsed['identities']) ||
    parsed['identityBound'] !== true ||
    typeof parsed['startedAt'] !== 'string' ||
    Number.isNaN(Date.parse(parsed['startedAt']))
  ) {
    return null;
  }
  const offsetPaths = Object.keys(parsed['offsets']);
  const identityPaths = Object.keys(parsed['identities']);
  if (
    nonNegativeInteger(parsed['offsets'][parsed['sessionLog']]) === null ||
    offsetPaths.length !== identityPaths.length ||
    offsetPaths.some((entry) => {
      const identity = parsed['identities'][entry];
      return (
        nonNegativeInteger(parsed['offsets'][entry]) === null ||
        !isRecord(identity) ||
        nonNegativeInteger(identity['dev']) === null ||
        nonNegativeInteger(identity['ino']) === null
      );
    }) ||
    identityPaths.some((entry) => parsed['offsets'][entry] === undefined)
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

  const candidate = readSnapshot(args.start);
  const discovered = discoverSessionLog(args);

  // A snapshot's `identityBound` is evidence about the pass that wrote it, not
  // about this one. The start file sits at a fixed path that is stable across
  // every pass of an autonomous run, and a failed snapshot leaves the previous
  // pass's file in place — so a pass whose snapshot step failed would otherwise
  // measure from its predecessor's baseline and call the result scoped.
  //
  // Reject on a positive contradiction only: discovery identity-bound this pass
  // to a different log. Discovery that cannot identity-bind has not disproved
  // the snapshot, and downgrading there would cost measurements in every
  // environment where the session id is simply unavailable.
  const staleSnapshot =
    candidate !== null &&
    discovered !== null &&
    discovered.identityBound === true &&
    discovered.path !== candidate.sessionLog;
  const snapshot = staleSnapshot ? null : candidate;
  const sessionLog =
    snapshot?.sessionLog ?? discovered?.path ?? candidate?.sessionLog ?? null;

  if (sessionLog === null || fileSize(sessionLog) === null) {
    // No usable log. This must never serialise as zero: an engine that
    // reported nothing would otherwise look free and skew every average in its
    // favour, and that holds for every bucket, not only the token ones.
    return emit({
      mode: 'delta',
      enabled: true,
      tokenSource: 'unavailable',
      reason: 'no-session-log',
      tokensFile: null,
      lanesFile: null,
      engineVersion: null,
      durationSeconds: null,
      turns: null,
      error: null,
    });
  }

  const offsets = snapshot ? snapshot.offsets : {};
  const collected = collect(sessionLog, offsets, snapshot?.identities ?? {});

  let tokenSource;
  let reason = null;
  if (collected.tokens.length === 0 || collected.degraded) {
    tokenSource = 'unavailable';
    reason =
      collected.degradedReason ??
      (collected.degraded ? 'degraded' : 'no-turns');
  } else if (!snapshot || collected.rewound) {
    // Without a usable start snapshot, or after the log rewound under us, the
    // numbers are an unattributed session total rather than a measurement of
    // this pass. Heuristic discovery can select another concurrent session, so
    // this value has no directional bound and must not be used for pass-cost
    // comparison.
    tokenSource = 'unscoped-session';
    reason = collected.rewound
      ? 'log-rewound'
      : staleSnapshot
        ? 'snapshot-not-this-session'
        : 'no-start-snapshot';
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

  // An `unavailable` record has declared its own inputs unusable, so it must
  // not go on to report values extracted from them. `reason` is the exception:
  // it is diagnostic only and cannot upgrade provenance.
  const measured = tokenSource !== 'unavailable';

  return emit({
    mode: 'delta',
    enabled: true,
    tokenSource,
    reason,
    tokensFile,
    lanesFile,
    engineVersion: measured ? collected.engineVersion : null,
    durationSeconds,
    turns: measured ? collected.turns : null,
    error: null,
  });
}

function main(argv) {
  let args;
  try {
    args = parseArgs(argv);
  } catch (error) {
    const requestedMode =
      argv[0] === 'snapshot' || argv[0] === 'delta' ? argv[0] : null;
    const message = error instanceof Error ? error.message : String(error);
    if (requestedMode === 'snapshot') {
      return emit({
        mode: 'snapshot',
        enabled: true,
        sessionLog: null,
        snapshotFile: null,
        scoped: false,
        error: message,
      });
    }
    if (requestedMode === 'delta') {
      return emit({
        mode: 'delta',
        enabled: true,
        tokenSource: 'unavailable',
        reason: 'error',
        tokensFile: null,
        lanesFile: null,
        engineVersion: null,
        durationSeconds: null,
        turns: null,
        error: message,
      });
    }
    return emit({ mode: null, enabled: false, error: message });
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
      turns: null,
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
    // A failed snapshot must report the snapshot contract, not the delta one.
    // `scoped: false` is what tells the caller the start file it may still be
    // holding does not describe this pass.
    if (args.mode === 'snapshot') {
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
      reason: 'error',
      tokensFile: null,
      lanesFile: null,
      engineVersion: null,
      durationSeconds: null,
      turns: null,
      error: message,
    });
  }
}

process.exitCode = main(process.argv.slice(2));
