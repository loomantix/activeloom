'use strict';

/**
 * Unit tests for CLI parsing, tier resolution, harness selection, and config rendering.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const { parseArgs, validateCommandArgs } = require(
  path.join(REPO_ROOT, 'cli', 'bin', 'activeloom.js'),
);
const { detect } = require(path.join(REPO_ROOT, 'cli', 'lib', 'detect.js'));
const { TIERS, resolveTier } = require(
  path.join(REPO_ROOT, 'cli', 'lib', 'tiers.js'),
);
const initLib = require(path.join(REPO_ROOT, 'cli', 'lib', 'init.js'));
const addLib = require(path.join(REPO_ROOT, 'cli', 'lib', 'add.js'));
const ui = require(path.join(REPO_ROOT, 'cli', 'lib', 'ui.js'));

/** Minimal detect() result, overridable per test. */
function facts(overrides = {}) {
  return {
    repoDir: '/tmp/consumer',
    homeDir: '/tmp/home',
    harnesses: [
      {
        id: 'claude',
        root: '.claude',
        inRepo: false,
        onMachine: false,
        cliInstalled: false,
      },
      {
        id: 'codex',
        root: '.codex',
        inRepo: false,
        onMachine: false,
        cliInstalled: false,
      },
      {
        id: 'gemini',
        root: '.agents',
        inRepo: false,
        onMachine: false,
        cliInstalled: false,
      },
    ],
    packageManager: null,
    ecosystems: [],
    scripts: { test: null, lint: null, format: null },
    git: {
      inRepo: true,
      remote: null,
      slug: null,
      defaultBranch: null,
      ghAuthenticated: false,
    },
    hasWorkflows: false,
    hasConfig: false,
    ...overrides,
  };
}

test('parseArgs reads a command and its positionals', () => {
  const opts = parseArgs(['add', 'critique', 'issues']);
  assert.strictEqual(opts.command, 'add');
  assert.deepStrictEqual(opts.positionals, ['critique', 'issues']);
});

test('parseArgs accumulates repeated --harness', () => {
  const opts = parseArgs(['init', '--harness', 'claude', '--harness', 'codex']);
  assert.deepStrictEqual(opts.harnesses, ['claude', 'codex']);
});

test('parseArgs rejects an unknown option instead of ignoring it', () => {
  // Unknown options must throw rather than silently passing.
  assert.throws(() => parseArgs(['init', '--forse']), /unknown option --forse/);
});

test('parseArgs rejects a value-taking flag with no value', () => {
  assert.throws(() => parseArgs(['init', '--ref']), /--ref needs a value/);
});

test('commands reject operands and options they cannot apply', () => {
  const initArgv = ['init', 'critique'];
  assert.match(
    validateCommandArgs(parseArgs(initArgv), initArgv),
    /init does not accept operands: critique/,
  );

  const addArgv = ['add', 'critique', '--app'];
  assert.match(
    validateCommandArgs(parseArgs(addArgv), addArgv),
    /add does not accept --app/,
  );
});

test('detect resolves a repository subdirectory to the git top level', () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-repo-'));
  const nested = path.join(repo, 'packages', 'widget');
  try {
    execFileSync('git', ['init', '-q', repo]);
    fs.mkdirSync(nested, { recursive: true });
    assert.strictEqual(detect({ repoDir: nested }).repoDir, repo);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test('the ladder is monotonic and complete', () => {
  assert.deepStrictEqual(
    TIERS.map((t) => t.n),
    [0, 1, 2, 3],
  );
});

test('flags resolve to the documented tiers', () => {
  assert.strictEqual(resolveTier({}).n, 1);
  assert.strictEqual(resolveTier({ sync: true }).n, 2);
  assert.strictEqual(resolveTier({ sync: true, app: true }).n, 3);
});

test('--app alone still resolves to tier 3', () => {
  // App flag implies scheduled sync; bare app flag is not a distinct tier.
  assert.strictEqual(resolveTier({ app: true }).n, 3);
});

test('only tier 3 requires a credential', () => {
  for (const tier of TIERS) {
    const needsCredential = !/^none/.test(tier.credential);
    assert.strictEqual(
      needsCredential,
      tier.n === 3,
      `tier ${tier.n} credential "${tier.credential}" contradicts the ladder`,
    );
  }
});

test('init prefers harnesses already checked into the repo', () => {
  const f = facts();
  f.harnesses[1].inRepo = true; // codex in repo
  f.harnesses[0].cliInstalled = true; // claude only on this machine
  const chosen = initLib.chooseHarnesses(f, []);
  // Harnesses present in the repository outrank machine-only signals.
  assert.deepStrictEqual(chosen.ids, ['codex']);
});

test('init falls back to machine evidence, then to claude', () => {
  const onMachine = facts();
  onMachine.harnesses[2].onMachine = true;
  assert.deepStrictEqual(initLib.chooseHarnesses(onMachine, []).ids, [
    'gemini',
  ]);

  const nothing = facts();
  const chosen = initLib.chooseHarnesses(nothing, []);
  assert.deepStrictEqual(chosen.ids, ['claude']);
  assert.match(chosen.reason, /defaulting/);
});

test('an unknown harness is refused by name', () => {
  assert.throws(
    () => initLib.chooseHarnesses(facts(), ['gemeni']),
    /unknown harness gemeni/,
  );
});

test('chooseHarnesses deduplicates repeated explicit harnesses', () => {
  const chosen = initLib.chooseHarnesses(facts(), ['claude', 'claude']);
  assert.deepStrictEqual(chosen.ids, ['claude']);
});

test('the generated config never declares the reserved telemetry key', () => {
  // The engine computes REVIEW_TELEMETRY_ENV; config must not declare it.
  const body = initLib.renderConfig({ harnesses: ['claude'], facts: facts() });
  assert.ok(!body.includes('REVIEW_TELEMETRY_ENV'));
});

test('only tier 2 skips the workflow target GITHUB_TOKEN cannot push', () => {
  // Tier 2 uses GITHUB_TOKEN and must skip pushing changes to workflow files.
  const render = (tierNumber) =>
    initLib.renderConfig({ harnesses: ['claude'], facts: facts(), tierNumber });

  const tier2 = render(2);
  assert.match(
    tier2,
    /^skip_targets:\n {2}- \.github\/workflows\/dco\.yml\n {2}- \.github\/workflows\/review-glance-label\.yml$/m,
  );
  assert.match(tier2, /^allow_sensitive_writes: \[\]$/m);

  for (const tierNumber of [1, 3]) {
    const body = render(tierNumber);
    assert.match(
      body,
      /^allow_sensitive_writes:\n {2}- \.github\/workflows\/dco\.yml\n {2}- \.github\/workflows\/review-glance-label\.yml$/m,
      `tier ${tierNumber} must still receive the shared workflow targets`,
    );
    assert.match(body, /^skip_targets: \[\]$/m);
  }
});

test('the generated config marks what it could not determine', () => {
  const body = initLib.renderConfig({ harnesses: ['claude'], facts: facts() });
  assert.ok(
    body.includes(initLib.TODO),
    'a config with no detected prose must say so',
  );
  assert.match(body, /^harnesses: \[claude\]$/m);
});

test('detected facts reach the config, and are not invented when absent', () => {
  const detected = facts({
    ecosystems: ['node'],
    packageManager: 'pnpm',
    scripts: { test: 'test', lint: 'lint', format: null },
    git: {
      inRepo: true,
      remote: null,
      slug: 'acme/widget',
      defaultBranch: 'main',
      ghAuthenticated: true,
    },
  });
  const body = initLib.renderConfig({ harnesses: ['claude'], facts: detected });

  assert.match(body, /PROJECT_NAME: 'widget'/);
  assert.ok(body.includes('| Packages | pnpm |'));
  assert.ok(body.includes('pnpm run test'));
  assert.ok(body.includes('pnpm run lint'));
  assert.ok(!body.includes('run format'));
});

test('config scalars are quoted so punctuation cannot restructure the YAML', () => {
  assert.strictEqual(initLib.yamlScalar("it's"), "'it''s'");
  assert.strictEqual(initLib.yamlScalar('a: b #c'), "'a: b #c'");
});

test('add refuses a skill tree containing an unsubstituted placeholder', () => {
  // Reject unsubstituted placeholder tokens in skills.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-test-'));
  try {
    fs.writeFileSync(
      path.join(dir, 'SKILL.md'),
      'Review using <<PROJECT_NAME>> conventions.\n',
    );
    assert.throws(
      () => addLib.assertNoPlaceholders(dir, 'claude/demo'),
      /unsubstituted placeholder/,
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('add accepts a clean skill tree, including nested files', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-test-'));
  try {
    fs.mkdirSync(path.join(dir, 'scripts'));
    fs.writeFileSync(path.join(dir, 'SKILL.md'), '# Demo\n');
    fs.writeFileSync(path.join(dir, 'scripts', 'helper.py'), 'print("ok")\n');
    assert.doesNotThrow(() => addLib.assertNoPlaceholders(dir, 'claude/demo'));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('copyTree preserves the executable bit', () => {
  // Preserve executable permission bits when copying skills.
  const src = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-src-'));
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-dst-'));
  try {
    const script = path.join(src, 'ready.py');
    fs.writeFileSync(script, '#!/usr/bin/env python3\n');
    fs.chmodSync(script, 0o755);
    fs.writeFileSync(path.join(src, 'SKILL.md'), '# Demo\n');

    addLib.copyTree(src, path.join(dest, 'out'));

    assert.strictEqual(
      fs.statSync(path.join(dest, 'out', 'ready.py')).mode & 0o777,
      0o755,
    );
    assert.strictEqual(
      fs.statSync(path.join(dest, 'out', 'SKILL.md')).mode & 0o777,
      0o644,
    );
  } finally {
    fs.rmSync(src, { recursive: true, force: true });
    fs.rmSync(dest, { recursive: true, force: true });
  }
});

test('Tier 0 installs the harness support files used by critique', () => {
  const upstream = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-up-'));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-home-'));
  try {
    const root = path.join(upstream, '.codex');
    fs.mkdirSync(path.join(root, 'references', 'roles'), { recursive: true });
    fs.writeFileSync(path.join(root, 'REVIEW_WORKFLOW.md'), '# Workflow\n');
    fs.writeFileSync(path.join(root, 'prompt-stack.json'), '{}\n');
    fs.writeFileSync(
      path.join(root, 'references', 'local-review-ledger.md'),
      '# Ledger\n',
    );
    fs.writeFileSync(
      path.join(root, 'references', 'roles', 'code-reviewer.md'),
      '# Reviewer\n',
    );
    fs.mkdirSync(path.join(home, '.codex', 'references'), { recursive: true });
    fs.writeFileSync(
      path.join(home, '.codex', 'references', 'consumer-note.md'),
      '# Keep me\n',
    );
    addLib.installSupportFiles(
      upstream,
      { root: '.codex', home: '.codex' },
      home,
      false,
      false,
    );
    assert.strictEqual(
      fs.readFileSync(path.join(home, '.codex', 'REVIEW_WORKFLOW.md'), 'utf8'),
      '# Workflow\n',
    );
    assert.strictEqual(
      fs.readFileSync(
        path.join(home, '.codex', 'references', 'roles', 'code-reviewer.md'),
        'utf8',
      ),
      '# Reviewer\n',
    );
    assert.strictEqual(
      fs.readFileSync(
        path.join(home, '.codex', 'references', 'consumer-note.md'),
        'utf8',
      ),
      '# Keep me\n',
    );

    const claudeRoot = path.join(upstream, '.claude');
    fs.mkdirSync(path.join(claudeRoot, 'agents'), { recursive: true });
    fs.writeFileSync(
      path.join(claudeRoot, 'agents', 'code-explorer.md'),
      '# Explorer\n',
    );
    addLib.installSupportFiles(
      upstream,
      { root: '.claude', home: '.claude' },
      home,
      false,
      false,
    );
    assert.strictEqual(
      fs.readFileSync(
        path.join(home, '.claude', 'agents', 'code-explorer.md'),
        'utf8',
      ),
      '# Explorer\n',
    );
  } finally {
    fs.rmSync(upstream, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('installSupportFiles rethrows filesystem errors other than ENOENT', () => {
  const upstream = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-up-'));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-home-'));
  const originalLstat = fs.lstatSync;
  try {
    const root = path.join(upstream, '.codex');
    fs.mkdirSync(root, { recursive: true });
    fs.writeFileSync(path.join(root, 'REVIEW_WORKFLOW.md'), '# Workflow\n');

    const eacces = new Error('permission denied');
    eacces.code = 'EACCES';
    fs.lstatSync = (p, ...rest) => {
      if (typeof p === 'string' && p.endsWith('REVIEW_WORKFLOW.md')) {
        throw eacces;
      }
      return originalLstat(p, ...rest);
    };

    assert.throws(
      () =>
        addLib.installSupportFiles(
          upstream,
          { root: '.codex', home: '.codex' },
          home,
          false,
          false,
        ),
      (err) => err.code === 'EACCES',
    );
  } finally {
    fs.lstatSync = originalLstat;
    fs.rmSync(upstream, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('installSupportFiles ignores ENOENT from lstatSync', () => {
  const upstream = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-up-'));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-home-'));
  const originalLstat = fs.lstatSync;
  try {
    const root = path.join(upstream, '.codex');
    fs.mkdirSync(root, { recursive: true });
    fs.writeFileSync(path.join(root, 'REVIEW_WORKFLOW.md'), '# Workflow\n');

    const enoent = new Error('not found');
    enoent.code = 'ENOENT';
    fs.lstatSync = (p, ...rest) => {
      if (typeof p === 'string' && p.endsWith('REVIEW_WORKFLOW.md')) {
        throw enoent;
      }
      return originalLstat(p, ...rest);
    };

    assert.doesNotThrow(() =>
      addLib.installSupportFiles(
        upstream,
        { root: '.codex', home: '.codex' },
        home,
        false,
        false,
      ),
    );
  } finally {
    fs.lstatSync = originalLstat;
    fs.rmSync(upstream, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('add chooses harnesses from machine evidence, not repo evidence', () => {
  // Tier 0 installs to user config directory and ignores repo contents.
  const f = facts();
  f.harnesses[0].inRepo = true;
  f.harnesses[1].onMachine = true;
  assert.deepStrictEqual(addLib.chooseHarnesses(f, []).ids, ['codex']);
});

test('refuseSelfSync catches the identical-directory case', () => {
  const msg = initLib.refuseSelfSync(REPO_ROOT, REPO_ROOT);
  assert.match(msg, /refusing to sync a tree into itself/);
});

test('refuseSelfSync catches a different activeloom checkout', () => {
  const msg = initLib.refuseSelfSync(REPO_ROOT, os.tmpdir());
  assert.match(msg, /is itself an activeloom upstream/);
});

test('refuseSelfSync permits an ordinary consumer', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  try {
    assert.strictEqual(initLib.refuseSelfSync(dir, REPO_ROOT), null);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('write destinations may not traverse repository symlinks', () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-outside-'));
  try {
    fs.symlinkSync(outside, path.join(repo, '.github'));
    assert.throws(
      () =>
        initLib.assertSafeWritePath(
          repo,
          path.join(repo, '.github', 'workflows', 'sync.yml'),
        ),
      /refusing to write through symlink/,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

const initArgs = (repoDir, overrides = {}) => ({
  upstreamDir: REPO_ROOT,
  ref: 'local',
  facts: facts({ repoDir }),
  harnesses: [],
  sync: false,
  app: false,
  dryRun: false,
  assumeYes: true,
  force: false,
  python: '/bin/true',
  baseBranch: undefined,
  upstreamRepo: 'loomantix/activeloom',
  ...overrides,
});

test('--force preserves an existing consumer-owned config', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  try {
    const config = path.join(repo, '.activeloom-config.yml');
    const original =
      'harnesses: [codex]\nsubstitutions:\n  PROJECT_NAME: kept\n';
    fs.writeFileSync(config, original);
    assert.strictEqual(await initLib.init(initArgs(repo, { force: true })), 0);
    assert.strictEqual(fs.readFileSync(config, 'utf8'), original);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test('Tier 2 refuses an existing config that would let GITHUB_TOKEN push a workflow', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  try {
    const config = path.join(repo, '.activeloom-config.yml');
    fs.writeFileSync(
      config,
      'harnesses: [codex]\nallow_sensitive_writes:\n  - .github/workflows/dco.yml\nskip_targets: []\n',
    );
    const remoteFacts = facts({
      repoDir: repo,
      git: {
        inRepo: true,
        remote: 'git@github.com:example/consumer.git',
        slug: 'example/consumer',
        defaultBranch: 'main',
        ghAuthenticated: false,
      },
    });
    assert.strictEqual(
      await initLib.init(
        initArgs(repo, { facts: remoteFacts, sync: true, python: 'python3' }),
      ),
      1,
    );
    assert.strictEqual(fs.existsSync(path.join(repo, '.github')), false);
    assert.match(fs.readFileSync(config, 'utf8'), /skip_targets: \[\]/);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test('Tier 2 config check reads the shared skip from the consumer config', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-configs-'));
  try {
    const safe = path.join(dir, 'safe.yml');
    const unsafe = path.join(dir, 'unsafe.yml');
    fs.writeFileSync(safe, 'skip_targets:\n  - .github/workflows/dco.yml\n');
    fs.writeFileSync(unsafe, 'skip_targets: []\n');
    assert.deepStrictEqual(initLib.checkTier2Config('python3', [safe]), {
      ok: true,
    });
    assert.strictEqual(initLib.checkTier2Config('python3', [unsafe]).ok, false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('a pre-sync-v2 per-harness config no longer stands in for the config', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  try {
    fs.writeFileSync(
      path.join(repo, '.platform-config.yml'),
      'substitutions: {}\n',
    );
    assert.strictEqual(await initLib.init(initArgs(repo)), 0);
    assert.strictEqual(
      fs.existsSync(path.join(repo, '.activeloom-config.yml')),
      true,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test('explicit harnesses are refused when a config already owns the list', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  try {
    fs.writeFileSync(
      path.join(repo, '.activeloom-config.yml'),
      'harnesses: [claude]\n',
    );
    assert.strictEqual(
      await initLib.init(initArgs(repo, { harnesses: ['codex'] })),
      1,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test('an old upstream engine fails closed with a compatibility error', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  const fakePython = path.join(repo, 'old-python');
  const failures = [];
  const originalFail = ui.fail;
  try {
    fs.writeFileSync(
      path.join(repo, '.activeloom-config.yml'),
      'harnesses: [codex]\n',
    );
    fs.writeFileSync(
      fakePython,
      '#!/bin/sh\ncase "$1" in --version) echo "Python 3.12.0"; exit 0;; -c) exit 0;; esac\necho "error: unrecognized arguments: --reject-consumer-symlinks" >&2\nexit 2\n',
      { mode: 0o755 },
    );
    ui.fail = (message) => failures.push(message);

    assert.strictEqual(
      await initLib.init(
        initArgs(repo, { python: fakePython, ref: 'old-ref' }),
      ),
      2,
    );
    assert.deepStrictEqual(failures, [
      'upstream ref old-ref predates safe local onboarding; choose a newer ref whose sync engine supports consumer-symlink preflight.',
    ]);
  } finally {
    ui.fail = originalFail;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test('init rolls back newly written config when the sync engine exits non-zero', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  const config = path.join(repo, '.activeloom-config.yml');
  const fakePython = path.join(repo, 'failing-python');
  try {
    fs.writeFileSync(
      fakePython,
      '#!/bin/sh\ncase "$1" in --version) echo "Python 3.12.0"; exit 0;; -c) exit 0;; esac\necho "allow_sensitive_writes refusal" >&2\nexit 1\n',
      { mode: 0o755 },
    );
    assert.strictEqual(
      await initLib.init(initArgs(repo, { python: fakePython })),
      1,
    );
    assert.strictEqual(
      fs.existsSync(config),
      false,
      'config written by this run must be removed on engine failure',
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test('init rolls back newly written config when the sync engine fails to spawn', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  const config = path.join(repo, '.activeloom-config.yml');
  const fakePython = path.join(repo, 'spawn-failing-python');
  try {
    fs.writeFileSync(
      fakePython,
      '#!/bin/sh\ncase "$1" in --version) echo "Python 3.12.0"; exit 0;; -c) chmod 0000 "$0"; exit 0;; esac\nexit 0\n',
      { mode: 0o755 },
    );
    assert.strictEqual(
      await initLib.init(initArgs(repo, { python: fakePython })),
      1,
    );
    assert.strictEqual(
      fs.existsSync(config),
      false,
      'config written by this run must be removed on spawn failure',
    );
  } finally {
    try {
      fs.chmodSync(fakePython, 0o755);
    } catch {}
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test('init preserves pre-existing config when the sync engine exits non-zero', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  const config = path.join(repo, '.activeloom-config.yml');
  const fakePython = path.join(repo, 'failing-python');
  try {
    fs.writeFileSync(config, 'harnesses: [claude]\n');
    fs.writeFileSync(
      fakePython,
      '#!/bin/sh\ncase "$1" in --version) echo "Python 3.12.0"; exit 0;; -c) exit 0;; esac\nexit 1\n',
      { mode: 0o755 },
    );
    assert.strictEqual(
      await initLib.init(initArgs(repo, { python: fakePython })),
      1,
    );
    assert.strictEqual(
      fs.existsSync(config),
      true,
      'pre-existing config must not be removed on engine failure',
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

/** Force the TTY branch on for interactive tests. */
function withTTY(value, fn) {
  const original = Object.getOwnPropertyDescriptor(process.stdin, 'isTTY');
  Object.defineProperty(process.stdin, 'isTTY', { value, configurable: true });
  return fn().finally(() => {
    if (original) Object.defineProperty(process.stdin, 'isTTY', original);
    else delete process.stdin.isTTY;
  });
}

/** Prompt doubles that record what they were asked. */
function prompts({ accept = true, answer = '' } = {}) {
  const asked = [];
  return {
    asked,
    confirm: async (q) => {
      asked.push(q);
      return accept;
    },
    ask: async (q) => {
      asked.push(q);
      return answer;
    },
  };
}

test('confirmHarnesses does not prompt when stdin is not a TTY', async () => {
  // Never prompt interactively when stdin is not a TTY.
  const p = prompts();
  await withTTY(false, async () => {
    const chosen = { ids: ['claude', 'codex'], reason: 'detected' };
    const out = await initLib.confirmHarnesses(
      chosen,
      facts(),
      { assumeYes: false, explicit: false },
      p,
    );
    assert.deepStrictEqual(out, chosen);
  });
  assert.deepStrictEqual(p.asked, [], 'nothing should have been asked');
});

test('confirmHarnesses does not prompt when --yes is passed', async () => {
  const p = prompts();
  await withTTY(true, async () => {
    const chosen = { ids: ['claude'], reason: 'detected' };
    const out = await initLib.confirmHarnesses(
      chosen,
      facts(),
      { assumeYes: true, explicit: false },
      p,
    );
    assert.deepStrictEqual(out, chosen);
  });
  assert.deepStrictEqual(p.asked, []);
});

test('confirmHarnesses does not re-ask what --harness already stated', async () => {
  // Do not prompt when harnesses were explicitly specified.
  const p = prompts();
  await withTTY(true, async () => {
    const chosen = { ids: ['codex'], reason: 'requested with --harness' };
    const out = await initLib.confirmHarnesses(
      chosen,
      facts(),
      { assumeYes: false, explicit: true },
      p,
    );
    assert.deepStrictEqual(out, chosen);
  });
  assert.deepStrictEqual(p.asked, []);
});

test('confirmHarnesses keeps the detected set when the user accepts', async () => {
  const p = prompts({ accept: true });
  await withTTY(true, async () => {
    const chosen = { ids: ['claude', 'codex', 'gemini'], reason: 'detected' };
    const out = await initLib.confirmHarnesses(
      chosen,
      facts(),
      { assumeYes: false, explicit: false },
      p,
    );
    assert.deepStrictEqual(out.ids, ['claude', 'codex', 'gemini']);
  });
  assert.strictEqual(
    p.asked.length,
    1,
    'should ask exactly once when accepted',
  );
});

test('confirmHarnesses takes a corrected list when the user declines', async () => {
  const p = prompts({ accept: false, answer: 'claude' });
  await withTTY(true, async () => {
    const chosen = { ids: ['claude', 'codex', 'gemini'], reason: 'detected' };
    const out = await initLib.confirmHarnesses(
      chosen,
      facts(),
      { assumeYes: false, explicit: false },
      p,
    );
    assert.deepStrictEqual(out.ids, ['claude']);
    assert.strictEqual(out.reason, 'confirmed');
  });
});

test('a corrected list is ordered by the manifest, not by typing order', async () => {
  // Normalize harness ordering according to manifest.
  const p = prompts({ accept: false, answer: 'gemini claude' });
  await withTTY(true, async () => {
    const out = await initLib.confirmHarnesses(
      { ids: ['claude', 'codex', 'gemini'], reason: 'detected' },
      facts(),
      { assumeYes: false, explicit: false },
      p,
    );
    assert.deepStrictEqual(out.ids, ['claude', 'gemini']);
  });
});

test('confirmHarnesses accepts comma-separated input and de-duplicates', async () => {
  const p = prompts({ accept: false, answer: 'codex, codex,claude' });
  await withTTY(true, async () => {
    const out = await initLib.confirmHarnesses(
      { ids: ['claude', 'codex', 'gemini'], reason: 'detected' },
      facts(),
      { assumeYes: false, explicit: false },
      p,
    );
    assert.deepStrictEqual(out.ids, ['claude', 'codex']);
  });
});

test('confirmHarnesses gives up after three unusable answers', async () => {
  // Abort after three invalid responses to avoid an infinite prompt loop.
  const p = prompts({ accept: false, answer: 'gemeni' });
  await withTTY(true, async () => {
    await assert.rejects(
      () =>
        initLib.confirmHarnesses(
          { ids: ['claude'], reason: 'detected' },
          facts(),
          { assumeYes: false, explicit: false },
          p,
        ),
      /could not read a harness list/,
    );
  });
  assert.strictEqual(p.asked.length, 4, 'one confirm + three retries');
});

test('add exit code separates an unknown skill from one already installed', async () => {
  // Distinguish missing skill names from skills that are already installed.
  const upstream = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-up-'));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-home-'));
  try {
    const skill = path.join(upstream, '.claude', 'skills', 'critique');
    fs.mkdirSync(skill, { recursive: true });
    fs.writeFileSync(path.join(skill, 'SKILL.md'), '# Critique\n');

    const run = (skills, dryRun = false) =>
      addLib.add({
        skills,
        upstreamDir: upstream,
        facts: facts({ homeDir: home }),
        harnesses: ['claude'],
        dryRun,
        force: false,
      });

    assert.strictEqual(await run(['critique']), 0, 'first install succeeds');
    assert.strictEqual(
      await run(['critique']),
      0,
      're-running add is idempotent, not a failure',
    );
    assert.strictEqual(
      await run(['critique', 'nope']),
      1,
      'an unknown skill fails even when another skill was already present',
    );
    assert.strictEqual(
      await run(['nope']),
      1,
      'an unknown skill on its own fails',
    );
    assert.strictEqual(
      await run(['nope'], true),
      1,
      'dry-run preserves the unknown-skill failure contract',
    );
  } finally {
    fs.rmSync(upstream, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('add success is per requested skill across chosen harnesses', async () => {
  const upstream = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-up-'));
  const homes = [];
  try {
    const claudeSkill = path.join(upstream, '.claude', 'skills', 'artifact');
    const codexSkill = path.join(upstream, '.codex', 'skills', 'critique');
    fs.mkdirSync(claudeSkill, { recursive: true });
    fs.mkdirSync(codexSkill, { recursive: true });
    fs.writeFileSync(path.join(claudeSkill, 'SKILL.md'), '# Artifact\n');
    fs.writeFileSync(path.join(codexSkill, 'SKILL.md'), '# Critique\n');

    for (const harnesses of [[], ['claude', 'codex']]) {
      const homeDir = fs.mkdtempSync(
        path.join(os.tmpdir(), 'activeloom-home-'),
      );
      homes.push(homeDir);
      const detected = facts({ homeDir });
      detected.harnesses[0].onMachine = true;
      detected.harnesses[1].onMachine = true;
      assert.strictEqual(
        await addLib.add({
          skills: ['artifact'],
          upstreamDir: upstream,
          facts: detected,
          harnesses,
          dryRun: false,
          force: false,
        }),
        0,
        harnesses.length === 0
          ? 'auto-detected harnesses succeed when any supplies the skill'
          : 'explicit harnesses use the same per-request success rule',
      );
      assert.strictEqual(
        fs.existsSync(
          path.join(homeDir, '.claude', 'skills', 'artifact', 'SKILL.md'),
        ),
        true,
      );
    }
  } finally {
    fs.rmSync(upstream, { recursive: true, force: true });
    for (const home of homes) fs.rmSync(home, { recursive: true, force: true });
  }
});

test('add preserves an existing skill when replacement copy fails', async () => {
  const upstream = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-up-'));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-home-'));
  const originalCopyFileSync = fs.copyFileSync;
  try {
    const upstreamSkill = path.join(upstream, '.claude', 'skills', 'critique');
    fs.mkdirSync(upstreamSkill, { recursive: true });
    fs.writeFileSync(path.join(upstreamSkill, 'SKILL.md'), '# New Critique\n');

    const destDir = path.join(home, '.claude', 'skills');
    const destSkill = path.join(destDir, 'critique');
    fs.mkdirSync(destSkill, { recursive: true });
    fs.writeFileSync(path.join(destSkill, 'SKILL.md'), '# Original Critique\n');

    fs.copyFileSync = () => {
      throw new Error('simulated ENOSPC mid-copy');
    };

    await assert.rejects(
      () =>
        addLib.add({
          skills: ['critique'],
          upstreamDir: upstream,
          facts: facts({ homeDir: home }),
          harnesses: ['claude'],
          dryRun: false,
          force: true,
        }),
      /simulated ENOSPC mid-copy/,
    );

    assert.strictEqual(
      fs.readFileSync(path.join(destSkill, 'SKILL.md'), 'utf8'),
      '# Original Critique\n',
      'existing skill must remain intact when copy fails',
    );

    const remaining = fs.readdirSync(destDir);
    assert.deepStrictEqual(
      remaining,
      ['critique'],
      'temporary staging directory must be cleaned up on failure',
    );
  } finally {
    fs.copyFileSync = originalCopyFileSync;
    fs.rmSync(upstream, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});

for (const restoreFails of [false, true]) {
  test(`add preserves the original skill when promotion fails and restoration ${restoreFails ? 'fails' : 'succeeds'}`, async () => {
    const upstream = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-up-'));
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-home-'));
    const originalRename = fs.renameSync;
    try {
      const upstreamSkill = path.join(
        upstream,
        '.claude',
        'skills',
        'critique',
      );
      const destDir = path.join(home, '.claude', 'skills');
      const destSkill = path.join(destDir, 'critique');
      for (const dir of [upstreamSkill, destSkill]) {
        fs.mkdirSync(dir, { recursive: true });
      }
      fs.writeFileSync(
        path.join(upstreamSkill, 'SKILL.md'),
        '# New Critique\n',
      );
      fs.writeFileSync(
        path.join(destSkill, 'SKILL.md'),
        '# Original Critique\n',
      );

      fs.renameSync = (from, to) => {
        if (to === destSkill && !path.basename(from).includes('-old-')) {
          if (restoreFails) {
            // Simulate destination existing between renames to test restore failure.
            fs.mkdirSync(destSkill);
            fs.writeFileSync(path.join(destSkill, 'concurrent.txt'), 'keep me');
          } else {
            throw new Error('simulated promotion failure');
          }
        }
        return originalRename(from, to);
      };

      let failure;
      await assert.rejects(
        () =>
          addLib.add({
            skills: ['critique'],
            upstreamDir: upstream,
            facts: facts({ homeDir: home }),
            harnesses: ['claude'],
            dryRun: false,
            force: true,
          }),
        (error) => {
          failure = error;
          return true;
        },
      );
      const remaining = fs.readdirSync(destDir);
      let originalDir = destSkill;
      if (restoreFails) {
        const backups = remaining.filter((name) =>
          name.startsWith('.tmp-critique-old-'),
        );
        assert.strictEqual(backups.length, 1);
        originalDir = path.join(destDir, backups[0]);
        assert.ok(
          failure.message.includes(
            `Original skill preserved at ${originalDir}`,
          ),
        );
        assert.strictEqual(
          fs.readFileSync(path.join(destSkill, 'concurrent.txt'), 'utf8'),
          'keep me',
        );
        assert.deepStrictEqual(
          remaining.sort(),
          [backups[0], 'critique'].sort(),
        );
      } else {
        assert.match(failure.message, /simulated promotion failure/);
        assert.deepStrictEqual(remaining, ['critique']);
      }
      assert.strictEqual(
        fs.readFileSync(path.join(originalDir, 'SKILL.md'), 'utf8'),
        '# Original Critique\n',
      );
    } finally {
      fs.renameSync = originalRename;
      fs.rmSync(upstream, { recursive: true, force: true });
      fs.rmSync(home, { recursive: true, force: true });
    }
  });
}

test('add replaces an existing skill when force is passed and cleans up temp directories', async () => {
  const upstream = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-up-'));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-home-'));
  try {
    const upstreamSkill = path.join(upstream, '.claude', 'skills', 'critique');
    fs.mkdirSync(upstreamSkill, { recursive: true });
    fs.writeFileSync(path.join(upstreamSkill, 'SKILL.md'), '# New Critique\n');

    const destDir = path.join(home, '.claude', 'skills');
    const destSkill = path.join(destDir, 'critique');
    fs.mkdirSync(destSkill, { recursive: true });
    fs.writeFileSync(path.join(destSkill, 'SKILL.md'), '# Original Critique\n');

    const code = await addLib.add({
      skills: ['critique'],
      upstreamDir: upstream,
      facts: facts({ homeDir: home }),
      harnesses: ['claude'],
      dryRun: false,
      force: true,
    });
    assert.strictEqual(code, 0);

    assert.strictEqual(
      fs.readFileSync(path.join(destSkill, 'SKILL.md'), 'utf8'),
      '# New Critique\n',
    );

    const remaining = fs.readdirSync(destDir);
    assert.deepStrictEqual(
      remaining,
      ['critique'],
      'temporary staging and backup directories must be cleaned up on success',
    );
  } finally {
    fs.rmSync(upstream, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('init keeps the existing config without asking which harnesses to write', async () => {
  // Existing configs are preserved without prompting for harness selection.
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-consumer-'));
  const originalConfirm = ui.confirm;
  const asked = [];
  ui.confirm = async (question) => {
    asked.push(question);
    return true;
  };
  try {
    fs.writeFileSync(
      path.join(repo, '.activeloom-config.yml'),
      'harnesses: [codex]\nsubstitutions:\n  PROJECT_NAME: kept\n',
    );
    await withTTY(true, async () => {
      assert.strictEqual(
        await initLib.init(initArgs(repo, { assumeYes: false })),
        0,
      );
    });
    assert.deepStrictEqual(asked, [], 'nothing should have been asked');
  } finally {
    ui.confirm = originalConfirm;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test('checkPython ignores a yaml.py in the working directory', () => {
  // Checked-in yaml.py in consumer directory must not shadow PyYAML.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-cwd-'));
  const marker = path.join(dir, 'ran');
  const previous = process.cwd();
  try {
    fs.writeFileSync(
      path.join(dir, 'yaml.py'),
      `open(${JSON.stringify(marker)}, "w").close()\nraise SystemExit(7)\n`,
    );
    process.chdir(dir);
    const result = initLib.checkPython('python3');
    assert.strictEqual(fs.existsSync(marker), false, 'yaml.py must not run');
    assert.strictEqual(result.ok, true, JSON.stringify(result));
  } finally {
    process.chdir(previous);
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
