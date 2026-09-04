#!/usr/bin/env node
// Identify the prompt generation one review pass ran on.
//
// A telemetry record says what a pass cost. Without a prompt identity it
// cannot say what the pass was *running*, so "did that prompt change help"
// has no answer: findings-per-token is a property of the prompt as much as of
// the model. This helper computes the two digests the ledger already carries
// slots for, `promptStackSha256` and `repoInstructionsSha256`.
//
// The two stay separate and are never collapsed into one figure. The synced
// stack is fleet-wide and moves when upstream moves; repo-local instructions
// are per-repository by definition. A combined digest would make every
// repository look like a different prompt generation forever, which destroys
// exactly the cross-repository correlation the hash exists to enable.
//
// This is deliberately a separate script from `usage-snapshot.js` beside it.
// That one reads the session transcript — every file read and every command
// run — which is why it lives in the engine's own skill rather than in the
// vendored ledger bundle. This one reads nothing but files already checked
// into the repository the pass is reviewing, so it inherits none of that
// argument and none of that blast radius.
//
// Always exits 0 and always prints one JSON object. A telemetry defect must
// never fail a review that found real defects.

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The version of the *hash input definition* — which files, in what order,
 * with what normalisation. It is not the semantic version of the prompt stack
 * (`promptStackVersion`), which this helper reports from the manifest without
 * computing, and which must not be confused with it: one identifies how the
 * digest was taken, the other orders the prompts themselves.
 *
 * It is mixed into the digest rather than reported beside it. A redefinition
 * that kept producing the same digests for the same files would silently
 * rewrite the meaning of every record already emitted; mixing the version in
 * makes v2 of the definition produce different digests by construction, so old
 * records keep meaning what they meant.
 */
const HASH_INPUT_VERSION = 2;

/**
 * The manifest emitted beside this harness root by the upstream renderer, and
 * synced into the consumer alongside the prompts it names.
 *
 * Version 1 of the hash input carried the file list in this script, which meant
 * two engine copies of one helper had to keep agreeing on both the membership
 * and the byte order of that list forever — a convention, enforced by nothing,
 * whose failure mode is two identities minted for one prompt generation. The
 * list is now a build output of the repository that owns the prompts: it knows
 * what it shipped, a declaration naming a file it does not have fails the
 * render, and this helper reads what it was given.
 *
 * Reading a manifest is still not a glob. The hash input remains an enumerated
 * declaration; what changed is who writes it down.
 *
 * Scripts stay excluded, and the exclusions the declaration encodes are
 * documented with it upstream. Only prompt files this helper can read in a
 * consumer checkout can be part of a digest that has to be reproducible there.
 */
const MANIFEST_NAME = 'prompt-stack.json';

/**
 * The manifest schema this helper understands.
 *
 * A consumer can hold a manifest newer than its copy of this script — sync
 * ships files, not transactions. An unrecognised schema abstains rather than
 * guessing at a shape it was not written for: a digest computed over a
 * misread declaration is a different stack's digest wearing this one's name.
 */
const SUPPORTED_MANIFEST_VERSION = 1;

/** `MAJOR.MINOR.PATCH`, the only shape the upstream version file may hold. */
const VERSION_RE = /^\d+\.\d+\.\d+$/;

/**
 * The harness root this copy of the helper belongs to, derived from its own
 * location rather than hard-coded.
 *
 * The script is synced to `<root>/skills/critique/scripts/`, so the fourth
 * parent names its root. Deriving it is what lets the two engine copies of this
 * file be byte-identical: a hard-coded root was the last remaining reason for
 * them to differ, and every line that differs between two copies of one helper
 * is a line that can drift.
 *
 * It is cross-checked against the `root` the manifest declares, so a copy that
 * ended up somewhere unexpected abstains instead of hashing a stack that is not
 * its own.
 */
const HARNESS_ROOT = basename(
  dirname(dirname(dirname(dirname(fileURLToPath(import.meta.url))))),
);

/**
 * Repo-local agent instructions, at the repository root.
 *
 * Both names are declared for both engines rather than each engine hashing
 * only the file it reads. The same repository state must produce the same
 * `repoInstructionsSha256` whichever engine emitted the record, or the field
 * cannot be joined across engines at all — and a repository that carries both
 * files is the common case, not the exception.
 *
 * Nested instruction files are out of scope for v1: their discovery depends on
 * which directories a pass happened to touch, which is not reproducible.
 */
const REPO_INSTRUCTION_FILES = ['AGENTS.md', 'CLAUDE.md'];

/**
 * Read and validate the shipped stack declaration.
 *
 * Every rejection below abstains rather than falling back to some other list.
 * A fallback would make an unreadable or malformed manifest produce a digest
 * anyway — the one outcome worse than no digest, because it looks measured. The
 * caller reports the reason so a consumer whose sync did not deliver the
 * manifest can be told apart from one whose manifest is corrupt.
 */
function readManifest(root) {
  let raw;
  try {
    raw = readFileSync(join(root, HARNESS_ROOT, MANIFEST_NAME), 'utf8');
  } catch (error) {
    const code = error?.code;
    if (code === 'ENOENT' || code === 'ENOTDIR') {
      return { error: `no ${MANIFEST_NAME} under ${HARNESS_ROOT}` };
    }
    return { error: `${MANIFEST_NAME} could not be read` };
  }

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
    // Not pedantry: a manifest for another harness names another engine's
    // prompt generation, and hashing it here would file this engine's pass
    // under that engine's identity.
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
    // Each entry is a path this helper is about to read. Constraining it to a
    // plain relative path inside the declared root is what keeps a manifest —
    // an ordinary file, editable in any consumer checkout — from directing a
    // read outside the prompt root it describes.
    if (
      typeof entry !== 'string' ||
      entry.length === 0 ||
      !entry.startsWith(prefix) ||
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

/**
 * Normalise line endings and strip a UTF-8 BOM before hashing.
 *
 * A checkout with `core.autocrlf` on, or a consumer whose sync landed CRLF,
 * holds the same prompt as a checkout that did not — and a digest that
 * disagreed would report a prompt-generation difference that does not exist.
 * The sync engine has already had a render-fidelity defect in exactly this
 * area, so leaving the decision implicit is not an option.
 *
 * Nothing else is normalised. Trailing whitespace and blank-line changes are
 * real edits to a prompt file and must move the digest.
 *
 * `latin1` is a byte-preserving round trip, so this rewrites the CR bytes
 * without decoding the file as text.
 */
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

/**
 * Read one declared file.
 *
 * Absent and unreadable are different answers. A consumer that never received
 * a file — it opted out, or it predates the file — has a stack that really is
 * missing it, and recording the absence is what lets that consumer's drift be
 * seen. A file that exists and cannot be read is a failure, and a digest
 * computed over the rest of the stack would be a different stack's digest
 * wearing this one's name.
 */
function readDeclared(path) {
  let bytes;
  try {
    bytes = readFileSync(path);
  } catch (error) {
    const code = error?.code;
    if (code === 'ENOENT' || code === 'ENOTDIR') {
      return { state: 'absent' };
    }
    return { state: 'error' };
  }
  return {
    state: 'present',
    digest: createHash('sha256').update(normalise(bytes)).digest('hex'),
  };
}

/**
 * Digest one declared set.
 *
 * Paths are sorted here rather than trusted from the declaration, so the order
 * they happen to be written in above cannot become part of the definition. Two
 * engines that hashed the same stack in different orders would mint two
 * identities for one prompt generation, which reads downstream as a real
 * difference and is worse than having no hash.
 *
 * The digest is taken over per-file digests rather than concatenated content:
 * fixed-width records cannot be made to collide by moving a byte across a file
 * boundary. Each record carries its path, so a rename is a change. The domain
 * string carries the hash-input version and the set's name, so a one-file
 * stack can never collide with a one-file instruction set.
 */
function digestOver(name, root, files) {
  const ordered = [...files].sort((left, right) =>
    left < right ? -1 : left > right ? 1 : 0,
  );
  const hash = createHash('sha256');
  hash.update(`loom-review-prompt-hash/v${HASH_INPUT_VERSION}/${name}\n`);
  let present = 0;
  for (const relative of ordered) {
    const found = readDeclared(join(root, relative));
    if (found.state === 'error') {
      // Never a partial digest. A hash that covered some of the stack would be
      // indistinguishable from one that covered all of it.
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
    // A set with nothing in it is not a prompt stack. Emitting the digest of
    // "everything absent" would give every empty checkout one shared identity
    // that looks measured.
    sha256: present === 0 ? null : hash.digest('hex'),
    declared: ordered.length,
    present,
    failed: false,
  };
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
    // The two digests are independent, so a manifest problem must not cost the
    // record its repo-instructions digest as well. Only the prompt-stack half
    // abstains.
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
      // Reported beside the digest, never mixed into it. A version bump that
      // changed no prompt must not move the digest, and a prompt edit must move
      // it whether or not anyone remembered to bump the version.
      promptStackVersion: manifest.error ? null : manifest.version,
      repoInstructionsSha256: instructions.sha256,
      promptStack: { declared: stack.declared, present: stack.present },
      repoInstructions: {
        declared: instructions.declared,
        present: instructions.present,
      },
      error:
        manifest.error ??
        (stack.failed
          ? 'the prompt stack could not be read'
          : instructions.failed
            ? 'the repo instructions could not be read'
            : null),
    });
  } catch (error) {
    return emit(
      abstained(error instanceof Error ? error.message : String(error)),
    );
  }
}

process.exitCode = main(process.argv.slice(2));
