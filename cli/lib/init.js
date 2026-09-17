'use strict';

/**
 * Tiers 1-3 — `npx activeloom init [--sync] [--app]`.
 *
 * Initializes harness roots, consumer configuration, and scheduled sync workflows.
 */

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const ui = require('./ui');
const { resolveTier } = require('./tiers');
const {
  HARNESSES,
  chooseHarnesses: sharedChooseHarnesses,
} = require('./detect');

/**
 * Placeholder marker for values requiring human or agent configuration.
 */
const TODO = 'TODO(activeloom): ';
const TIER2_SKIPPED_WORKFLOW = '.github/workflows/dco.yml';

/**
 * Verify that existing consumer configs skip workflow pushes under Tier 2.
 *
 * @param {string} python
 * @param {string[]} configPaths
 * @returns {{ok: true} | {ok: false, reason: string}}
 */
function checkTier2Config(python, configPaths) {
  const script = [
    // Strip working directory from sys.path to prevent shadowing PyYAML.
    'import sys',
    'sys.path = [entry for entry in sys.path if entry]',
    'import yaml',
    'target = sys.argv[1]',
    'for filename in sys.argv[2:]:',
    '    try:',
    '        with open(filename, encoding="utf-8") as stream:',
    '            doc = yaml.safe_load(stream)',
    '    except Exception as exc:',
    '        print(f"{filename}: {exc}", file=sys.stderr)',
    '        raise SystemExit(2)',
    '    skips = doc.get("skip_targets", []) if isinstance(doc, dict) else []',
    '    if not isinstance(skips, list) or target not in skips:',
    '        raise SystemExit(10)',
  ].join('\n');
  const run = spawnSync(
    python,
    ['-c', script, TIER2_SKIPPED_WORKFLOW, ...configPaths],
    { encoding: 'utf8' },
  );
  if (run.error || run.signal) {
    return {
      ok: false,
      reason: `could not inspect the existing config: ${run.error ? run.error.message : `killed by ${run.signal}`}`,
    };
  }
  if (run.status === 0) return { ok: true };
  if (run.status === 10) {
    return {
      ok: false,
      reason: `the existing config must skip ${TIER2_SKIPPED_WORKFLOW} before Tier 2 can use GITHUB_TOKEN`,
    };
  }
  return {
    ok: false,
    reason: `could not inspect the existing config${run.stderr ? `: ${run.stderr.trim()}` : ''}`,
  };
}

/**
 * Assert the python interpreter is executable and can import PyYAML.
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
  // Strip working directory from sys.path to avoid local yaml.py shadowing.
  const yaml = spawnSync(
    python,
    ['-c', 'import sys; sys.path = [p for p in sys.path if p]; import yaml'],
    { encoding: 'utf8' },
  );
  if (yaml.error || yaml.signal) {
    return {
      ok: false,
      reason: `\`${python}\` could not be run: ${yaml.error ? yaml.error.message : `killed by ${yaml.signal}`}`,
      remedy: 'Check the interpreter path, or pass --python <path>.',
    };
  }
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
 * Refuse to sync an upstream into itself or another activeloom upstream.
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

  // Prevent syncing into another activeloom upstream repository.
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
 * Choose harnesses for repository initialization, preferring repo evidence.
 *
 * @param {ReturnType<import('./detect').detect>} facts
 * @param {string[]} requested
 * @returns {{ids: string[], reason: string}}
 */
function chooseHarnesses(facts, requested) {
  return sharedChooseHarnesses(facts, requested, {
    preferRepo: true,
    noEvidenceReason: 'no evidence either way — defaulting to Claude Code',
  });
}

/**
 * Interactive prompt to confirm or adjust detected harnesses before writing.
 *
 * @param {{ids: string[], reason: string}} chosen
 * @param {ReturnType<import('./detect').detect>} facts
 * @param {object} options
 * @param {boolean} options.assumeYes
 * @param {boolean} options.explicit  `--harness` was passed.
 * @param {{confirm: typeof ui.confirm, ask: typeof ui.ask}} [prompts]
 * @returns {Promise<{ids: string[], reason: string}>}
 */
async function confirmHarnesses(chosen, facts, options, prompts = ui) {
  if (options.explicit || options.assumeYes || !process.stdin.isTTY) {
    return chosen;
  }

  ui.info('');
  ui.info(ui.bold('  Detected harnesses'));
  for (const h of facts.harnesses) {
    const mark = chosen.ids.includes(h.id) ? ui.green('•') : ' ';
    ui.info(`   ${mark} ${h.id.padEnd(8)} ${ui.harnessSignals(h)}`);
  }
  ui.info('');

  const accepted = await prompts.confirm(
    `  Write ${ui.bold(chosen.ids.join(', '))} into this repo?`,
    true,
  );
  if (accepted) return chosen;

  const known = new Set(HARNESSES.map((h) => h.id));
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const answer = await prompts.ask(`  Which? (${[...known].join(' / ')}) >`);
    const ids = answer.split(/[\s,]+/).filter(Boolean);
    if (ids.length === 0) {
      ui.warn('  name at least one harness, or press Ctrl-C to abort.');
      continue;
    }
    const unknown = ids.filter((id) => !known.has(id));
    if (unknown.length > 0) {
      ui.warn(`  unknown: ${unknown.join(', ')}`);
      continue;
    }
    // Order harnesses consistently according to the manifest.
    const ordered = HARNESSES.map((h) => h.id).filter((id) => ids.includes(id));
    return { ids: ordered, reason: 'confirmed' };
  }

  throw new Error(
    `could not read a harness list. Pass them explicitly: --harness ${[...known][0]}`,
  );
}

/**
 * Quote a scalar for YAML output using single-quotes.
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
 * @param {object} args
 * @param {string[]} args.harnesses
 * @param {ReturnType<import('./detect').detect>} args.facts
 * @param {number} [args.tierNumber] Tier this config is being written for.
 * @returns {string}
 */
function renderConfig({ harnesses, facts, tierNumber = 1 }) {
  // Tier 2 uses GITHUB_TOKEN which cannot push workflow changes.
  const skipsWorkflowTarget = tierNumber === 2;
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

${
  skipsWorkflowTarget
    ? `# Required before the sync may write a sensitive path. A refusal names any
# others it needs, in a block you can paste as-is.
allow_sensitive_writes: []

# Opt out of specific upstream files by source or destination path.
#
# The shared workflows are skipped because this repo syncs with the workflow's
# built-in GITHUB_TOKEN (tier 2), and GitHub refuses a GITHUB_TOKEN push whose
# commit creates or updates any file under \`.github/workflows/\`.
# To receive them, either add the files by hand once, or move to tier 3
# (\`npx activeloom init --sync --app\`) and delete these entries.
skip_targets:
  - .github/workflows/dco.yml
  - .github/workflows/review-glance-label.yml`
    : `# Required before the sync may write a sensitive path. The shared target set
# ships two workflows every consumer receives, so these entries are what let
# your first sync run. A refusal names any others it needs, in a block you can
# paste as-is.
allow_sensitive_writes:
  - .github/workflows/dco.yml
  - .github/workflows/review-glance-label.yml

# Opt out of specific upstream files by source or destination path.
skip_targets: []`
}
`;
}

/**
 * Write the tier-appropriate sync workflow into the consumer repository.
 *
 * @param {object} args
 * @param {import('./tiers').Tier} args.tier
 * @param {string} args.upstreamDir
 * @param {string} args.repoDir
 * @param {string} args.upstreamRepo
 * @param {string} args.upstreamRef  the ref the trees were rendered from
 * @param {string} args.baseBranch
 * @param {boolean} args.dryRun
 * @param {boolean} args.force
 * @returns {{written: boolean, ready: boolean, dest: string, note?: string}}
 */
function writeWorkflow({
  tier,
  upstreamDir,
  repoDir,
  upstreamRepo,
  upstreamRef,
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
  assertSafeWritePath(repoDir, dest);

  let body = fs.readFileSync(src, 'utf8');
  // Substitute configured upstream repo and PR base branch placeholders.
  const substitutions = [
    {
      key: 'UPSTREAM_REPO',
      pattern: /UPSTREAM_REPO: <owner>\/<repo>[^\n]*/,
      replacement: `UPSTREAM_REPO: ${yamlScalar(upstreamRepo)}`,
    },
    {
      key: 'PR_BASE_BRANCH',
      pattern: /PR_BASE_BRANCH: ''[^\n]*/,
      replacement: `PR_BASE_BRANCH: ${yamlScalar(baseBranch)}`,
    },
    // Track the ref that trees were rendered from.
    ...(upstreamRef && upstreamRef !== 'local'
      ? [
          {
            key: 'UPSTREAM_REF',
            pattern: /UPSTREAM_REF: [^\n]*/,
            replacement: `UPSTREAM_REF: ${yamlScalar(upstreamRef)}`,
          },
        ]
      : []),
  ];
  const missing = substitutions
    .filter((substitution) => !substitution.pattern.test(body))
    .map((substitution) => substitution.key);
  if (missing.length > 0) {
    throw new Error(
      `${templateName} did not contain the expected ${missing.join(', ')} placeholder${missing.length > 1 ? 's' : ''}.`,
    );
  }
  for (const substitution of substitutions) {
    body = body.replace(substitution.pattern, () => substitution.replacement);
  }

  if (fs.existsSync(dest)) {
    const existing = fs.readFileSync(dest, 'utf8');
    if (existing === body) {
      return { written: false, ready: true, dest, note: 'already matches' };
    }
    if (!force) {
      return {
        written: false,
        ready: false,
        dest,
        note: 'differs from the requested tier — re-run with --force to replace the generated workflow (your config is preserved)',
      };
    }
  }

  if (!dryRun) {
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, body);
  }
  return { written: true, ready: true, dest };
}

/** Refuse a write through any symlink below the consumer root. */
function assertSafeWritePath(repoDir, dest) {
  const relative = path.relative(repoDir, dest);
  if (relative.startsWith('..') || path.isAbsolute(relative)) {
    throw new Error(
      `refusing to write outside the consumer repository: ${dest}`,
    );
  }
  let current = repoDir;
  for (const part of relative.split(path.sep)) {
    current = path.join(current, part);
    let stat;
    try {
      stat = fs.lstatSync(current);
    } catch (error) {
      if (error.code === 'ENOENT') continue;
      throw error;
    }
    if (stat.isSymbolicLink()) {
      throw new Error(`refusing to write through symlink: ${current}`);
    }
  }
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

  // Guard against self-sync.
  const selfSync = refuseSelfSync(facts.repoDir, upstreamDir);
  if (selfSync) {
    ui.fail(selfSync);
    return 1;
  }

  // Tiers 2 and 3 require a GitHub origin remote.
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

  // Existing configuration owns the harness list.
  const configPath = path.join(facts.repoDir, '.activeloom-config.yml');
  const configExists = fs.existsSync(configPath);
  const legacyConfigs = HARNESSES.map((harness) => harness.legacyConfig)
    .map((name) => path.join(facts.repoDir, name))
    .filter((candidate) => fs.existsSync(candidate));
  const keepingConfig = configExists || legacyConfigs.length > 0;
  if (keepingConfig && args.harnesses.length > 0) {
    ui.fail(
      '`--harness` only applies when creating a new config; edit the existing config harness list, then re-run `init`.',
    );
    return 1;
  }
  let chosen = { ids: [], reason: 'from the existing config' };
  if (keepingConfig) {
    const source = configExists
      ? '.activeloom-config.yml'
      : legacyConfigs.map((entry) => path.basename(entry)).join(', ');
    ui.step(`harnesses: ${ui.dim(`from ${source}`)}`);
  } else {
    const detected = chooseHarnesses(facts, args.harnesses);
    chosen = await confirmHarnesses(detected, facts, {
      assumeYes: args.assumeYes === true,
      explicit: args.harnesses.length > 0,
    });
    ui.step(
      `harnesses: ${ui.bold(chosen.ids.join(', '))} ${ui.dim(`(${chosen.reason})`)}`,
    );
  }

  const baseBranch = args.baseBranch ?? facts.git.defaultBranch ?? 'main';
  const workflowArgs = {
    tier,
    upstreamDir,
    repoDir: facts.repoDir,
    upstreamRepo,
    upstreamRef: args.ref,
    baseBranch,
    force,
  };
  if (tier.n >= 2) {
    const preflight = writeWorkflow({ ...workflowArgs, dryRun: true });
    if (!preflight.ready) {
      ui.fail(`workflow:   ${preflight.note}`);
      return 1;
    }
  }

  if (tier.n === 2 && keepingConfig) {
    const check = checkTier2Config(
      python,
      configExists ? [configPath] : legacyConfigs,
    );
    if (!check.ok) {
      ui.fail(`config:     ${check.reason}.`);
      ui.info('  Preserve your existing values and add this top-level entry:');
      ui.info('      skip_targets:');
      ui.info(`        - ${TIER2_SKIPPED_WORKFLOW}`);
      ui.info('  Then re-run `npx activeloom init --sync`.');
      return 1;
    }
  }
  let configWritten = false;
  if (configExists) {
    ui.step(
      `config:     ${ui.dim('.activeloom-config.yml exists — keeping yours')}`,
    );
  } else if (legacyConfigs.length > 0) {
    ui.step(
      `config:     ${ui.dim(`keeping legacy config${legacyConfigs.length > 1 ? 's' : ''} (${legacyConfigs.map((entry) => path.basename(entry)).join(', ')}); the sync engine will compose them`)}`,
    );
  } else {
    const body = renderConfig({
      harnesses: chosen.ids,
      facts,
      tierNumber: tier.n,
    });
    assertSafeWritePath(facts.repoDir, configPath);
    if (!dryRun) {
      fs.writeFileSync(configPath, body);
      configWritten = true;
    }
    ui.step(
      `config:     ${dryRun ? 'would write' : 'wrote'} .activeloom-config.yml`,
    );
  }

  // Invoke the sync engine with identical arguments to CI sync.
  const engine = path.join(upstreamDir, 'scripts', 'sync-engine.py');
  const engineArgs = [
    engine,
    '--upstream-repo',
    upstreamDir,
    '--consumer-dir',
    facts.repoDir,
    '--reject-consumer-symlinks',
  ];
  if (dryRun) engineArgs.push('--dry-run');

  // The sync engine requires config on disk, which dry-run skips writing on new repos.
  const engineCanRun =
    !dryRun || fs.existsSync(configPath) || legacyConfigs.length > 0;
  if (!engineCanRun) {
    ui.step(
      `trees:      ${ui.dim('skipped — the engine needs .activeloom-config.yml on disk, and a dry run has not written it')}`,
    );
  } else {
    ui.step(`trees:      running the sync engine from ${ui.bold(args.ref)}`);
    const run = spawnSync(python, engineArgs, {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    // Distinguish subprocess spawn failure from engine refusal.
    if (run.error || run.signal) {
      if (configWritten) {
        fs.rmSync(configPath, { force: true });
        configWritten = false;
      }
      ui.fail(
        `could not run the sync engine (${python}): ${run.error ? run.error.message : `killed by ${run.signal}`}`,
      );
      return 1;
    }
    if (run.status !== 0) {
      if (configWritten) {
        fs.rmSync(configPath, { force: true });
        configWritten = false;
      }
      const missingSymlinkGuard = run.stderr?.includes(
        'unrecognized arguments: --reject-consumer-symlinks',
      );
      ui.fail(
        missingSymlinkGuard
          ? `upstream ref ${args.ref} predates safe local onboarding; choose a newer ref whose sync engine supports consumer-symlink preflight.`
          : 'the sync engine refused to write.',
      );
      if (run.stdout) process.stdout.write(run.stdout);
      if (run.stderr) process.stderr.write(run.stderr);
      return run.status ?? 1;
    }
    if (run.stdout) process.stdout.write(run.stdout);
    if (run.stderr) process.stderr.write(run.stderr);
  }

  if (tier.n >= 2) {
    const result = writeWorkflow({ ...workflowArgs, dryRun });
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
  printNextSteps(tier, chosen.ids, facts, configWritten);
  return 0;
}

/**
 * Print post-init instructions for completing configuration and setup.
 *
 * @param {import('./tiers').Tier} tier
 * @param {string[]} harnesses
 * @param {ReturnType<import('./detect').detect>} facts
 * @param {boolean} configWritten
 */
function printNextSteps(tier, harnesses, facts, configWritten) {
  ui.info('');
  ui.info(ui.bold('Next:'));

  if (configWritten) {
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
    ui.step(
      'On each workflow-created sync PR, select “Approve workflows to run” before its pull-request checks can start.',
    );
  }

  if (tier.n === 3) {
    ui.step(
      'Install the App with Contents: write, Pull requests: write, and Workflows: write.',
    );
    ui.step('Set the two App secrets on the repo:');
    ui.info('       gh secret set SYNC_APP_ID');
    ui.info('       gh secret set SYNC_APP_PRIVATE_KEY < key.pem');
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
  confirmHarnesses,
  yamlScalar,
  TODO,
  assertSafeWritePath,
  checkTier2Config,
};
