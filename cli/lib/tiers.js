'use strict';

/**
 * The onboarding ladder, ordered by credential cost.
 *
 * Each tier adds exactly one requirement to the previous tier.
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
 * Recommended tier highlighted in documentation. Bare `init` defaults to Tier 1.
 */
const RECOMMENDED_TIER = 2;

/**
 * Resolve the tier a set of parsed flags selects.
 *
 * @param {{sync?: boolean, app?: boolean}} flags
 * @returns {Tier}
 */
function resolveTier(flags) {
  // `--app` implies `--sync`: signing requires a scheduled sync workflow.
  if (flags.app) return TIERS[3];
  if (flags.sync) return TIERS[2];
  return TIERS[1];
}

module.exports = { TIERS, RECOMMENDED_TIER, resolveTier };
