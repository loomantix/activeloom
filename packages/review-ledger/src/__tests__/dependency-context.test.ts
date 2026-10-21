import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { execFileSync } from 'node:child_process';
import {
  classifyFiles,
  classifyRange,
  isDependencyPinChange,
  parseDiffPatch,
} from '../index.js';

const cases: [string, string, string, string, boolean][] = [
  [
    '.github/workflows/ci.yml',
    'jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n      - run: |\n          cat > config.yml <<EOF\n          image: node:22\n          EOF\n',
    'image: node:22',
    'image: node:23',
    false,
  ],
  [
    '.github/workflows/ci.yml',
    'jobs:\n  build:\n    steps:\n      - run: |\n          echo ready\n      - uses: actions/checkout@v4\n',
    '@v4',
    '@v5',
    true,
  ],
  [
    'Dockerfile',
    'FROM node:22\nCOPY <<EOF /app/template\nFROM example:1\nEOF\n',
    'example:1',
    'example:2',
    false,
  ],
  [
    'package.json',
    '{\n "dependencies": {\n "react": "18.2.0"\n },\n "scripts": {\n "test": "1.0.0"\n }\n}',
    '18.2.0',
    '18.3.1',
    true,
  ],
  [
    'package.json',
    '{\n "dependencies": {\n "react": "18.2.0"\n },\n "scripts": {\n "test": "1.0.0"\n }\n}',
    '1.0.0',
    '2.0.0',
    false,
  ],
  [
    'package.json',
    '{\n "config": {\n "port": "3000"\n }\n}',
    '3000',
    '4000',
    false,
  ],
  [
    'package.json',
    '{\n "packageManager": "pnpm@10.2.1"\n}',
    '10.2.1',
    '10.3.0',
    true,
  ],
  [
    'composer.json',
    '{\n "require": {\n "php": "^8.2"\n }\n}',
    '^8.2',
    '^8.3',
    true,
  ],
  [
    'pnpm-workspace.yaml',
    'catalog:\n  react: ^18.2.0',
    '^18.2.0',
    '^18.3.1',
    true,
  ],
  ['pnpm-workspace.yaml', 'settings:\n  port: 3000', '3000', '4000', false],
  [
    'Cargo.toml',
    '[dependencies]\nserde = { version = "1.0.210", features = ["derive"] }',
    '1.0.210',
    '1.0.211',
    true,
  ],
  [
    'Cargo.toml',
    '[dependencies]\nserde = { version = "1.0.210", features = ["derive"] }',
    '"1.0.210", features = ["derive"]',
    '"1.0.211", features = []',
    false,
  ],
  ['Cargo.toml', '[package.metadata]\nport = "3000"', '3000', '4000', false],
  [
    'pyproject.toml',
    '[tool.poetry.dependencies]\nhttpx = "^0.27.0"',
    '0.27.0',
    '0.28.0',
    true,
  ],
  ['requirements.txt', 'requests==2.32.2', '2.32.2', '2.32.3', true],
  ['go.mod', 'require example.com/lib/v2 v2.1.0', 'v2.1.0', 'v2.2.0', true],
  [
    '.github/workflows/ci.yml',
    'steps:\n  - uses: actions/checkout@v4 # v4',
    '@v4 # v4',
    '@v5 # v5',
    true,
  ],
  [
    'go.mod',
    'require golang.org/x/net v0.30.0 // indirect',
    'v0.30.0',
    'v0.31.0',
    true,
  ],
  ['pom.xml', '<version>3.3.4</version>', '3.3.4', '3.3.5', true],
  [
    'build.gradle.kts',
    'implementation("com.squareup.okio:okio:3.9.1")',
    '3.9.1',
    '3.9.2',
    true,
  ],
  ['Gemfile', "gem 'rails', '~> 7.2.1'", '7.2.1', '7.2.2', true],
  [
    '.github/workflows/ci.yml',
    'jobs:\n  build:\n    steps:\n      - uses: actions/checkout@' +
      'a'.repeat(40),
    'a'.repeat(40),
    'b'.repeat(40),
    true,
  ],
  [
    'Dockerfile',
    'FROM --platform=linux/amd64 node:22 AS build\nRUN npm test',
    'node:22',
    'node:23',
    true,
  ],
  [
    'Dockerfile',
    'FROM --platform=linux/amd64 node:22 AS build',
    'linux/amd64 node:22',
    'linux/arm64 node:23',
    false,
  ],
  [
    'Dockerfile',
    'FROM node:22 AS build',
    'node:22 AS build',
    'node:23 AS other',
    false,
  ],
  [
    'Dockerfile',
    'FROM node:22\nRUN npm test',
    'node:22\nRUN npm test',
    'node:23\nRUN npm publish',
    false,
  ],
  [
    '.github/workflows/ci.yml',
    'jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n      - run: npm test',
    '@v4\n      - run: npm test',
    '@v5\n      - run: npm publish',
    false,
  ],
  [
    'docker-compose.yml',
    'services:\n  db:\n    image: postgres:17.1',
    '17.1',
    '17.2',
    true,
  ],
  ['charts/app/values.yaml', 'image:\n  tag: "1.4.2"', '1.4.2', '1.4.3', true],
  [
    'docker-compose.yml',
    'services:\n  app:\n    image: node:22\n  db:\n    image: postgres:17',
    '    image: node:22\n  db:',
    '  db:\n    image: node:23',
    false,
  ],
];

describe('dependency pin context', () => {
  it('keeps renames and mode transitions on the review route', () => {
    for (const patch of [
      'diff --git a/src/main.js b/package-lock.json\nsimilarity index 100%\nrename from src/main.js\nrename to package-lock.json\n',
      'diff --git a/package-lock.json b/package-lock.json\nold mode 100644\nnew mode 100755\n',
      'diff --git a/vendor/lib b/vendor/renamed\nsimilarity index 100%\nrename from vendor/lib\nrename to vendor/renamed\nindex aaaaaaa..aaaaaaa 160000\n',
    ]) {
      const report = classifyFiles(parseDiffPatch(patch));
      expect(report.dependencyUpdate).toBe(false);
      expect(report.skip).toBe(false);
    }
  });
  it.each(cases)(
    '%s %s: %s -> %s, pin-only=%s',
    (path, beforeContent, from, to, expected) => {
      const afterContent = beforeContent.replace(from, to);
      const before = beforeContent.split('\n');
      const after = afterContent.split('\n');
      expect(
        isDependencyPinChange({
          path,
          beforeContent,
          afterContent,
          added: 1,
          deleted: 1,
          changedLines: [
            ...before.filter((line) => !after.includes(line)),
            ...after.filter((line) => !before.includes(line)),
          ],
        }),
      ).toBe(expected);
    },
  );
});

describe('pinned range dependency classification', () => {
  const original = process.cwd();
  let directory: string | undefined;
  afterEach(() => {
    process.chdir(original);
    if (directory) rmSync(directory, { recursive: true, force: true });
  });

  it('does not bypass review when Git detects a source-to-lockfile rename', () => {
    directory = mkdtempSync(join(tmpdir(), 'dependency-rename-'));
    process.chdir(directory);
    const git = (...args: string[]) =>
      execFileSync('git', args, {
        encoding: 'utf8',
        stdio: ['pipe', 'pipe', 'pipe'],
      }).trim();
    git('init');
    git('config', 'user.email', 'test@example.com');
    git('config', 'user.name', 'Test');
    writeFileSync('main.js', 'console.log("hello");\n');
    git('add', '.');
    git('-c', 'commit.gpgsign=false', 'commit', '-m', 'base');
    const base = git('rev-parse', 'HEAD');
    git('mv', 'main.js', 'package-lock.json');
    git('-c', 'commit.gpgsign=false', 'commit', '-m', 'rename');
    const report = classifyRange({ base, head: git('rev-parse', 'HEAD') });
    expect(report.dependencyUpdate).toBe(false);
    expect(report.skip).toBe(false);
  });

  it.each(cases)(
    'classifies committed context in %s: %s',
    (path, before, from, to, expected) => {
      directory = mkdtempSync(join(tmpdir(), 'dependency-range-'));
      process.chdir(directory);
      const git = (...args: string[]) =>
        execFileSync('git', args, {
          encoding: 'utf8',
          stdio: ['pipe', 'pipe', 'pipe'],
        }).trim();
      git('init');
      git('config', 'user.email', 'test@example.com');
      git('config', 'user.name', 'Test');
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, before);
      git('add', '.');
      git('-c', 'commit.gpgsign=false', 'commit', '-m', 'base');
      const base = git('rev-parse', 'HEAD');
      writeFileSync(path, before.replace(from, to));
      git('add', '.');
      git('-c', 'commit.gpgsign=false', 'commit', '-m', 'head');
      const head = git('rev-parse', 'HEAD');
      writeFileSync(path, 'uncommitted and irrelevant');
      expect(classifyRange({ base, head }).dependencyUpdate).toBe(expected);
    },
  );
});
