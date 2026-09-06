#!/usr/bin/env node
'use strict';

/**
 * `npx activeloom` — the CLI door onto the toolkit.
 *
 * Four commands, mapping onto the four onboarding tiers plus two read-only
 * helpers. Argument parsing is hand-rolled and dependency-free on purpose: this
 * package is fetched and executed by `npx` before a user has decided to trust
 * it, so its install footprint is part of its argument. Zero dependencies means
 * zero transitive supply chain for that first run.
 */

const { detect } = require('../lib/detect');
const {
  resolveUpstream,
  DEFAULT_REF,
  DEFAULT_UPSTREAM,
} = require('../lib/upstream');
const { TIERS, resolveTier } = require('../lib/tiers');
const ui = require('../lib/ui');

const USAGE = `
${ui.bold('activeloom')} — one agent toolkit, four ways in.

${ui.bold('Usage')}
  npx activeloom add <skill>...        Tier 0  install skills for yourself
  npx activeloom init                  Tier 1  write the trees into this repo
  npx activeloom init --sync           Tier 2  ...and keep them updated (default)
  npx activeloom init --sync --app     Tier 3  ...with GitHub-signed commits

  npx activeloom add                   list the skills available
  npx activeloom detect                print what this repo looks like
  npx activeloom tiers                 explain the four tiers

${ui.bold('Options')}
  --harness <id>     claude | codex | gemini. Repeatable. Default: detected.
  --ref <ref>        upstream ref to install from (default: ${DEFAULT_REF})
  --base-branch <b>  branch sync PRs land on (default: origin's HEAD)
  --python <path>    interpreter for the sync engine (default: python3)
  --upstream-dir <d> install from a local checkout instead of downloading
  --consumer-dir <d> repository to write into (default: current directory)
  --dry-run          report what would happen, write nothing
  --force            replace files that already exist
  -h, --help         this message

${ui.dim(`Content is fetched from ${DEFAULT_UPSTREAM} at the pinned ref — the same gate`)}
${ui.dim('the CI sync workflow reads, so both doors deliver identical prompts.')}
`.trimStart();

/**
 * Parse argv.
 *
 * Unknown flags are an error rather than a silent no-op: a mistyped `--forse`
 * that parsed as "no flags" would report success while having skipped the
 * behaviour the user asked for.
 *
 * @param {string[]} argv
 */
function parseArgs(argv) {
  const opts = {
    command: null,
    /** @type {string[]} */ positionals: [],
    /** @type {string[]} */ harnesses: [],
    ref: undefined,
    baseBranch: undefined,
    python: 'python3',
    upstreamDir: undefined,
    consumerDir: undefined,
    dryRun: false,
    force: false,
    sync: false,
    app: false,
    help: false,
  };

  /** @param {string} flag */
  const needsValue = (flag, value) => {
    if (value === undefined) throw new Error(`${flag} needs a value`);
    return value;
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    switch (arg) {
      case '-h':
      case '--help':
        opts.help = true;
        break;
      case '--sync':
        opts.sync = true;
        break;
      case '--app':
        opts.app = true;
        break;
      case '--dry-run':
        opts.dryRun = true;
        break;
      case '--force':
        opts.force = true;
        break;
      case '--harness':
        opts.harnesses.push(needsValue(arg, argv[(i += 1)]));
        break;
      case '--ref':
        opts.ref = needsValue(arg, argv[(i += 1)]);
        break;
      case '--base-branch':
        opts.baseBranch = needsValue(arg, argv[(i += 1)]);
        break;
      case '--python':
        opts.python = needsValue(arg, argv[(i += 1)]);
        break;
      case '--upstream-dir':
        opts.upstreamDir = needsValue(arg, argv[(i += 1)]);
        break;
      case '--consumer-dir':
        opts.consumerDir = needsValue(arg, argv[(i += 1)]);
        break;
      default:
        if (arg.startsWith('-')) throw new Error(`unknown option ${arg}`);
        if (opts.command === null) opts.command = arg;
        else opts.positionals.push(arg);
    }
  }
  return opts;
}

/** Print the tier ladder. Read-only; touches neither disk nor network. */
function printTiers() {
  ui.info(
    ui.bold('The four tiers') +
      ui.dim('  — each one is the previous plus one thing.\n'),
  );
  for (const tier of TIERS) {
    ui.info(`${ui.bold(`Tier ${tier.n} — ${tier.name}`)}`);
    ui.info(`  ${tier.command}`);
    ui.info(`  ${ui.dim('needs')}    ${tier.credential}`);
    ui.info(`  ${ui.dim('writes')}   ${tier.writes}`);
    ui.info(`  ${ui.dim('for')}      ${tier.who}`);
    ui.info('');
  }
  ui.info(
    ui.dim(
      'Tier 2 is the default. A GitHub App is only ever needed at Tier 3.',
    ),
  );
}

/**
 * Print detection results.
 *
 * Its own command because it is the thing to run when `init` chose something
 * surprising — it shows the same facts `init` decided on, with nothing written.
 *
 * @param {ReturnType<import('../lib/detect').detect>} facts
 */
function printDetect(facts) {
  ui.info(ui.bold(`Detected in ${facts.repoDir}\n`));
  ui.info(ui.bold('  harnesses'));
  for (const h of facts.harnesses) {
    const signals = [
      h.inRepo ? 'in repo' : null,
      h.onMachine ? 'config on machine' : null,
      h.cliInstalled ? 'CLI installed' : null,
    ].filter(Boolean);
    ui.info(
      `    ${h.id.padEnd(8)} ${signals.length > 0 ? signals.join(', ') : ui.dim('no signal')}`,
    );
  }
  ui.info('');
  ui.info(ui.bold('  repository'));
  ui.info(`    git repo       ${facts.git.inRepo}`);
  ui.info(`    slug           ${facts.git.slug ?? ui.dim('none')}`);
  ui.info(`    default branch ${facts.git.defaultBranch ?? ui.dim('unknown')}`);
  ui.info(`    gh authed      ${facts.git.ghAuthenticated}`);
  ui.info(`    workflows dir  ${facts.hasWorkflows}`);
  ui.info(`    config present ${facts.hasConfig}`);
  ui.info('');
  ui.info(ui.bold('  stack'));
  ui.info(
    `    ecosystems     ${facts.ecosystems.join(', ') || ui.dim('none')}`,
  );
  ui.info(`    package mgr    ${facts.packageManager ?? ui.dim('none')}`);
  ui.info(`    test script    ${facts.scripts.test ?? ui.dim('none')}`);
  ui.info(`    lint script    ${facts.scripts.lint ?? ui.dim('none')}`);
  ui.info(`    format script  ${facts.scripts.format ?? ui.dim('none')}`);
  ui.info('');
  ui.info(
    ui.dim(
      'Nothing was written. Everything above is a file test, a PATH lookup, or git.',
    ),
  );
}

async function main(argv) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (err) {
    ui.fail(err.message);
    ui.info('');
    ui.info(USAGE);
    return 2;
  }

  if (opts.help || opts.command === null || opts.command === 'help') {
    ui.info(USAGE);
    return opts.command === null && !opts.help ? 2 : 0;
  }

  if (opts.command === 'tiers') {
    printTiers();
    return 0;
  }

  const facts = detect({ repoDir: opts.consumerDir });

  if (opts.command === 'detect') {
    printDetect(facts);
    return 0;
  }

  if (opts.command !== 'add' && opts.command !== 'init') {
    ui.fail(`unknown command "${opts.command}"`);
    ui.info('');
    ui.info(USAGE);
    return 2;
  }

  // `--app` without `--sync` is accepted and resolves to Tier 3, but say so
  // rather than silently reinterpreting the flags the user typed.
  if (opts.app && !opts.sync) {
    ui.warn('--app implies --sync; installing Tier 3.');
    opts.sync = true;
  }

  let upstream;
  try {
    upstream = await resolveUpstream({
      ref: opts.ref,
      upstreamDir: opts.upstreamDir,
    });
  } catch (err) {
    ui.fail(err.message);
    return 1;
  }

  try {
    if (opts.command === 'add') {
      const { add } = require('../lib/add');
      return await add({
        skills: opts.positionals,
        upstreamDir: upstream.dir,
        facts,
        harnesses: opts.harnesses,
        dryRun: opts.dryRun,
        force: opts.force,
      });
    }

    const { init } = require('../lib/init');
    return await init({
      upstreamDir: upstream.dir,
      ref: upstream.ref,
      facts,
      harnesses: opts.harnesses,
      sync: opts.sync,
      app: opts.app,
      dryRun: opts.dryRun,
      force: opts.force,
      python: opts.python,
      baseBranch: opts.baseBranch,
      upstreamRepo: DEFAULT_UPSTREAM,
    });
  } catch (err) {
    ui.fail(err.message);
    return 1;
  } finally {
    upstream.cleanup();
  }
}

if (require.main === module) {
  main(process.argv.slice(2)).then(
    (code) => {
      process.exitCode = code;
    },
    (err) => {
      ui.fail(err && err.stack ? err.stack : String(err));
      process.exitCode = 1;
    },
  );
}

module.exports = { main, parseArgs, USAGE, resolveTier };
