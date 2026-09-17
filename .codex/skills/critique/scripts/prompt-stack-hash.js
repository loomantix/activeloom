#!/usr/bin/env node
// Computes promptStackSha256 and repoInstructionsSha256 for review pass telemetry.
// Stack and repo instruction digests remain separate so fleet-wide prompts can be correlated across repos.
// Always exits 0 and prints a single JSON object.

import { createHash } from 'node:crypto';
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readSync,
  realpathSync,
} from 'node:fs';
import {
  basename,
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Version of the hash input definition, mixed into the domain separator so
 * definition changes invalidate older digests.
 */
const HASH_INPUT_VERSION = 2;

/** Manifest listing prompt stack files emitted by the upstream renderer. */
const MANIFEST_NAME = 'prompt-stack.json';

/** Manifest schema version supported by this helper. */
const SUPPORTED_MANIFEST_VERSION = 1;

/** Semver format required for prompt stack version. */
const VERSION_RE = /^\d+\.\d+\.\d+$/;

const MAX_INPUT_BYTES = 1024 * 1024;

/** Harness root derived from script path, matching the manifest declaration. */
const HARNESS_ROOT = basename(
  dirname(dirname(dirname(dirname(fileURLToPath(import.meta.url))))),
);

/** Repo-local instruction files hashed across all engines. */
const REPO_INSTRUCTION_FILES = ['AGENTS.md', 'CLAUDE.md'];

/** Read a bounded regular file within root, refusing symlinks and escapes. */
function readBoundedRegular(root, relativePath) {
  let realRoot;
  try {
    realRoot = realpathSync(root);
  } catch (error) {
    const code = error?.code;
    return { state: code === 'ENOENT' ? 'absent' : 'error' };
  }

  const candidate = resolve(realRoot, relativePath);
  const fromRoot = relative(realRoot, candidate);
  if (
    fromRoot === '' ||
    fromRoot === '..' ||
    fromRoot.startsWith(`..${sep}`) ||
    isAbsolute(fromRoot)
  ) {
    return { state: 'error' };
  }

  let current = realRoot;
  try {
    for (const part of fromRoot.split(sep)) {
      current = join(current, part);
      if (lstatSync(current).isSymbolicLink()) {
        return { state: 'error' };
      }
    }
  } catch (error) {
    const code = error?.code;
    return {
      state: code === 'ENOENT' || code === 'ENOTDIR' ? 'absent' : 'error',
    };
  }

  let descriptor;
  try {
    descriptor = openSync(
      candidate,
      constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0),
    );
    const stat = fstatSync(descriptor);
    if (!stat.isFile() || stat.size > MAX_INPUT_BYTES) {
      return { state: 'error' };
    }
    const bytes = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < bytes.length) {
      const count = readSync(descriptor, bytes, offset, bytes.length - offset);
      if (count === 0) break;
      offset += count;
    }
    return { state: 'present', bytes: bytes.subarray(0, offset) };
  } catch (error) {
    const code = error?.code;
    return {
      state: code === 'ENOENT' || code === 'ENOTDIR' ? 'absent' : 'error',
    };
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
  }
}

function readManifest(root) {
  const found = readBoundedRegular(root, join(HARNESS_ROOT, MANIFEST_NAME));
  if (found.state === 'absent') {
    return { error: `no ${MANIFEST_NAME} under ${HARNESS_ROOT}` };
  }
  if (found.state !== 'present') {
    return { error: `${MANIFEST_NAME} could not be read` };
  }
  const raw = found.bytes.toString('utf8');

  let doc;
  try {
    doc = JSON.parse(raw);
  } catch {
    return { error: `${MANIFEST_NAME} is not valid JSON` };
  }
  if (doc === null || typeof doc !== 'object' || Array.isArray(doc)) {
    return { error: `${MANIFEST_NAME} is not an object` };
  }
  if (doc.manifestVersion !== SUPPORTED_MANIFEST_VERSION) {
    return {
      error: `${MANIFEST_NAME} declares unsupported manifestVersion ${JSON.stringify(
        doc.manifestVersion,
      )}`,
    };
  }
  if (doc.root !== HARNESS_ROOT) {
    return {
      error: `${MANIFEST_NAME} declares root ${JSON.stringify(
        doc.root,
      )}, not ${HARNESS_ROOT}`,
    };
  }
  if (
    typeof doc.promptStackVersion !== 'string' ||
    !VERSION_RE.test(doc.promptStackVersion)
  ) {
    return { error: `${MANIFEST_NAME} declares no MAJOR.MINOR.PATCH version` };
  }
  if (!Array.isArray(doc.files) || doc.files.length === 0) {
    return { error: `${MANIFEST_NAME} declares no prompt files` };
  }

  const prefix = `${HARNESS_ROOT}/`;
  const files = [];
  for (const entry of doc.files) {
    if (
      typeof entry !== 'string' ||
      entry.length === 0 ||
      !entry.startsWith(prefix) ||
      entry === `${HARNESS_ROOT}/${MANIFEST_NAME}` ||
      entry.includes('\\') ||
      entry
        .split('/')
        .some((part) => part === '' || part === '.' || part === '..')
    ) {
      return {
        error: `${MANIFEST_NAME} declares an unusable path ${JSON.stringify(entry)}`,
      };
    }
    files.push(entry);
  }
  if (new Set(files).size !== files.length) {
    return { error: `${MANIFEST_NAME} declares a duplicate path` };
  }

  return { version: doc.promptStackVersion, files };
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = argv[i + 1];
    const take = (name) => {
      if (next === undefined || next.startsWith('--')) {
        throw new Error(`missing argument for ${name}`);
      }
      i += 1;
      return next;
    };
    switch (arg) {
      case '--repo-root':
        args.repoRoot = take(arg);
        break;
      default:
        throw new Error(`unknown argument ${arg}`);
    }
  }
  return args;
}

/** Normalise line endings to LF and strip UTF-8 BOM before hashing. */
function normalise(bytes) {
  const withoutBom =
    bytes.length >= 3 &&
    bytes[0] === 0xef &&
    bytes[1] === 0xbb &&
    bytes[2] === 0xbf
      ? bytes.subarray(3)
      : bytes;
  return Buffer.from(
    withoutBom.toString('latin1').replace(/\r\n?/g, '\n'),
    'latin1',
  );
}

/** Read and hash a single declared file, distinguishing absent from unreadable. */
function readDeclared(root, relativePath) {
  const found = readBoundedRegular(root, relativePath);
  if (found.state !== 'present') return found;
  return {
    state: 'present',
    digest: createHash('sha256').update(normalise(found.bytes)).digest('hex'),
  };
}

/** Compute a deterministic digest over a set of files sorted by path. */
function digestOver(name, root, files) {
  const ordered = [...files].sort((left, right) =>
    left < right ? -1 : left > right ? 1 : 0,
  );
  const hash = createHash('sha256');
  hash.update(`loom-review-prompt-hash/v${HASH_INPUT_VERSION}/${name}\n`);
  let present = 0;
  for (const relative of ordered) {
    const found = readDeclared(root, relative);
    if (found.state === 'error') {
      return {
        sha256: null,
        declared: ordered.length,
        present: 0,
        failed: true,
      };
    }
    if (found.state === 'present') {
      present += 1;
    }
    hash.update(
      `${relative}\0${found.state === 'present' ? found.digest : '-'}\n`,
    );
  }
  return {
    sha256: present === 0 ? null : hash.digest('hex'),
    declared: ordered.length,
    present,
    failed: false,
  };
}

/** Join independent abstention reasons into a single error string. */
function joinReasons(reasons) {
  const stated = reasons.filter((reason) => reason != null);
  return stated.length === 0 ? null : stated.join('; ');
}

/** The shape emitted when nothing could be computed at all. */
function abstained(message) {
  return {
    mode: 'hash',
    hashInputVersion: HASH_INPUT_VERSION,
    manifestVersion: SUPPORTED_MANIFEST_VERSION,
    harnessRoot: HARNESS_ROOT,
    promptStackSha256: null,
    promptStackVersion: null,
    repoInstructionsSha256: null,
    promptStack: null,
    repoInstructions: null,
    error: message,
  };
}

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  return 0;
}

function main(argv) {
  let args;
  try {
    args = parseArgs(argv);
  } catch (error) {
    return emit(
      abstained(error instanceof Error ? error.message : String(error)),
    );
  }

  try {
    const root = resolve(args.repoRoot ?? process.cwd());
    const manifest = readManifest(root);
    const stack = manifest.error
      ? { sha256: null, declared: 0, present: 0, failed: false }
      : digestOver('prompt-stack', root, manifest.files);
    const instructions = digestOver(
      'repo-instructions',
      root,
      REPO_INSTRUCTION_FILES,
    );
    return emit({
      mode: 'hash',
      hashInputVersion: HASH_INPUT_VERSION,
      manifestVersion: SUPPORTED_MANIFEST_VERSION,
      harnessRoot: HARNESS_ROOT,
      promptStackSha256: stack.sha256,
      promptStackVersion: manifest.error ? null : manifest.version,
      repoInstructionsSha256: instructions.sha256,
      promptStack: { declared: stack.declared, present: stack.present },
      repoInstructions: {
        declared: instructions.declared,
        present: instructions.present,
      },
      error: joinReasons([
        manifest.error ??
          (stack.failed
            ? 'the prompt stack could not be read'
            : stack.declared > 0 && stack.present === 0
              ? `no declared prompt-stack file is present under ${HARNESS_ROOT}`
              : null),
        instructions.failed ? 'the repo instructions could not be read' : null,
      ]),
    });
  } catch (error) {
    return emit(
      abstained(error instanceof Error ? error.message : String(error)),
    );
  }
}

process.exitCode = main(process.argv.slice(2));
