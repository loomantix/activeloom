'use strict';

/**
 * Verifies documentation across README and getting-started matches cli/lib/tiers.js.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const { TIERS, RECOMMENDED_TIER, resolveTier } = require(
  path.join(REPO_ROOT, 'cli', 'lib', 'tiers.js'),
);

const README = fs.readFileSync(path.join(REPO_ROOT, 'README.md'), 'utf8');
const GETTING_STARTED = fs.readFileSync(
  path.join(REPO_ROOT, 'docs', 'getting-started.md'),
  'utf8',
);

/**
 * The command a tier is reached by, as it appears in prose.
 *
 * @param {import('../../cli/lib/tiers').Tier} tier
 */
const commandStem = (tier) =>
  tier.command.replace(/^npx activeloom /, '').replace(/ <skill>$/, '');

/**
 * Whether a page presents a tier's command as a command.
 *
 * @param {string} body
 * @param {string} stem
 */
const mentionsCommand = (body, stem) => {
  const escaped = stem.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(?:activeloom(?:\\.js)?\\s+|\`)${escaped}\\b`).test(body);
};

test('getting-started names every tier by number and command', () => {
  for (const tier of TIERS) {
    assert.ok(
      mentionsCommand(GETTING_STARTED, commandStem(tier)),
      `docs/getting-started.md does not mention \`${commandStem(tier)}\` (tier ${tier.n})`,
    );
    assert.match(
      GETTING_STARTED,
      new RegExp(`Tier ${tier.n}`),
      `docs/getting-started.md does not mention Tier ${tier.n}`,
    );
  }
});

test('the README names every tier command', () => {
  for (const tier of TIERS) {
    assert.ok(
      mentionsCommand(README, commandStem(tier)),
      `README.md does not mention \`${commandStem(tier)}\` (tier ${tier.n})`,
    );
  }
});

test('both docs state which tier is recommended', () => {
  for (const [name, body] of [
    ['README.md', README],
    ['docs/getting-started.md', GETTING_STARTED],
  ]) {
    assert.match(
      body,
      new RegExp(`Tier ${RECOMMENDED_TIER} is the recommended tier`),
      `${name} does not say Tier ${RECOMMENDED_TIER} is the recommended tier`,
    );
  }
});

test('the recommended tier is not what bare `init` resolves to', () => {
  // Bare init resolves to Tier 1, so recommended tier is distinct from default.
  assert.notStrictEqual(
    resolveTier({}).n,
    RECOMMENDED_TIER,
    'bare `init` now resolves to the recommended tier — say "default" in the docs again',
  );
  assert.strictEqual(resolveTier({ sync: true }).n, RECOMMENDED_TIER);
});

test('both docs confine the GitHub App to tier 3', () => {
  // Verify documentation confines the GitHub App requirement to Tier 3.
  for (const [name, body] of [
    ['README.md', README],
    ['docs/getting-started.md', GETTING_STARTED],
  ]) {
    assert.match(
      body,
      /GitHub App is only ever needed at Tier 3/,
      `${name} does not confine the GitHub App to Tier 3`,
    );
  }
});

test('getting-started does not open with a credential prerequisite', () => {
  const preamble = GETTING_STARTED.split('## Tier 0')[0];
  assert.ok(preamble.length > 0, 'no Tier 0 heading found');
  for (const forbidden of [
    'SYNC_APP_ID',
    'SYNC_APP_PRIVATE_KEY',
    'UPSTREAM_READ_TOKEN',
  ]) {
    assert.ok(
      !preamble.includes(forbidden),
      `docs/getting-started.md names ${forbidden} before Tier 0 — that is the barrier the tiering removes`,
    );
  }
});

test('tier 0 is documented as needing nothing', () => {
  const tier0 = TIERS[0];
  assert.strictEqual(tier0.credential, 'none');
  // Tier 0 requires no credentials or tokens.
  const section =
    GETTING_STARTED.split('## Tier 0')[1].split('<a id="tier-1">')[0];
  assert.ok(!/gh secret set/.test(section), 'Tier 0 section sets a secret');
  assert.ok(
    !/GitHub App/.test(section),
    'Tier 0 section mentions a GitHub App',
  );
});

test('Tier 3 keeps private keys out of process arguments', () => {
  assert.match(
    GETTING_STARTED,
    /gh secret set SYNC_APP_PRIVATE_KEY[^\n]*< key\.pem/,
  );
  assert.doesNotMatch(GETTING_STARTED, /--body "\$\(cat key\.pem\)"/);
});

test('Tier 3 names every App permission its synced targets require', () => {
  const template = fs.readFileSync(
    path.join(
      REPO_ROOT,
      '.github',
      'workflows',
      'sync-from-upstream.yml.template',
    ),
    'utf8',
  );
  for (const permission of [
    'Contents: write',
    'Pull requests: write',
    'Workflows: write',
  ]) {
    assert.match(GETTING_STARTED, new RegExp(permission));
  }
  assert.match(template, /`workflows: write`/);
  assert.match(template, /ships workflows under\n#\s+`\.github\/workflows\/`/);
});

test('Tier 2 documents approval-required pull-request checks', () => {
  assert.match(GETTING_STARTED, /Approve workflows to run/);
  assert.match(GETTING_STARTED, /Push-triggered workflows are not started/);
});
