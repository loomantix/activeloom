'use strict';

/**
 * `review-config` hands its arguments to the review-profile helper verbatim and
 * reports the helper's exit status. The helper's own contract is covered by
 * `tests/test_review_profile.py`; this file covers the CLI door onto it.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const { parseArgs, validateCommandArgs } = require(
  path.join(REPO_ROOT, 'cli', 'bin', 'activeloom.js'),
);
const { reviewConfig, HELPER } = require(
  path.join(REPO_ROOT, 'cli', 'lib', 'review-config.js'),
);

test('arguments after review-config pass through untouched', () => {
  const argv = [
    '--python',
    'python3.12',
    'review-config',
    'set',
    '--repo',
    'example/project',
    'claude.effort=high',
  ];
  const opts = parseArgs(argv);
  assert.strictEqual(opts.command, 'review-config');
  assert.strictEqual(opts.python, 'python3.12');
  assert.deepStrictEqual(opts.positionals, [
    'set',
    '--repo',
    'example/project',
    'claude.effort=high',
  ]);
  assert.strictEqual(validateCommandArgs(opts, argv), null);
});

test('options that review-config cannot apply are still rejected', () => {
  const argv = ['--sync', 'review-config', 'show'];
  const opts = parseArgs(argv);
  assert.match(validateCommandArgs(opts, argv), /does not accept --sync/);
});

test('the helper receives the arguments and its exit status is returned', () => {
  const calls = [];
  const status = reviewConfig({
    upstreamDir: REPO_ROOT,
    python: 'python3',
    helperArgs: ['order', '--tier', 'deep'],
    spawn: (file, args, options) => {
      calls.push({ file, args, options });
      return { status: 3 };
    },
  });
  assert.strictEqual(status, 3);
  assert.deepStrictEqual(calls[0].args, [
    '-I',
    path.join(REPO_ROOT, HELPER),
    'order',
    '--tier',
    'deep',
  ]);
  assert.deepStrictEqual(calls[0].options, { stdio: 'inherit' });
});

test('with no arguments the helper prints its usage', () => {
  let received;
  reviewConfig({
    upstreamDir: REPO_ROOT,
    python: 'python3',
    helperArgs: [],
    spawn: (file, args) => {
      received = args;
      return { status: 0 };
    },
  });
  assert.deepStrictEqual(received.slice(2), ['--help']);
});

test('an upstream without the helper is reported, not silently skipped', () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-upstream-'));
  try {
    assert.throws(
      () =>
        reviewConfig({ upstreamDir: empty, python: 'python3', helperArgs: [] }),
      /does not ship/,
    );
  } finally {
    fs.rmSync(empty, { recursive: true, force: true });
  }
});

test('the real helper refuses to resolve settings before setup', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-profile-'));
  const previous = process.env.ACTIVELOOM_REVIEW_PROFILE;
  process.env.ACTIVELOOM_REVIEW_PROFILE = path.join(
    home,
    'review-profile.json',
  );
  try {
    const status = reviewConfig({
      upstreamDir: REPO_ROOT,
      python: 'python3',
      helperArgs: ['resolve', '--engine', 'claude'],
      spawn: (file, args) => {
        const env = { ...process.env };
        delete env.ACTIVELOOM_REVIEW_MODEL;
        delete env.ACTIVELOOM_REVIEW_EFFORT;
        return require('node:child_process').spawnSync(file, args, {
          encoding: 'utf8',
          env,
        });
      },
    });
    assert.strictEqual(status, 3);
  } finally {
    if (previous === undefined) delete process.env.ACTIVELOOM_REVIEW_PROFILE;
    else process.env.ACTIVELOOM_REVIEW_PROFILE = previous;
    fs.rmSync(home, { recursive: true, force: true });
  }
});
