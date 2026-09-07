'use strict';

/**
 * Terminal output helpers.
 *
 * Colour is opt-out via the `NO_COLOR` convention and is suppressed when stdout
 * is not a TTY, so piping the output into a file or a CI log yields plain text
 * rather than escape sequences.
 */

const useColour = process.stdout.isTTY === true && !process.env.NO_COLOR;

/** @param {string} code @param {string} s @returns {string} */
const wrap = (code, s) => (useColour ? `\u001b[${code}m${s}\u001b[0m` : s);

/** @param {string} s */
const bold = (s) => wrap('1', s);
/** @param {string} s */
const dim = (s) => wrap('2', s);
/** @param {string} s */
const green = (s) => wrap('32', s);
/** @param {string} s */
const yellow = (s) => wrap('33', s);
/** @param {string} s */
const red = (s) => wrap('31', s);

/** @param {string} msg */
const info = (msg) => console.log(msg);
/** @param {string} msg */
const step = (msg) => console.log(`  ${msg}`);
/** @param {string} msg */
const ok = (msg) => console.log(`${green('✓')} ${msg}`);
/** @param {string} msg */
const warn = (msg) => console.error(`${yellow('!')} ${msg}`);
/** @param {string} msg */
const fail = (msg) => console.error(`${red('✗')} ${msg}`);

/**
 * Ask a yes/no question on stdin.
 *
 * Returns `defaultYes` without prompting when stdin is not a TTY, so the CLI
 * stays usable in a script or a CI step. A caller that must not proceed
 * unattended checks `--yes` itself rather than relying on this.
 *
 * @param {string} question
 * @param {boolean} [defaultYes]
 * @returns {Promise<boolean>}
 */
async function confirm(question, defaultYes = true) {
  const suffix = defaultYes ? '[Y/n]' : '[y/N]';
  const answer = (await ask(`${question} ${suffix}`)).toLowerCase();
  if (answer === '') return defaultYes;
  return answer === 'y' || answer === 'yes';
}

/**
 * Ask for a line of free text on stdin.
 *
 * Returns `''` without prompting when stdin is not a TTY, for the same reason
 * `confirm` returns its default: a non-interactive caller must never block on a
 * question nobody can answer.
 *
 * @param {string} question
 * @returns {Promise<string>}
 */
async function ask(question) {
  if (!process.stdin.isTTY) return '';
  const readline = require('node:readline/promises');
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  try {
    return (await rl.question(`${question} `)).trim();
  } finally {
    rl.close();
  }
}

/**
 * Render one detected harness's signal list.
 *
 * Shared so the `detect` command and `init`'s confirmation prompt cannot show
 * different facts: a signal added to `detectHarnesses` appears in both views or
 * neither. Callers own their own leading mark and padding.
 *
 * @param {{inRepo: boolean, onMachine: boolean, cliInstalled: boolean}} harness
 * @returns {string}
 */
function harnessSignals(harness) {
  const signals = [
    harness.inRepo ? 'in repo' : null,
    harness.onMachine ? 'config on machine' : null,
    harness.cliInstalled ? 'CLI installed' : null,
  ].filter(Boolean);
  return signals.length > 0 ? signals.join(', ') : dim('no signal');
}

module.exports = {
  ask,
  harnessSignals,
  bold,
  dim,
  green,
  yellow,
  red,
  info,
  step,
  ok,
  warn,
  fail,
  confirm,
};
