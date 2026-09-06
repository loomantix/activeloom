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
  if (!process.stdin.isTTY) return defaultYes;
  const readline = require('node:readline/promises');
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  try {
    const suffix = defaultYes ? '[Y/n]' : '[y/N]';
    const answer = (await rl.question(`${question} ${suffix} `))
      .trim()
      .toLowerCase();
    if (answer === '') return defaultYes;
    return answer === 'y' || answer === 'yes';
  } finally {
    rl.close();
  }
}

module.exports = {
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
