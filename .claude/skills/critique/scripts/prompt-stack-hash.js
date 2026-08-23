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
import { join, resolve } from 'node:path';

/**
 * The version of the *hash input definition* — which files, in what order,
 * with what normalisation. It is not the semantic version of the prompt stack
 * (`promptStackVersion`), which this helper does not compute and must not be
 * confused with.
 *
 * It is mixed into the digest rather than reported beside it. A redefinition
 * that kept producing the same digests for the same files would silently
 * rewrite the meaning of every record already emitted; mixing the version in
 * makes v2 of the definition produce different digests by construction, so old
 * records keep meaning what they meant.
 */
const HASH_INPUT_VERSION = 1;

/**
 * The synced prompt files a review pass runs on, enumerated rather than
 * globbed.
 *
 * A glob makes the hash input depend on what happens to be on disk, so a file
 * arriving from an unrelated sync would read as a prompt-generation change.
 * The explicit list is the definition, and changing it is a redefinition that
 * bumps `HASH_INPUT_VERSION`.
 *
 * Scripts are excluded: the ledger bundle and the usage extractor are not
 * prompts, and folding them in would move the digest on every ledger release
 * while nothing about the review's instructions changed. The engine version
 * already travels in the record.
 *
 * This engine's finder lenses are plugin agents that live outside the synced
 * surface, so v1 of the definition does not cover them. That is a known gap,
 * not an oversight: a file this helper cannot read in a consumer checkout
 * cannot be part of a digest that has to be reproducible there.
 */
const PROMPT_STACK_FILES = [
  '.claude/MODEL_NOTES.md',
  '.claude/REVIEW_WORKFLOW.md',
  '.claude/references/local-review-ledger.md',
  '.claude/skills/critique/SKILL.md',
  '.claude/skills/deepcritique/SKILL.md',
  '.claude/skills/refactorpass/SKILL.md',
  '.claude/skills/reviewit/SKILL.md',
];

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

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  return 0;
}

function main(argv) {
  let args;
  try {
    args = parseArgs(argv);
  } catch (error) {
    return emit({
      mode: 'hash',
      hashInputVersion: HASH_INPUT_VERSION,
      promptStackSha256: null,
      repoInstructionsSha256: null,
      promptStack: null,
      repoInstructions: null,
      error: error instanceof Error ? error.message : String(error),
    });
  }

  try {
    const root = resolve(args.repoRoot ?? process.cwd());
    const stack = digestOver('prompt-stack', root, PROMPT_STACK_FILES);
    const instructions = digestOver(
      'repo-instructions',
      root,
      REPO_INSTRUCTION_FILES,
    );
    return emit({
      mode: 'hash',
      hashInputVersion: HASH_INPUT_VERSION,
      promptStackSha256: stack.sha256,
      repoInstructionsSha256: instructions.sha256,
      promptStack: { declared: stack.declared, present: stack.present },
      repoInstructions: {
        declared: instructions.declared,
        present: instructions.present,
      },
      error: stack.failed
        ? 'the prompt stack could not be read'
        : instructions.failed
          ? 'the repo instructions could not be read'
          : null,
    });
  } catch (error) {
    return emit({
      mode: 'hash',
      hashInputVersion: HASH_INPUT_VERSION,
      promptStackSha256: null,
      repoInstructionsSha256: null,
      promptStack: null,
      repoInstructions: null,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

process.exitCode = main(process.argv.slice(2));
