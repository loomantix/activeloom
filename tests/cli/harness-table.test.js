'use strict';

/**
 * The CLI's static harness table must match the canonical sync manifest.
 *
 * `cli/lib/detect.js` hard-codes the harness ids and roots because `add` needs
 * them before any upstream tree has been fetched. That makes it a second copy
 * of something `scripts/sync-targets.yml` already states, and the ids and roots
 * are not the same strings — gemini's id is `gemini` while its root is
 * `.agents`. A copy that drifts writes a `harnesses:` list the engine rejects,
 * or installs into a directory no harness reads.
 *
 * Rather than trust the copy, pin it here. The manifest is parsed with a
 * deliberately small YAML reader: this is the repo's only Node-side consumer of
 * that file, and adding a YAML dependency to a zero-dependency package's test
 * suite to check one mapping would cost more than it proves.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const { HARNESSES } = require(path.join(REPO_ROOT, 'cli', 'lib', 'detect.js'));

/**
 * Read `harnesses:` from the manifest as `{id: root}`.
 *
 * Scoped to exactly the shape being checked: the top-level `harnesses:` block,
 * whose children are two-space-indented ids each carrying a four-space-indented
 * `root:`. Anything else in the file is ignored. If the manifest's shape ever
 * changes this parser stops finding entries, and the emptiness assertion below
 * turns that into a failure rather than a silent pass.
 *
 * @returns {Map<string, string>}
 */
function parseHarnesses() {
  const text = fs.readFileSync(
    path.join(REPO_ROOT, 'scripts', 'sync-targets.yml'),
    'utf8',
  );
  const lines = text.split('\n');
  const found = new Map();

  let inBlock = false;
  let currentId = null;
  for (const line of lines) {
    if (/^harnesses:\s*$/.test(line)) {
      inBlock = true;
      continue;
    }
    if (!inBlock) continue;
    // A non-indented, non-comment, non-blank line ends the block.
    if (/^[^\s#]/.test(line)) break;

    const idMatch = /^ {2}([A-Za-z0-9_-]+):\s*$/.exec(line);
    if (idMatch) {
      currentId = idMatch[1];
      continue;
    }
    const rootMatch = /^ {4}root:\s*(\S+)\s*$/.exec(line);
    if (rootMatch && currentId) {
      found.set(currentId, rootMatch[1]);
      currentId = null;
    }
  }
  return found;
}

test('the manifest still has a parseable harnesses block', () => {
  const manifest = parseHarnesses();
  assert.ok(
    manifest.size > 0,
    'parsed zero harnesses from scripts/sync-targets.yml — the manifest shape changed and this test is no longer checking anything',
  );
});

test('CLI harness ids and roots match scripts/sync-targets.yml', () => {
  const manifest = parseHarnesses();
  const cli = new Map(HARNESSES.map((h) => [h.id, h.root]));

  assert.deepStrictEqual(
    [...cli.keys()].sort(),
    [...manifest.keys()].sort(),
    'harness ids differ between cli/lib/detect.js and the manifest',
  );

  for (const [id, root] of manifest) {
    assert.strictEqual(
      cli.get(id),
      root,
      `harness "${id}" root differs from the manifest`,
    );
  }
});

test('gemini is the id and .agents is the root', () => {
  // Named explicitly because it is the pairing a second copy gets wrong, and a
  // deepStrictEqual failure elsewhere would not say why it matters.
  const gemini = HARNESSES.find((h) => h.id === 'gemini');
  assert.ok(gemini, 'no harness with id "gemini"');
  assert.strictEqual(gemini.root, '.agents');
});
