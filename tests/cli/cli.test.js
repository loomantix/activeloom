'use strict';

/**
 * Unit coverage for the CLI's decisions: argument parsing, tier resolution,
 * harness selection, config rendering, and the Tier 0 placeholder guard.
 *
 * The end-to-end behaviour of `init` is proved in
 * `tests/test_cli_init_equivalence.py`, which runs the real binary against the
 * real engine. What is left for here is the logic that decides *what* to ask
 * the engine for — the part where a wrong answer produces a plausible-looking
 * config rather than a crash.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const { parseArgs } = require(
  path.join(REPO_ROOT, 'cli', 'bin', 'activeloom.js'),
);
const { TIERS, resolveTier } = require(
  path.join(REPO_ROOT, 'cli', 'lib', 'tiers.js'),
);
const initLib = require(path.join(REPO_ROOT, 'cli', 'lib', 'init.js'));
const addLib = require(path.join(REPO_ROOT, 'cli', 'lib', 'add.js'));

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

// --- argument parsing -------------------------------------------------------

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
  // A mistyped flag that parsed as "no flags" would report success having
  // skipped the behaviour asked for — the failure this rejection prevents.
  assert.throws(() => parseArgs(['init', '--forse']), /unknown option --forse/);
});

test('parseArgs rejects a value-taking flag with no value', () => {
  assert.throws(() => parseArgs(['init', '--ref']), /--ref needs a value/);
});

// --- tiers ------------------------------------------------------------------

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
  // An App identity with nothing to sign is not a coherent request; silently
  // producing a tier-1 tree would be the wrong resolution of it.
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

// --- harness selection ------------------------------------------------------

test('init prefers harnesses already checked into the repo', () => {
  const f = facts();
  f.harnesses[1].inRepo = true; // codex in repo
  f.harnesses[0].cliInstalled = true; // claude only on this machine
  const chosen = initLib.chooseHarnesses(f, []);
  // The config is committed and governs every teammate's sync, so a harness
  // the team checked in outranks one that happens to be on this laptop.
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

// --- config rendering -------------------------------------------------------

test('the generated config never declares the reserved telemetry key', () => {
  // `REVIEW_TELEMETRY_ENV` is computed by the engine from the `telemetry:`
  // block, and a consumer that declares it is rejected outright — so emitting
  // it would produce a config that fails on its very first sync.
  const body = initLib.renderConfig({ harnesses: ['claude'], facts: facts() });
  assert.ok(!body.includes('REVIEW_TELEMETRY_ENV'));
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
  // No format script was detected, so none is claimed.
  assert.ok(!body.includes('run format'));
});

test('config scalars are quoted so punctuation cannot restructure the YAML', () => {
  assert.strictEqual(initLib.yamlScalar("it's"), "'it''s'");
  assert.strictEqual(initLib.yamlScalar('a: b #c'), "'a: b #c'");
});

// --- tier 0 guard -----------------------------------------------------------

test('add refuses a skill tree containing an unsubstituted placeholder', () => {
  // Installing one would put a literal `<<KEY>>` in front of a model, which
  // reads as an instruction it cannot satisfy — and would stay invisible until
  // a review went wrong.
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
  // The manifest ships `skills/issues/scripts/ready.py` as 0755 and it is
  // invoked directly; a copy that flattened modes would install a skill that
  // silently cannot run its own helper.
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

test('add chooses harnesses from machine evidence, not repo evidence', () => {
  // Tier 0 installs into the user's own config directory, so what this repo
  // contains is irrelevant to it — the mirror image of `init`'s rule.
  const f = facts();
  f.harnesses[0].inRepo = true;
  f.harnesses[1].onMachine = true;
  assert.deepStrictEqual(addLib.chooseHarnesses(f, []).ids, ['codex']);
});

// --- self-sync guard --------------------------------------------------------

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

// --- harness confirmation ---------------------------------------------------

/**
 * Force the TTY branch on.
 *
 * `confirmHarnesses` short-circuits when stdin is not a TTY, which is exactly
 * what a test runner gives it — so without this the interactive assertions
 * below would pass by never running the code they name.
 */
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
  // Scripts, CI, and `npx | sh` pipelines must never block on a question
  // nobody can answer.
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
  // Re-asking second-guesses a decision the user just typed.
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
  // The case the whole prompt exists for: three CLIs installed, one actually
  // used, and without this all three trees get committed to a shared repo.
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
  // So the generated config is byte-stable regardless of how it was entered.
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
  // Bounded rather than looping forever: a user who cannot name a valid
  // harness is better served by the error and `--harness` than by a prompt
  // they have to Ctrl-C out of.
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
