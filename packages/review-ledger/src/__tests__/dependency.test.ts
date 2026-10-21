import { describe, expect, it } from 'vitest';
import {
  classifyFiles,
  isDependencyPinChange,
  parseDiffPatch,
} from '../index.js';
import type { ChangedFile } from '../types.js';

const SHA = 'a'.repeat(40);
const DIGEST = `sha256:${'b'.repeat(64)}`;

function pin(path: string, changedLines: string[]): ChangedFile {
  return {
    path,
    added: changedLines.length,
    deleted: 0,
    blank: 0,
    changedLines,
  };
}

describe('isDependencyPinChange', () => {
  it.each([
    ['pnpm-lock.yaml', ['  lodash: 4.17.21', '  anything: at all']],
    ['apps/web/yarn.lock', ['whatever changed']],
    [
      'package.json',
      ['    "react": "^18.2.0",', '    "react": "^18.3.1",', '  },'],
    ],
    [
      'package.json',
      ['  "@scope/pkg": "~1.4.0"', '  "packageManager": "pnpm@10.2.1"'],
    ],
    [
      'package.json',
      ['    "internal": "workspace:*"', '    "shared": "catalog:"'],
    ],
    ['pnpm-workspace.yaml', ['  react: ^18.3.1', '  zod: 3.24.0']],
    ['pyproject.toml', ['    "httpx>=0.27,<0.29",', 'ruff = "^0.6.1"']],
    ['Cargo.toml', ['serde = { version = "1.0.210", features = ["derive"] }']],
    [
      'requirements.txt',
      ['requests==2.32.3 \\', '    --hash=sha256:abc123', '    # via app'],
    ],
    ['go.mod', ['\tgolang.org/x/net v0.30.0 // indirect', 'go 1.23']],
    ['pom.xml', ['<version>3.3.4</version>']],
    ['build.gradle.kts', ['implementation("com.squareup.okio:okio:3.9.1")']],
    ['Gemfile', ["gem 'rails', '~> 7.2.1'"]],
    [
      '.github/workflows/ci.yml',
      [`      - uses: actions/checkout@${SHA} # v4.2.2`],
    ],
    [
      '.github/actions/setup/action.yml',
      ['    uses: actions/setup-node@v4.1.0'],
    ],
    ['Dockerfile', [`FROM node:22.11-alpine@${DIGEST} AS build`]],
    [
      'services/api/Dockerfile.prod',
      ['FROM --platform=linux/amd64 python:3.13-slim'],
    ],
    ['docker-compose.yml', ['    image: postgres:17.2']],
    [
      'k8s/api/deployment.yaml',
      [`        image: ghcr.io/example/api:1.4.2@${DIGEST}`],
    ],
    ['charts/api/values.yaml', ['  tag: "1.4.2"']],
  ])('requires context for non-lockfile %s', (path, lines) => {
    // Line-only callers cannot establish dependency context.
    expect(isDependencyPinChange(pin(path, lines))).toBe(
      path.endsWith('lock') || path.endsWith('lock.yaml'),
    );
  });

  it.each([
    ['package.json', ['    "build": "tsc -p ."']],
    ['package.json', ['    "build:all": "npm:build:*"']],
    ['package.json', ['    "migrate": "2to3"', '    "debug": "v8-inspector"']],
    ['package.json', ['  "port": 3000']],
    ['package.json', ['  "version": "2.0.0"']],
    ['package.json', ['  "dependencies": {', '    "react": "^18.3.1"']],
    ['package.json', ['  {', '  }']],
    ['Cargo.toml', ['serde = { version = "1.0.210", path = "../serde" }']],
    [
      'Cargo.toml',
      [
        'serde = { version = "1.0.210", git = "https://example.com/serde.git" }',
      ],
    ],
    ['requirements.txt', ['# only a comment']],
    ['pnpm-workspace.yaml', ["  - 'apps/*'"]],
    ['.github/workflows/ci.yml', ['      - run: pnpm test']],
    ['.github/workflows/ci.yml', ['      - uses: actions/checkout@main']],
    [
      '.github/workflows/ci.yml',
      [`      - uses: actions/checkout@${SHA}`, '        with:'],
    ],
    ['Dockerfile', ['RUN apt-get update']],
    ['Dockerfile', ['FROM ${BASE_IMAGE}']],
    ['Dockerfile', ['FROM build']],
    ['docker-compose.yml', ['    image: false']],
    ['k8s/api/deployment.yaml', ['        replicas: 3']],
    ['src/config.yaml', ['  tag: "1.4.2"']],
    ['src/index.ts', ['import x from "y";']],
  ])('does not recognise %s with %j', (path, lines) => {
    expect(isDependencyPinChange(pin(path, lines))).toBe(false);
  });

  it('fails closed on a manifest with no line content', () => {
    expect(
      isDependencyPinChange({ path: 'package.json', added: 1, deleted: 1 }),
    ).toBe(false);
    expect(isDependencyPinChange(pin('package.json', ['', '  ']))).toBe(false);
  });

  it('treats a lockfile and a submodule gitlink as pins on their own', () => {
    expect(
      isDependencyPinChange({ path: 'go.sum', added: 40, deleted: 40 }),
    ).toBe(true);
    expect(
      isDependencyPinChange({
        path: 'vendor/lib',
        added: 1,
        deleted: 1,
        submodule: true,
      }),
    ).toBe(true);
  });
});

describe('classifyFiles dependency-update gate', () => {
  it('keeps an unverified manifest on the review route beside a lockfile', () => {
    const report = classifyFiles([
      pin('package.json', ['    "zod": "3.23.8"', '    "zod": "3.24.0"']),
      { path: 'pnpm-lock.yaml', added: 30, deleted: 30, blank: 0 },
    ]);
    expect(report.dependencyUpdate).toBe(false);
    expect(report.dependencyPaths).toEqual(['pnpm-lock.yaml']);
    expect(report.skip).toBe(false);
    expect(report.smallChange).toBe(false);
  });

  it('requires full context for an Actions pin bump', () => {
    const report = classifyFiles([
      pin('.github/workflows/ci.yml', [
        `      - uses: actions/checkout@${'c'.repeat(40)} # v4.2.1`,
        `      - uses: actions/checkout@${SHA} # v4.2.2`,
      ]),
    ]);
    expect(report.dependencyUpdate).toBe(false);
  });

  it('does not treat accompanying docs as missing Dockerfile context', () => {
    const report = classifyFiles([
      pin('Dockerfile', ['FROM node:22.10-alpine', 'FROM node:22.11-alpine']),
      { path: 'CHANGELOG.md', added: 2, deleted: 0, blank: 0 },
    ]);
    expect(report.dependencyUpdate).toBe(false);
    expect(report.dependencyPaths).toEqual([]);
  });

  it('reports a mixed range without routing it away from the tier', () => {
    const report = classifyFiles([
      pin('package.json', ['    "zod": "3.24.0"']),
      { path: 'pnpm-lock.yaml', added: 30, deleted: 30, blank: 0 },
      { path: 'src/schema.ts', added: 4, deleted: 2, blank: 0 },
    ]);
    expect(report.dependencyUpdate).toBe(false);
    expect(report.dependencyPaths).toEqual(['pnpm-lock.yaml']);
    expect(report.smallChange).toBe(false);
  });

  it('does not route a manifest with a non-version edit', () => {
    const report = classifyFiles([
      pin('package.json', ['    "zod": "3.24.0"', '    "lint": "eslint ."']),
    ]);
    expect(report.dependencyUpdate).toBe(false);
    expect(report.dependencyPaths).toEqual([]);
  });

  it('treats a manifest or lockfile under a test path as a fixture, not a pin', () => {
    const report = classifyFiles([
      pin('tests/fixtures/app/package.json', ['    "zod": "3.24.0"']),
      { path: 'tests/fixtures/app/pnpm-lock.yaml', added: 10, deleted: 10 },
    ]);
    expect(report.dependencyUpdate).toBe(false);
    expect(report.dependencyPaths).toEqual([]);
  });

  it('routes a submodule gitlink bump even under vendor/', () => {
    const report = classifyFiles([
      { path: 'vendor/lib', added: 1, deleted: 1, submodule: true },
    ]);
    expect(report.dependencyUpdate).toBe(true);
    expect(report.dependencyPaths).toEqual(['vendor/lib']);
    expect(report.skip).toBe(false);
  });

  it('never routes an empty or docs-only range', () => {
    expect(classifyFiles([]).dependencyUpdate).toBe(false);
    expect(
      classifyFiles([{ path: 'README.md', added: 1, deleted: 0 }])
        .dependencyUpdate,
    ).toBe(false);
  });

  it('reads pin lines from a unified diff', () => {
    const patch = [
      'diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml',
      '--- a/.github/workflows/ci.yml',
      '+++ b/.github/workflows/ci.yml',
      '@@ -10,3 +10,3 @@ jobs:',
      '     steps:',
      `-      - uses: actions/checkout@${'c'.repeat(40)} # v4.2.1`,
      `+      - uses: actions/checkout@${SHA} # v4.2.2`,
      '       - run: pnpm test',
    ].join('\n');
    const report = classifyFiles(parseDiffPatch(patch));
    expect(report.dependencyUpdate).toBe(false);

    const withStep = patch
      .replace(
        '       - run: pnpm test',
        '+      - run: pnpm lint\n       - run: pnpm test',
      )
      .replace('@@ -10,3 +10,3 @@', '@@ -10,3 +10,4 @@');
    expect(classifyFiles(parseDiffPatch(withStep)).dependencyUpdate).toBe(
      false,
    );
  });
});
