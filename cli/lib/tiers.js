'use strict';

/**
 * The onboarding ladder, defined once.
 *
 * Every tier is strictly the previous tier plus exactly one thing, and the
 * axis is credential cost — not feature richness. That ordering is the whole
 * point: a reader can stop at the first tier whose price they are willing to
 * pay, and nothing above it is a prerequisite for anything below it.
 *
 * This module is the single definition. `docs/getting-started.md`, `README.md`,
 * and the CLI's own `tiers` output all describe the same ladder, and
 * `tests/cli/tiers-docs.test.js` fails when the docs and this file disagree —
 * so a tier renamed here cannot quietly leave stale prose behind.
 */

/**
 * @typedef {object} Tier
 * @property {number} n            Tier number, 0-3.
 * @property {string} name         Short label used in headings and CLI output.
 * @property {string} command      The command that lands a repo on this tier.
 * @property {string} credential   What the user must possess. `none` is literal.
 * @property {string} writes       What appears on disk, in one line.
 * @property {string} who          Who this tier is for.
 */

/** @type {readonly Tier[]} */
const TIERS = Object.freeze([
  Object.freeze({
    n: 0,
    name: 'Try it',
    command: 'npx activeloom add <skill>',
    credential: 'none',
    writes: 'skills into your own agent config directory; nothing in the repo',
    who: 'One person evaluating the toolkit. No repo required.',
  }),
  Object.freeze({
    n: 1,
    name: 'Commit it',
    command: 'npx activeloom init',
    credential: 'none',
    writes:
      'harness roots + .activeloom-config.yml into the repo, for you to commit',
    who: 'A team that wants shared skills without any automation.',
  }),
  Object.freeze({
    n: 2,
    name: 'Automate it',
    command: 'npx activeloom init --sync',
    credential: "none — the workflow's built-in GITHUB_TOKEN",
    writes:
      'tier 1, plus a sync workflow that opens an ordinary PR on a schedule',
    who: 'Most repositories. This is the recommended tier.',
  }),
  Object.freeze({
    n: 3,
    name: 'Sign it',
    command: 'npx activeloom init --sync --app',
    credential: 'a GitHub App id + private key, stored as repository secrets',
    writes:
      'tier 2, but sync commits are GitHub-signed and private upstreams work',
    who: 'Repositories under an audit regime that requires signed commits.',
  }),
]);

/**
 * The tier the docs point a reader at.
 *
 * Deliberately not named `DEFAULT_TIER`: bare `npx activeloom init` resolves to
 * Tier 1, and a constant claiming otherwise made the docs and `resolveTier`
 * disagree with nothing to catch it. `tests/cli/tiers-docs.test.js` reads this
 * value, asserts the prose against it, and separately asserts that bare `init`
 * does *not* resolve to it — so the constant, the docs, and `resolveTier` are
 * pinned to one meaning.
 */
const RECOMMENDED_TIER = 2;

/**
 * Resolve the tier a set of parsed flags selects.
 *
 * Kept separate from the flag parser so the mapping is testable on its own and
 * stated in exactly one place — the CLI prints the resolved tier back to the
 * user, and a drift between "what we did" and "what we said we did" is the
 * failure mode worth ruling out by construction.
 *
 * @param {{sync?: boolean, app?: boolean}} flags
 * @returns {Tier}
 */
function resolveTier(flags) {
  // `--app` implies `--sync`: an App identity with nothing to sign is not a
  // coherent request, and silently accepting it would produce a tier-1 tree
  // while the user believed they had asked for signed automation.
  if (flags.app) return TIERS[3];
  if (flags.sync) return TIERS[2];
  return TIERS[1];
}

module.exports = { TIERS, RECOMMENDED_TIER, resolveTier };
