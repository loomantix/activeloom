'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const REPO_ROOT = path.resolve(__dirname, '..', '..');

test('the packed CLI exposes working help and tier commands', () => {
  const workdir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-pack-'));
  try {
    const output = execFileSync(
      'npm',
      [
        'pack',
        path.join(REPO_ROOT, 'cli'),
        '--pack-destination',
        workdir,
        '--json',
      ],
      { encoding: 'utf8' },
    );
    const [{ filename }] = JSON.parse(output);
    execFileSync('tar', ['-xzf', path.join(workdir, filename), '-C', workdir]);
    const packageDir = path.join(workdir, 'package');
    const manifest = JSON.parse(
      fs.readFileSync(path.join(packageDir, 'package.json'), 'utf8'),
    );
    assert.strictEqual(manifest.bin.activeloom, 'bin/activeloom.js');
    const executable = path.join(packageDir, manifest.bin.activeloom);
    const help = execFileSync(process.execPath, [executable, '--help'], {
      encoding: 'utf8',
    });
    const tiers = execFileSync(process.execPath, [executable, 'tiers'], {
      encoding: 'utf8',
    });
    assert.match(help, /npx activeloom init --sync/);
    assert.match(tiers, /Tier 2 is the recommended tier/);
  } finally {
    fs.rmSync(workdir, { recursive: true, force: true });
  }
});
