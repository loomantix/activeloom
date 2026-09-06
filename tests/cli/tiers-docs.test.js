'use strict';

/**
 * The docs and the code must describe the same ladder.
 *
 * `cli/lib/tiers.js` is the definition; `README.md`, `docs/getting-started.md`,
 * and the CLI's own `tiers` output are four descriptions of it. Prose drifts
 * silently — a tier renamed in code leaves stale docs that still read fine, and
 * the reader follows the docs. So pin the parts a reader acts on: the command
 * for each tier, and the claim that decides which tier they pick.
 *
 * This checks correspondence, not wording. Nothing here objects to the docs
 * explaining a tier differently from the CLI; it objects to them naming a
 * command that no longer exists.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const { TIERS, DEFAULT_TIER } = require(
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
 * `tiers.js` writes Tier 0's command with a `<skill>` metavariable, which the
 * docs replace with a real skill name. Compare the invariant part.
 *
 * @param {import('../../cli/lib/tiers').Tier} tier
 */
const commandStem = (tier) => tier.command.replace(/ <skill>$/, '');

test('getting-started names every tier by number and command', () => {
  for (const tier of TIERS) {
    assert.ok(
      GETTING_STARTED.includes(commandStem(tier)),
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
      README.includes(commandStem(tier)),
      `README.md does not mention \`${commandStem(tier)}\` (tier ${tier.n})`,
    );
  }
});

test('both docs state which tier is the default', () => {
  // The single most load-bearing sentence in the onboarding: it is what stops a
  // reader concluding they need a GitHub App to begin.
  for (const [name, body] of [
    ['README.md', README],
    ['docs/getting-started.md', GETTING_STARTED],
  ]) {
    assert.match(
      body,
      new RegExp(`Tier ${DEFAULT_TIER} is the default`),
      `${name} does not say Tier ${DEFAULT_TIER} is the default`,
    );
  }
});

test('both docs confine the GitHub App to tier 3', () => {
  // #786's one fixed constraint. The old getting-started led with "a GitHub App
  // is installed" as a hard prerequisite, which is exactly the barrier the
  // tiering exists to move — so assert it has not crept back.
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
  // Structural, not stylistic: whatever appears before the first tier heading
  // is what a first-time reader meets. A secret or an App named there
  // re-erects the barrier regardless of what the tier table later says.
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
  // The acceptance criterion is "no account/key/secret". If a step needs a
  // token it is not Tier 0, so the doc must not introduce one in that section.
  const section =
    GETTING_STARTED.split('## Tier 0')[1].split('<a id="tier-1">')[0];
  assert.ok(!/gh secret set/.test(section), 'Tier 0 section sets a secret');
  assert.ok(
    !/GitHub App/.test(section),
    'Tier 0 section mentions a GitHub App',
  );
});
