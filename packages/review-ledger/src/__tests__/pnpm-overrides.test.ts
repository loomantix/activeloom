import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { classifyFiles, classifyRange } from '../index.js';

const manifest = (pnpm: unknown) => JSON.stringify({ pnpm }, null, 2);
const overrides = (value: unknown, key = 'example-lib') => ({
  overrides: { [key]: value },
});
const file = (beforeContent: string, afterContent: string) => ({
  path: 'package.json',
  added: 1,
  deleted: 1,
  blank: 0,
  beforeContent,
  afterContent,
  changedLines: [
    ...beforeContent
      .split('\n')
      .filter((line) => !afterContent.split('\n').includes(line)),
    ...afterContent
      .split('\n')
      .filter((line) => !beforeContent.split('\n').includes(line)),
  ],
});
const lockfile = { path: 'pnpm-lock.yaml', added: 1, deleted: 1, blank: 0 };

describe('package.json pnpm overrides', () => {
  it.each(['example-lib', '@example/lib'])(
    'recognizes existing %s version pins',
    (key) => {
      const report = classifyFiles([
        file(
          manifest(overrides('^1.2.0', key)),
          manifest(overrides('^1.2.1', key)),
        ),
        lockfile,
      ]);
      expect(report.dependencyUpdate).toBe(true);
      expect(report.dependencyPaths).toEqual([
        'package.json',
        'pnpm-lock.yaml',
      ]);
    },
  );

  it.each([
    [
      'other configuration',
      { ...overrides('1.0.0'), config: '1.0.0' },
      { ...overrides('1.0.1'), config: '2.0.0' },
    ],
    ['renamed key', overrides('1.0.0'), overrides('1.0.1', 'other-lib')],
    [
      'changed selector',
      overrides('1.0.0', 'example-lib@1'),
      overrides('1.0.1', 'example-lib@2'),
    ],
    [
      'unchanged selector',
      overrides('1.0.0', 'example-lib@1'),
      overrides('1.0.1', 'example-lib@1'),
    ],
    [
      'selector value',
      overrides('1.0.0', 'parent>example-lib'),
      overrides('1.0.1', 'parent>example-lib'),
    ],
    ['added entry', { overrides: {} }, overrides('1.0.1')],
    ['removed entry', overrides('1.0.0'), { overrides: {} }],
    ['array', { overrides: ['1.0.0'] }, { overrides: ['1.0.1'] }],
    ['non-object pnpm', '1.0.0', '1.0.1'],
    ...[
      null,
      2,
      ['1.0.1'],
      { version: '1.0.1' },
      'latest',
      '-',
      '$example-lib',
      'file:../lib',
      'npm:other-lib@1.0.1',
      'workspace:^1.0.1',
      '1.0.1 garbage',
    ].map((value): [string, unknown, unknown] => [
      'unsupported value',
      overrides('1.0.0'),
      overrides(value),
    ]),
  ])('keeps %s on the mixed route', (_label, before, after) => {
    const report = classifyFiles([
      file(manifest(before), manifest(after)),
      lockfile,
    ]);
    expect(report.dependencyUpdate).toBe(false);
    expect(report.dependencyPaths).toEqual(['pnpm-lock.yaml']);
  });

  it('rejects malformed JSON', () => {
    const report = classifyFiles([
      file(manifest(overrides('1.0.0')), manifest(overrides('1.0.1')) + ','),
      lockfile,
    ]);
    expect(report.dependencyUpdate).toBe(false);
    expect(report.dependencyPaths).toEqual(['pnpm-lock.yaml']);
  });

  it.each(['src/app.ts', 'tests/app.test.ts'])(
    'keeps %s changes mixed',
    (path) => {
      const report = classifyFiles([
        file(manifest(overrides('1.0.0')), manifest(overrides('1.0.1'))),
        lockfile,
        { path, added: 1, deleted: 1, blank: 0 },
      ]);
      expect(report.dependencyUpdate).toBe(false);
      expect(report.dependencyPaths).toEqual([
        'package.json',
        'pnpm-lock.yaml',
      ]);
    },
  );
});

describe('pinned pnpm override context', () => {
  const original = process.cwd();
  let directory: string | undefined;
  afterEach(() => {
    process.chdir(original);
    if (directory) rmSync(directory, { recursive: true, force: true });
  });

  it('extracts the root override and dependency bump from committed content', () => {
    directory = mkdtempSync(join(tmpdir(), 'pnpm-overrides-'));
    process.chdir(directory);
    const git = (...args: string[]) =>
      execFileSync('git', args, {
        encoding: 'utf8',
        stdio: ['pipe', 'pipe', 'pipe'],
      }).trim();
    git('init');
    git('config', 'user.name', 'Test');
    git('config', 'user.email', 'test@example.invalid');
    const commit = (version: string) => {
      writeFileSync(
        'package.json',
        JSON.stringify(
          {
            dependencies: { 'example-lib': version },
            pnpm: overrides(version),
          },
          null,
          2,
        ),
      );
      writeFileSync(
        'pnpm-lock.yaml',
        `lockfileVersion: '9.0'\noverrides:\n  example-lib: ${version}\n`,
      );
      git('add', '.');
      git('-c', 'commit.gpgsign=false', 'commit', '-m', version);
      return git('rev-parse', 'HEAD');
    };
    const base = commit('1.0.0');
    const head = commit('1.0.1');
    writeFileSync('package.json', 'uncommitted and irrelevant');
    const report = classifyRange({ base, head });
    expect(report.dependencyUpdate).toBe(true);
    expect(report.dependencyPaths).toEqual(['package.json', 'pnpm-lock.yaml']);
  });
});
