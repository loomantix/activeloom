'use strict';

/**
 * Verifies that the CLI's static harness table matches scripts/sync-targets.yml.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const { HARNESSES } = require(path.join(REPO_ROOT, 'cli', 'lib', 'detect.js'));

/**
 * Read `harnesses:` from the manifest as `{id: {root, legacyConfig}}`.
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
      found.set(currentId, {});
      continue;
    }
    const rootMatch = /^ {4}root:\s*(\S+)\s*$/.exec(line);
    if (rootMatch && currentId) {
      found.get(currentId).root = rootMatch[1];
      continue;
    }
    const legacyMatch = /^ {4}legacy_config:\s*(\S+)\s*$/.exec(line);
    if (legacyMatch && currentId) {
      found.get(currentId).legacyConfig = legacyMatch[1];
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
  const cli = new Map(
    HARNESSES.map((h) => [
      h.id,
      { root: h.root, legacyConfig: h.legacyConfig },
    ]),
  );

  assert.deepStrictEqual(
    [...cli.keys()].sort(),
    [...manifest.keys()].sort(),
    'harness ids differ between cli/lib/detect.js and the manifest',
  );

  for (const [id, config] of manifest) {
    assert.deepStrictEqual(
      cli.get(id),
      config,
      `harness "${id}" config differs from the manifest`,
    );
  }
});

test('gemini is the id and .agents is the root', () => {
  // Pin the mapping between harness id 'gemini' and root '.agents'.
  const gemini = HARNESSES.find((h) => h.id === 'gemini');
  assert.ok(gemini, 'no harness with id "gemini"');
  assert.strictEqual(gemini.root, '.agents');
});
