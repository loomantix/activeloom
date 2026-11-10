import {
  appendFileSync,
  closeSync,
  existsSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';
import { fail, LedgerError } from './errors.js';
import { parseJsonOrFail } from './io.js';

/**
 * Silent-mode ledger storage. A PR pinned silent keeps every ledger comment,
 * reply and thread here instead of on GitHub, outside every worktree.
 */

/** Local comment IDs start here, above any ID GitHub has issued. */
export const LOCAL_ID_BASE = 2 ** 52;
export const LOCAL_THREAD_PREFIX = 'PRRT_local_';
export const SILENT_ROOT_ENV = 'ACTIVELOOM_SILENT_ROOT';
export const SILENT_FORCE_ENV = 'ACTIVELOOM_REVIEW_SILENT';
const STORE_VERSION = 1;
const LOCK_WAIT_MS = 10_000;

export interface LocalReviewComment extends Record<string, unknown> {
  id: number;
  body: string;
  user: { login: string };
  path: string | null;
  line: number | null;
  side: string | null;
  subject_type: 'line' | 'file';
  commit_id: string | null;
  in_reply_to_id?: number;
  created_at: string;
  updated_at: string;
}

export interface LocalIssueComment extends Record<string, unknown> {
  id: number;
  body: string;
  user: { login: string };
  created_at: string;
  updated_at: string;
  last_edited_at: string | null;
}

export interface LocalThread {
  id: string;
  root_id: number;
  resolved: boolean;
}

export interface LocalState {
  version: number;
  next: number;
  review_comments: LocalReviewComment[];
  issue_comments: LocalIssueComment[];
  threads: LocalThread[];
  summary_post: { comment_id: number; sha256: string } | null;
}

export interface ModePin {
  version: number;
  mode: 'silent';
  repo: string;
  pr: number;
  actor: string;
  pinned_at: string;
  source: string;
}

export function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export function isLocalId(id: number): boolean {
  return Number.isSafeInteger(id) && id > LOCAL_ID_BASE;
}

export function isLocalThreadId(id: string): boolean {
  return id.startsWith(LOCAL_THREAD_PREFIX);
}

/** Split OWNER/REPO, failing on anything else. */
export function splitRepo(repo: string): [string, string] {
  const parts = repo.split('/');
  if (
    parts.length !== 2 ||
    !/^[A-Za-z0-9_.-]+$/.test(parts[0] ?? '') ||
    !/^[A-Za-z0-9_.-]+$/.test(parts[1] ?? '')
  ) {
    fail('--repo must be OWNER/REPO');
  }
  return [parts[0]!, parts[1]!];
}

/** Directory for one PR's silent store under the shared review root. */
export function storeDir(root: string, repo: string, pr: number): string {
  const [owner, name] = splitRepo(repo);
  if (!Number.isSafeInteger(pr) || pr < 1) fail('--pr must be positive');
  return join(root, `${owner}-${name}-${pr}`, 'silent');
}

/** Resolve the review state root: `<git-common-dir>/activeloom-review`. */
export function reviewRoot(gitCommonDir: () => string): string {
  const override = process.env[SILENT_ROOT_ENV];
  if (override) {
    if (!isAbsolute(override)) fail(`${SILENT_ROOT_ENV} must be absolute`);
    return override;
  }
  return join(resolve(gitCommonDir().trim()), 'activeloom-review');
}

// --- Review profile: the `silent.repos` list only ---------------------------

function profilePath(): string {
  const explicit = process.env['ACTIVELOOM_REVIEW_PROFILE'];
  if (explicit) {
    if (!isAbsolute(explicit))
      fail('ACTIVELOOM_REVIEW_PROFILE must be an absolute path');
    return explicit;
  }
  const base = process.env['XDG_CONFIG_HOME'] || join(homedir(), '.config');
  return join(base, 'activeloom', 'review-profile.json');
}

/** Match OWNER/NAME against `owner/name`, `owner/*` or `*`, ignoring case. */
export function silentPatternMatches(pattern: string, repo: string): boolean {
  const target = repo.toLowerCase();
  const value = pattern.toLowerCase();
  if (value === '*') return true;
  if (value.endsWith('/*')) return target.startsWith(value.slice(0, -1));
  return value === target;
}

/**
 * Whether the user's review profile marks this repository silent. A profile
 * that cannot be read fails closed: posting to a repository the user wanted
 * kept quiet cannot be undone.
 */
export function profileSaysSilent(repo: string): string | null {
  if (process.env[SILENT_FORCE_ENV] === '1') return `env:${SILENT_FORCE_ENV}`;
  const path = profilePath();
  if (!existsSync(path)) return null;
  const document = parseJsonOrFail<Record<string, unknown>>(
    readFileSync(path, 'utf8'),
    `review profile is unreadable: ${path}; cannot decide silent mode`,
  );
  const silent = document?.['silent'];
  if (silent === undefined) return null;
  const repos = (silent as { repos?: unknown } | null)?.repos;
  if (!Array.isArray(repos) || repos.some((p) => typeof p !== 'string'))
    fail(`review profile silent.repos is malformed: ${path}`);
  const hit = (repos as string[]).find((p) => silentPatternMatches(p, repo));
  return hit === undefined ? null : `profile:${hit}`;
}

// --- Store -------------------------------------------------------------------

function emptyState(): LocalState {
  return {
    version: STORE_VERSION,
    next: 1,
    review_comments: [],
    issue_comments: [],
    threads: [],
    summary_post: null,
  };
}

function writeAtomic(path: string, text: string): void {
  const tmp = `${path}.${process.pid}.tmp`;
  const fd = openSync(tmp, 'w', 0o600);
  try {
    writeFileSync(fd, text);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(tmp, path);
}

function sleep(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as { code?: string }).code === 'EPERM';
  }
}

/** One PR's silent store. Every mutation runs under an exclusive lock. */
export class LocalStore {
  readonly dir: string;

  constructor(
    root: string,
    readonly repo: string,
    readonly pr: number,
  ) {
    this.dir = storeDir(root, repo, pr);
  }

  private get statePath(): string {
    return join(this.dir, 'state.json');
  }

  private get modePath(): string {
    return join(this.dir, 'mode.json');
  }

  get scratchpadPath(): string {
    return join(this.dir, 'scratchpad.md');
  }

  pin(): ModePin | null {
    if (!existsSync(this.modePath)) return null;
    const pin = parseJsonOrFail<ModePin>(
      readFileSync(this.modePath, 'utf8'),
      `silent mode pin is unreadable: ${this.modePath}`,
    );
    if (
      pin?.mode !== 'silent' ||
      pin.repo.toLowerCase() !== this.repo.toLowerCase() ||
      pin.pr !== this.pr
    )
      fail(`silent mode pin does not match ${this.repo}#${this.pr}`);
    return pin;
  }

  writePin(actor: string, source: string): ModePin {
    this.ensureDir();
    const pin: ModePin = {
      version: STORE_VERSION,
      mode: 'silent',
      repo: this.repo,
      pr: this.pr,
      actor,
      pinned_at: nowIso(),
      source,
    };
    writeAtomic(this.modePath, `${JSON.stringify(pin, null, 2)}\n`);
    if (!existsSync(this.scratchpadPath)) {
      writeFileSync(
        this.scratchpadPath,
        `# Silent review scratchpad: ${this.repo}#${this.pr}\n\nPinned ${pin.pinned_at} (${source}). Nothing in this file is posted to GitHub.\n`,
        { mode: 0o600 },
      );
    }
    return pin;
  }

  private ensureDir(): void {
    mkdirSync(this.dir, { recursive: true, mode: 0o700 });
    if (lstatSync(this.dir).isSymbolicLink())
      fail(`silent store cannot be a symlink: ${this.dir}`);
  }

  read(): LocalState {
    if (!existsSync(this.statePath)) return emptyState();
    const state = parseJsonOrFail<LocalState>(
      readFileSync(this.statePath, 'utf8'),
      `silent ledger is unreadable: ${this.statePath}`,
    );
    if (state?.version !== STORE_VERSION)
      fail(`unsupported silent ledger version in ${this.statePath}`);
    return state;
  }

  /** Read, mutate and atomically replace the state under the store lock. */
  update<T>(mutate: (state: LocalState) => T): T {
    this.ensureDir();
    const lock = join(this.dir, '.lock');
    const deadline = Date.now() + LOCK_WAIT_MS;
    for (;;) {
      try {
        const fd = openSync(lock, 'wx', 0o600);
        writeFileSync(fd, String(process.pid));
        closeSync(fd);
        break;
      } catch (error) {
        if ((error as { code?: string }).code !== 'EEXIST') throw error;
        const holder = Number.parseInt(readFileSync(lock, 'utf8'), 10);
        if (!Number.isSafeInteger(holder) || !pidAlive(holder)) {
          try {
            unlinkSync(lock);
          } catch {
            // another waiter removed it first
          }
          continue;
        }
        if (Date.now() > deadline)
          fail(`silent ledger is locked by process ${holder}: ${lock}`);
        sleep(50);
      }
    }
    try {
      const state = this.read();
      const result = mutate(state);
      writeAtomic(this.statePath, `${JSON.stringify(state, null, 2)}\n`);
      return result;
    } finally {
      unlinkSync(lock);
    }
  }

  /** Append a human-readable line to the scratchpad. Never fatal. */
  note(text: string): void {
    try {
      this.ensureDir();
      appendFileSync(this.scratchpadPath, `\n${text}\n`, { mode: 0o600 });
    } catch (error) {
      if (error instanceof LedgerError) throw error;
    }
  }
}

/** Allocate the next local comment ID. */
export function allocateId(state: LocalState): number {
  const id = LOCAL_ID_BASE + state.next;
  state.next += 1;
  return id;
}

export function storeExists(root: string, repo: string, pr: number): boolean {
  return existsSync(join(storeDir(root, repo, pr), 'mode.json'));
}
