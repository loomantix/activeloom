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
 * and `tests/test_cli_init_equivalence.py` demonstrates it rather than
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
const {
  HARNESSES,
  chooseHarnesses: sharedChooseHarnesses,
} = require('./detect');

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
const TIER2_SKIPPED_WORKFLOW = '.github/workflows/dco.yml';

/**
 * Verify that preserved consumer configs cannot ask a Tier 2 workflow to push
 * a workflow file with GITHUB_TOKEN. Canonical config has one source; legacy
 * configs compose top-level skips by intersection, so every source must carry
 * the entry.
 *
 * @param {string} python
 * @param {string[]} configPaths
 * @returns {{ok: true} | {ok: false, reason: string}}
 */
function checkTier2Config(python, configPaths) {
  const script = [
    'import sys, yaml',
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
  if (yaml.error || yaml.signal) {
    // Not a PyYAML problem: the interpreter that answered `--version` a moment
    // ago could not be started at all. Offering `pip install pyyaml` here would
    // send the user after the wrong thing.
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
  return sharedChooseHarnesses(facts, requested, {
    preferRepo: true,
    noEvidenceReason: 'no evidence either way — defaulting to Claude Code',
  });
}

/**
 * Show what was detected and let the user correct it before anything is written.
 *
 * Detection can only establish that a `claude` binary is on PATH or a `.codex`
 * directory exists. That is a proxy for "this is what I drive sessions with",
 * and it is a leaky one: a developer with all three CLIs installed would
 * otherwise get all three harness trees committed to a shared repository
 * without ever being asked. `init` writes into a repo the whole team pulls, so
 * the inference is worth one question.
 *
 * Not asked when:
 *   - `--harness` was given. The user already stated the answer; re-asking
 *     second-guesses a decision they just typed.
 *   - `--yes`, or stdin is not a TTY. Scripts, CI, and `npx | sh` pipelines
 *     must never block on a question nobody can answer.
 *
 * `prompts` is injected so the branching is testable without a terminal; the
 * default wires it to the real `ui` implementations.
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
  // Bounded rather than unbounded: a user who cannot name a valid harness in
  // three tries is better served by the error and the `--harness` flag than by
  // a loop they have to Ctrl-C out of.
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
    // De-duplicated, and ordered by the manifest rather than by typing order,
    // so the generated config is stable regardless of how it was entered.
    const ordered = HARNESSES.map((h) => h.id).filter((id) => ids.includes(id));
    return { ids: ordered, reason: 'confirmed' };
  }

  throw new Error(
    `could not read a harness list. Pass them explicitly: --harness ${[...known][0]}`,
  );
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
 * @param {number} [args.tierNumber] Tier this config is being written for.
 * @returns {string}
 */
function renderConfig({ harnesses, facts, tierNumber = 1 }) {
  // Tier 2's only identity is the workflow's built-in `GITHUB_TOKEN`, and
  // GitHub refuses a `GITHUB_TOKEN` push whose commit touches
  // `.github/workflows/`. There is no way to grant it: the Actions
  // `permissions:` block has no `workflows` key. So the shared target set's
  // `.github/workflows/dco.yml` has to be skipped at that tier or the very
  // first scheduled sync dies on `git push`, after committing. Tier 1 runs the
  // engine locally and tier 3 commits through an App installation token, so
  // neither is affected.
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
# \`.github/workflows/dco.yml\` is skipped because this repo syncs with the
# workflow's built-in GITHUB_TOKEN (tier 2), and GitHub refuses a GITHUB_TOKEN
# push whose commit creates or updates any file under \`.github/workflows/\`.
# To receive it, either add the file by hand once, or move to tier 3
# (\`npx activeloom init --sync --app\`) and delete this entry.
skip_targets:
  - .github/workflows/dco.yml`
    : `# Required before the sync may write a sensitive path. The shared target set
# ships \`.github/workflows/dco.yml\`, which every consumer receives, so this
# entry is what lets your first sync run. A refusal names any others it needs,
# in a block you can paste as-is.
allow_sensitive_writes:
  - .github/workflows/dco.yml

# Opt out of specific upstream files by source or destination path.
skip_targets: []`
}
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
  // Anchored on the exact placeholder text the templates ship, and asserted one
  // at a time. A silent no-op would install a workflow that fails on its first
  // scheduled run — with `UPSTREAM_REPO: <owner>/<repo>`, or with an empty
  // `PR_BASE_BRANCH` the template itself refuses — and comparing the finished
  // body against the original cannot see that, because either substitution
  // landing on its own makes the two differ.
  //
  // The `UPSTREAM_REPO` pattern consumes the trailing guidance comment along
  // with the placeholder: leaving it behind produces
  // `UPSTREAM_REPO: loomantix/activeloom # e.g. loomantix/activeloom`, which
  // reads as though the value were still unset.
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
    // The ref the trees were rendered from, not the one the template happens
    // to ship. Installing content from one ref beside a workflow that tracks
    // another means the next scheduled sync opens a PR reverting what `init`
    // just wrote. `local` is the `--upstream-dir` case: there is no remote ref
    // to track, so the template's own default is left standing.
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

  const detected = chooseHarnesses(facts, args.harnesses);
  const chosen = await confirmHarnesses(detected, facts, {
    assumeYes: args.assumeYes === true,
    explicit: args.harnesses.length > 0,
  });
  ui.step(
    `harnesses: ${ui.bold(chosen.ids.join(', '))} ${ui.dim(`(${chosen.reason})`)}`,
  );

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

  // --- config ---------------------------------------------------------------
  const configPath = path.join(facts.repoDir, '.activeloom-config.yml');
  const configExists = fs.existsSync(configPath);
  const legacyConfigs = HARNESSES.map((harness) => harness.legacyConfig)
    .map((name) => path.join(facts.repoDir, name))
    .filter((candidate) => fs.existsSync(candidate));
  if (tier.n === 2 && (configExists || legacyConfigs.length > 0)) {
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
  if ((configExists || legacyConfigs.length > 0) && args.harnesses.length > 0) {
    ui.fail(
      '`--harness` only applies when creating a new config; edit the existing config harness list, then re-run `init`.',
    );
    return 1;
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
    '--reject-consumer-symlinks',
  ];
  if (dryRun) engineArgs.push('--dry-run');

  // The engine reads `.activeloom-config.yml` off disk and exits non-zero
  // without one. A dry run over a repository being onboarded has not written
  // it — that is what makes the run dry — so invoking the engine here would
  // fail every first-time `--dry-run` with a missing-file error about a file
  // the user was never asked to create. Say what would happen instead.
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
    // `status` is null when the process never ran or was killed, and `stdout`
    // and `stderr` are null with it — so testing `status !== 0` alone reports
    // a spawn failure as an engine refusal with nothing to act on. "Refused"
    // is a claim about a decision the engine made; only say it when it made
    // one.
    if (run.error || run.signal) {
      ui.fail(
        `could not run the sync engine (${python}): ${run.error ? run.error.message : `killed by ${run.signal}`}`,
      );
      return 1;
    }
    if (run.status !== 0) {
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
      // The engine's own refusals are precise and paste-ready (it prints the
      // exact `allow_sensitive_writes` block a config is missing), so surface
      // them verbatim rather than summarising them into something vaguer.
      return run.status ?? 1;
    }
  }

  // --- the workflow ---------------------------------------------------------
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
