'use strict';

/**
 * Tiers 1-3 — `npx activeloom init [--sync] [--app]`.
 *
 * The acceptance criterion for this command is an equivalence: `init` on a
 * triple-harness repo must write the same trees the CI sync would deliver.
 * Rather than reimplement the manifest walk in JavaScript and then test that
 * the two agree, `init` **invokes the sync engine itself** — the same
 * `scripts/sync-engine.py`, from the same tag-pinned upstream tree, with the
 * same arguments the consumer workflow uses. Equivalence is then definitional,
 * and `tests/cli/test_init_equivalence.py` demonstrates it rather than
 * propping it up.
 *
 * The cost is a Python dependency for tiers 1 and up. That is a deliberate
 * place to spend it: Tier 0 stays pure Node precisely so the "under two
 * minutes, no account/key/secret" path never pays it, and by Tier 1 the user is
 * committing config into a shared repository — a context where `python3` is a
 * far smaller ask than a second implementation that can drift.
 */

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const ui = require('./ui');
const { resolveTier } = require('./tiers');
const { HARNESSES } = require('./detect');

/**
 * Placeholder marker for a prose value the CLI will not invent.
 *
 * Detection can establish that a repo uses pnpm and Vitest; it cannot establish
 * what the project *is* or what reviewers should look hardest at. Emitting a
 * marked TODO is honest and greppable, and it is what the `onboard` skill
 * looks for when an agent fills these in. A plausible-sounding guess here would
 * be worse than a blank, because nobody would think to check it.
 */
const TODO = 'TODO(activeloom): ';

/**
 * Assert the interpreter can run the sync engine.
 *
 * Checked up front and reported precisely, because the failure otherwise
 * surfaces as a Python traceback from a subprocess the user did not know was
 * involved.
 *
 * @param {string} python
 * @returns {{ok: true} | {ok: false, reason: string, remedy: string}}
 */
function checkPython(python) {
  const version = spawnSync(python, ['--version'], { encoding: 'utf8' });
  if (version.error || version.status !== 0) {
    return {
      ok: false,
      reason: `\`${python}\` is not runnable`,
      remedy:
        'Install Python 3.9+ and re-run, or pass --python <path>. ' +
        'Only `add` (Tier 0) works without Python.',
    };
  }
  const yaml = spawnSync(python, ['-c', 'import yaml'], { encoding: 'utf8' });
  if (yaml.status !== 0) {
    return {
      ok: false,
      reason: `\`${python}\` cannot import PyYAML`,
      remedy: `Install it with:\n      ${python} -m pip install pyyaml`,
    };
  }
  return { ok: true };
}

/**
 * Refuse to sync an upstream into itself.
 *
 * The sync engine takes a source and a destination and does what it is told; if
 * they are the same tree it will happily "sync" the repo onto itself, and
 * because the manifest carries `delete: true` retirement targets, that run
 * *removes files from the upstream's own working tree*. It looks like a
 * successful sync while deleting source.
 *
 * This is easy to trigger by accident — `npx activeloom init --upstream-dir .`
 * from inside a checkout reads as an obvious thing to type — so the guard is
 * structural rather than advisory, and there is no flag to override it. A fork
 * maintainer never needs to install their own upstream into itself; they run
 * the engine directly.
 *
 * @param {string} repoDir
 * @param {string} upstreamDir
 * @returns {string | null} refusal message, or null when the pairing is safe
 */
function refuseSelfSync(repoDir, upstreamDir) {
  const consumer = path.resolve(repoDir);
  const upstream = path.resolve(upstreamDir);

  if (consumer === upstream) {
    return (
      'refusing to sync a tree into itself.\n' +
      `  Both the upstream and the consumer resolve to ${consumer}.\n` +
      '  Run `init` from the repository you want to receive the files, not from the activeloom checkout.'
    );
  }

  // The same hazard one step removed: a *different* activeloom checkout is
  // still an upstream, and syncing one into another would apply the manifest's
  // retirement deletions to a source tree.
  const looksLikeUpstream =
    fs.existsSync(path.join(consumer, 'scripts', 'sync-targets.yml')) &&
    fs.existsSync(path.join(consumer, 'prompts', 'profiles'));
  if (looksLikeUpstream) {
    return (
      `${consumer} is itself an activeloom upstream (it has scripts/sync-targets.yml and prompts/profiles/).\n` +
      "  Syncing into it would apply the manifest's retirement deletions to the source tree.\n" +
      '  Run `init` from a consumer repository instead.'
    );
  }

  return null;
}

/**
 * Which harnesses should this repo receive?
 *
 * Repo evidence outranks machine evidence, and deliberately so: the config is
 * committed and governs every teammate's sync, so a harness already checked in
 * is a decision the team has made, whereas a CLI on *this* laptop says only
 * what one developer happens to run. Machine evidence is the fallback for a
 * repo that has nothing yet.
 *
 * @param {ReturnType<import('./detect').detect>} facts
 * @param {string[]} requested
 * @returns {{ids: string[], reason: string}}
 */
function chooseHarnesses(facts, requested) {
  if (requested.length > 0) {
    const known = new Set(HARNESSES.map((h) => h.id));
    const unknown = requested.filter((id) => !known.has(id));
    if (unknown.length > 0) {
      throw new Error(
        `unknown harness ${unknown.join(', ')}. Known: ${[...known].join(', ')}.`,
      );
    }
    return { ids: requested, reason: 'requested with --harness' };
  }

  const inRepo = facts.harnesses.filter((h) => h.inRepo).map((h) => h.id);
  if (inRepo.length > 0)
    return { ids: inRepo, reason: 'already checked into this repo' };

  const onMachine = facts.harnesses
    .filter((h) => h.onMachine || h.cliInstalled)
    .map((h) => h.id);
  if (onMachine.length > 0)
    return { ids: onMachine, reason: 'detected on this machine' };

  return {
    ids: ['claude'],
    reason: 'no evidence either way — defaulting to Claude Code',
  };
}

/**
 * Quote a scalar for YAML output.
 *
 * Single-quoted with internal quotes doubled: the one YAML scalar style with no
 * escape processing at all, so a value containing a backtick, colon, or hash
 * cannot change the meaning of the document. Detected values are mostly tame,
 * but `CANONICAL_DOCS` carries backticks by design.
 *
 * @param {string} value
 * @returns {string}
 */
function yamlScalar(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

/**
 * Render a `.activeloom-config.yml` from detected facts.
 *
 * Note what is absent: `REVIEW_TELEMETRY_ENV` is a reserved key the engine
 * computes from the `telemetry:` block, and a consumer that declares it is
 * rejected outright. Emitting it here would produce a config that fails on its
 * first sync.
 *
 * @param {object} args
 * @param {string[]} args.harnesses
 * @param {ReturnType<import('./detect').detect>} args.facts
 * @returns {string}
 */
function renderConfig({ harnesses, facts }) {
  const name = facts.git.slug
    ? facts.git.slug.split('/')[1]
    : path.basename(facts.repoDir);

  const stackRows = [];
  if (facts.ecosystems.length > 0) {
    stackRows.push(`| Language | ${facts.ecosystems.join(', ')} |`);
  }
  if (facts.packageManager) {
    stackRows.push(`| Packages | ${facts.packageManager} |`);
  }
  if (facts.scripts.test) {
    stackRows.push(
      `| Tests    | \`${facts.packageManager ?? 'npm'} run ${facts.scripts.test}\` |`,
    );
  }
  const stackTable =
    stackRows.length > 0
      ? ['| Layer    | Tech |', '| -------- | ---- |', ...stackRows].join('\n')
      : `${TODO}describe the stack as a Markdown table.`;

  const codeRules = [];
  if (facts.scripts.lint) {
    codeRules.push(
      `- Lint with \`${facts.packageManager ?? 'npm'} run ${facts.scripts.lint}\`.`,
    );
  }
  if (facts.scripts.format) {
    codeRules.push(
      `- Format with \`${facts.packageManager ?? 'npm'} run ${facts.scripts.format}\`.`,
    );
  }
  if (codeRules.length === 0) {
    codeRules.push(`- ${TODO}list the conventions a reviewer should enforce.`);
  }

  return `# Consumer configuration for activeloom.
#
# Written by \`npx activeloom init\`. The detected values below are facts read
# off this repository; the \`${TODO.trim()}\` markers are the ones that need a
# human — run the \`onboard\` skill in your agent to draft them, then confirm.
#
# Full reference: https://github.com/loomantix/activeloom/blob/main/docs/getting-started.md

# Which harnesses this repo runs. You receive these target sets plus the
# shared one.
harnesses: [${harnesses.join(', ')}]

substitutions:
  PROJECT_NAME: ${yamlScalar(name)}

  PROJECT_OVERVIEW: |
    ${TODO}one short paragraph — what this project does and who uses it.

  CANONICAL_DOCS: ${yamlScalar(`${TODO}e.g. \`docs/architecture.md\``)}

  STACK_TABLE: |
${stackTable
  .split('\n')
  .map((line) => `    ${line}`)
  .join('\n')}

  CODE_RULES: |
${codeRules.map((line) => `    ${line}`).join('\n')}

  DOMAIN_RULES: ''

  REVIEW_FOCUS: |
    1. **Correctness** — logic errors, edge cases, off-by-one.
    2. **Security** — secret handling, auth bypass, injection at edges.
    3. **Convention adherence.**
    4. **Testing gaps.**
    5. **Maintainability.**

  WHAT_NOT_TO_SUGGEST_EXTRA: ''

# Required before the sync may write a sensitive path. The shared target set
# ships \`.github/workflows/dco.yml\`, which every consumer receives, so this
# entry is what lets your first sync run. A refusal names any others it needs,
# in a block you can paste as-is.
allow_sensitive_writes:
  - .github/workflows/dco.yml

# Opt out of specific upstream files by source or destination path.
skip_targets: []
`;
}

/**
 * Write the tier-appropriate sync workflow into the consumer.
 *
 * The two templates are separate files rather than one file with a switch:
 * they differ in identity, permissions, and commit mechanism, which is a
 * difference in kind. `tests/cli/workflow-templates.test.js` pins the parts
 * that must NOT differ — the engine invocation and the ref gate.
 *
 * @param {object} args
 * @param {import('./tiers').Tier} args.tier
 * @param {string} args.upstreamDir
 * @param {string} args.repoDir
 * @param {string} args.upstreamRepo
 * @param {string} args.baseBranch
 * @param {boolean} args.dryRun
 * @param {boolean} args.force
 * @returns {{written: boolean, dest: string, note?: string}}
 */
function writeWorkflow({
  tier,
  upstreamDir,
  repoDir,
  upstreamRepo,
  baseBranch,
  dryRun,
  force,
}) {
  const templateName =
    tier.n === 3
      ? 'sync-from-upstream.yml.template'
      : 'sync-from-upstream-token.yml.template';
  const src = path.join(upstreamDir, '.github', 'workflows', templateName);
  if (!fs.existsSync(src)) {
    throw new Error(
      `upstream tree has no ${templateName}. ` +
        `A ref older than the tiered-onboarding change will not carry it — try --ref main.`,
    );
  }

  const dest = path.join(
    repoDir,
    '.github',
    'workflows',
    'sync-from-upstream.yml',
  );
  if (fs.existsSync(dest) && !force) {
    return {
      written: false,
      dest,
      note: 'already exists — left alone (re-run with --force to replace)',
    };
  }

  let body = fs.readFileSync(src, 'utf8');
  // Anchored on the exact placeholder text the templates ship. A silent
  // no-op here would install a workflow that fails on its first run with
  // "UPSTREAM_REPO: <owner>/<repo>", so assert both substitutions landed.
  const before = body;
  // Consume the trailing guidance comment along with the placeholder. Leaving
  // it behind produces `UPSTREAM_REPO: loomantix/activeloom # e.g.
  // loomantix/activeloom`, which reads as though the value were still unset.
  body = body.replace(
    /UPSTREAM_REPO: <owner>\/<repo>[^\n]*/,
    `UPSTREAM_REPO: ${upstreamRepo}`,
  );
  body = body.replace(
    /PR_BASE_BRANCH: ''[^\n]*/,
    `PR_BASE_BRANCH: '${baseBranch}'`,
  );
  if (body === before) {
    throw new Error(
      `${templateName} did not contain the expected UPSTREAM_REPO / PR_BASE_BRANCH placeholders.`,
    );
  }

  if (!dryRun) {
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, body);
  }
  return { written: true, dest };
}

/**
 * @param {object} args
 * @param {string} args.upstreamDir
 * @param {string} args.ref
 * @param {ReturnType<import('./detect').detect>} args.facts
 * @param {string[]} args.harnesses
 * @param {boolean} args.sync
 * @param {boolean} args.app
 * @param {boolean} args.dryRun
 * @param {boolean} args.force
 * @param {string} args.python
 * @param {string} [args.baseBranch]
 * @param {string} args.upstreamRepo
 * @returns {Promise<number>} process exit code
 */
async function init(args) {
  const { upstreamDir, facts, dryRun, force, python, upstreamRepo } = args;
  const tier = resolveTier({ sync: args.sync, app: args.app });

  ui.info(
    `${ui.bold(`Tier ${tier.n} — ${tier.name}`)}  ${ui.dim(`(${tier.credential})`)}`,
  );
  ui.info('');

  if (!facts.git.inRepo) {
    ui.fail(`${facts.repoDir} is not a git repository.`);
    ui.info(
      '  `init` writes files a team commits. To try the skills on this machine only:',
    );
    ui.info('      npx activeloom add critique');
    return 1;
  }

  // Before anything is written, and before any other check: a self-sync
  // deletes source files, so it must not be reachable by a run that is
  // otherwise well-formed.
  const selfSync = refuseSelfSync(facts.repoDir, upstreamDir);
  if (selfSync) {
    ui.fail(selfSync);
    return 1;
  }

  // Tiers 2 and 3 install a GitHub Actions workflow, so they need a GitHub
  // remote to install it against. Checked before anything is written, so a
  // repo that cannot reach the requested tier is told so instead of being left
  // half-configured.
  if (tier.n >= 2 && !facts.git.slug) {
    ui.fail(
      'no GitHub `origin` remote — tiers 2 and 3 install a GitHub Actions workflow.',
    );
    ui.info(
      '  Add a GitHub remote, or drop to Tier 1 (files only, no automation):',
    );
    ui.info('      npx activeloom init');
    return 1;
  }

  const python3 = checkPython(python);
  if (!python3.ok) {
    ui.fail(`${python3.reason} — the sync engine needs it.`);
    ui.info(`  ${python3.remedy}`);
    return 1;
  }

  const chosen = chooseHarnesses(facts, args.harnesses);
  ui.step(
    `harnesses: ${ui.bold(chosen.ids.join(', '))} ${ui.dim(`(${chosen.reason})`)}`,
  );

  // --- config ---------------------------------------------------------------
  const configPath = path.join(facts.repoDir, '.activeloom-config.yml');
  const configExists = fs.existsSync(configPath);
  if (configExists && !force) {
    ui.step(
      `config:     ${ui.dim('.activeloom-config.yml exists — keeping yours')}`,
    );
  } else {
    const body = renderConfig({ harnesses: chosen.ids, facts });
    if (!dryRun) fs.writeFileSync(configPath, body);
    ui.step(
      `config:     ${dryRun ? 'would write' : 'wrote'} .activeloom-config.yml`,
    );
  }

  // --- the trees ------------------------------------------------------------
  // The same invocation the consumer workflow makes. Not a reimplementation of
  // it, and not a subset — the identical entry point, so what lands here is
  // what CI would land by construction.
  const engine = path.join(upstreamDir, 'scripts', 'sync-engine.py');
  const engineArgs = [
    engine,
    '--upstream-repo',
    upstreamDir,
    '--consumer-dir',
    facts.repoDir,
  ];
  if (dryRun) engineArgs.push('--dry-run');

  ui.step(`trees:      running the sync engine from ${ui.bold(args.ref)}`);
  const run = spawnSync(python, engineArgs, {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (run.status !== 0) {
    ui.fail('the sync engine refused to write.');
    if (run.stdout) process.stdout.write(run.stdout);
    if (run.stderr) process.stderr.write(run.stderr);
    // The engine's own refusals are precise and paste-ready (it prints the
    // exact `allow_sensitive_writes` block a config is missing), so surface
    // them verbatim rather than summarising them into something vaguer.
    return run.status ?? 1;
  }

  // --- the workflow ---------------------------------------------------------
  if (tier.n >= 2) {
    const baseBranch = args.baseBranch ?? facts.git.defaultBranch ?? 'main';
    const result = writeWorkflow({
      tier,
      upstreamDir,
      repoDir: facts.repoDir,
      upstreamRepo,
      baseBranch,
      dryRun,
      force,
    });
    if (result.written) {
      ui.step(
        `workflow:   ${dryRun ? 'would write' : 'wrote'} .github/workflows/sync-from-upstream.yml ` +
          ui.dim(`(base: ${baseBranch})`),
      );
      if (!args.baseBranch && !facts.git.defaultBranch) {
        ui.warn(
          `could not read origin's default branch — PR_BASE_BRANCH guessed as "${baseBranch}". ` +
            `Set it explicitly with --base-branch if your repo lands sync PRs elsewhere.`,
        );
      }
    } else {
      ui.step(`workflow:   ${ui.dim(result.note)}`);
    }
  }

  ui.info('');
  if (dryRun) {
    ui.ok('Dry run complete. Nothing written.');
    return 0;
  }
  ui.ok(`Tier ${tier.n} written.`);
  printNextSteps(tier, chosen.ids, facts, configExists);
  return 0;
}

/**
 * What the user has to do that the CLI cannot.
 *
 * Deliberately explicit about the unfinished parts: the config's TODO markers
 * and, at Tier 3, the two secrets. A command that printed an unqualified
 * success while leaving `TODO(activeloom):` in a file that renders into a
 * reviewer prompt would be lying about its own output.
 *
 * @param {import('./tiers').Tier} tier
 * @param {string[]} harnesses
 * @param {ReturnType<import('./detect').detect>} facts
 * @param {boolean} configExisted
 */
function printNextSteps(tier, harnesses, facts, configExisted) {
  ui.info('');
  ui.info(ui.bold('Next:'));

  if (!configExisted) {
    ui.step(
      `1. Fill the ${ui.bold(TODO.trim())} markers in .activeloom-config.yml — ` +
        `run the ${ui.bold('onboard')} skill in your agent to draft them, then confirm.`,
    );
    ui.step(
      '2. Re-run `npx activeloom init` so the filled values reach the rendered files.',
    );
  }

  if (tier.n >= 2) {
    ui.step('Commit everything, including the workflow.');
  } else {
    ui.step('Commit the harness roots and the config so your team gets them.');
  }

  if (tier.n === 2) {
    ui.step(
      'Enable Settings > Actions > General > ' +
        '"Allow GitHub Actions to create and approve pull requests" — ' +
        'without it the sync cannot open its PR.',
    );
  }

  if (tier.n === 3) {
    ui.step('Set the two App secrets on the repo:');
    ui.info('       gh secret set SYNC_APP_ID --body "<app-id>"');
    ui.info(
      '       gh secret set SYNC_APP_PRIVATE_KEY --body "$(cat key.pem)"',
    );
    ui.info(
      ui.dim(
        '       Use the --body "$VALUE" form; passing a secret on stdin mangles it.',
      ),
    );
  }

  if (harnesses.includes('claude')) {
    ui.step(
      'Reference `.claude/REVIEW_WORKFLOW.md` and `.claude/MODEL_NOTES.md` from your CLAUDE.md — ' +
        'without that they are dormant.',
    );
  }
}

module.exports = {
  init,
  renderConfig,
  chooseHarnesses,
  writeWorkflow,
  checkPython,
  refuseSelfSync,
  yamlScalar,
  TODO,
};
