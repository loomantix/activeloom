'use strict';

/**
 * `activeloom review-config` — the per-user review profile, from the CLI.
 *
 * The profile's only validator and writer is the Python helper that ships with
 * the `review-setup` skill; review launchers resolve their model and effort
 * through that same file. This command runs the helper from the fetched
 * upstream instead of reimplementing it, so the CLI and the launchers cannot
 * disagree about what a valid profile is.
 */

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const HELPER = path.join(
  '.claude',
  'skills',
  'review-setup',
  'scripts',
  'review-profile.py',
);

/**
 * Run the review-profile helper with the user's arguments.
 *
 * @param {object} args
 * @param {string} args.upstreamDir
 * @param {string} args.python
 * @param {string[]} args.helperArgs  Passed through verbatim.
 * @param {typeof spawnSync} [args.spawn]
 * @returns {number} The helper's exit status.
 */
function reviewConfig({ upstreamDir, python, helperArgs, spawn = spawnSync }) {
  const helper = path.join(upstreamDir, HELPER);
  if (!fs.existsSync(helper)) {
    throw new Error(
      `the upstream content does not ship ${HELPER}; install from a newer --ref`,
    );
  }
  const run = spawn(
    python,
    ['-I', helper, ...(helperArgs.length > 0 ? helperArgs : ['--help'])],
    { stdio: 'inherit' },
  );
  if (run.error) {
    throw new Error(`\`${python}\` could not be run: ${run.error.message}`);
  }
  return typeof run.status === 'number' ? run.status : 1;
}

module.exports = { reviewConfig, HELPER };
