'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const {
  loadCompiler,
  verify,
} = require('../prompts/skills/simplify-comments/scripts/verify-typescript.cjs');

const modulePath =
  process.env.COMMENT_TEST_TYPESCRIPT ||
  path.resolve(__dirname, '../packages/review-ledger/node_modules/typescript');
const ts = loadCompiler(modulePath);
const helper = path.resolve(
  __dirname,
  '../prompts/skills/simplify-comments/scripts/comment-density.py',
);

const accepted = [
  ['ordinary.ts', '// history\nconst x = 1;\n', 'const x = 1;\n'],
  [
    'view.tsx',
    '// history\nconst x = <p title="//literal"> hi {/* history */} there </p>;',
    'const x = <p title="//literal"> hi {/**/} there </p>;',
  ],
  [
    'view.jsx',
    '// history\nconst x = <> hi //literal </>;',
    'const x = <> hi //literal </>;',
  ],
  [
    'regex.js',
    'if (ready) /a\\/b/.test(s); // why\n',
    'if (ready) /a\\/b/.test(s);\n',
  ],
  [
    'template.ts',
    '// history\nconst s = tag`x${1 + 2} /* literal */`;',
    'const s = tag`x${1 + 2} /* literal */`;',
  ],
  ['nul.ts', '// history\nconst s = "a\0b";', 'const s = "a\0b";'],
  ['unicode.js', '// history\u2028const x = 1;', 'const x = 1;'],
  ['format.ts', 'const x=f(1, 2); // history\n', 'const x = f(\n  1, 2\n);\n'],
  [
    'doc.ts',
    '/** Long narrative. */\nexport const x = 1;',
    '/** Summary. */\nexport const x = 1;',
  ],
  [
    'block.ts',
    '// history\n\n// Copyright Example Authors\n// All rights reserved.\nconst x=1;',
    '// Copyright Example Authors\n// All rights reserved.\nconst x=1;',
  ],
  [
    'header.ts',
    '/* history */\n// @ts-nocheck\nconst x=1;',
    '// @ts-nocheck\nconst x=1;',
  ],
];
for (const [file, before, after] of accepted) {
  test(`accepts comment reduction: ${file}`, () =>
    assert.equal(verify(ts, file, before, after).status, 'unchanged'));
}

const rejected = [
  ['value.ts', 'const x = 1;', 'const x = 2;'],
  ['type.ts', 'let x: number;', 'let x: string;'],
  ['literal.ts', 'const s = "//one";', 'const s = "//two";'],
  ['asi.js', 'function f(){ return\n1; }', 'function f(){ return 1; }'],
  ['postfix.js', 'let a=1,b=2; a\n++b;', 'let a=1,b=2; a++\nb;'],
  ['regex.js', 'if (ready) /a/.test(s);', 'if (ready) /b/.test(s);'],
  ['division.js', 'const x = a / b / c;', 'const x = a / b * c;'],
  ['view.tsx', 'const x=<p>hello // literal</p>;', 'const x=<p>hello</p>;'],
  ['space.jsx', 'const x=<p>a b</p>;', 'const x=<p>a  b</p>;'],
  ['newline.jsx', 'const x=<p>a\nb</p>;', 'const x=<p>ab</p>;'],
  ['attribute.tsx', 'const x=<p title="a  b"/>;', 'const x=<p title="a b"/>;'],
  ['raw.ts', 'const s = tag`\\n`;', 'const s = tag`\n`;'],
  ['nul.ts', 'const s = "a\0b";', 'const s = "ab";'],
  ['ignore.ts', '// @ts-ignore\nf();', 'f();'],
  ['global.js', '/* global x */\nx();', 'x();'],
  ['exported.js', '/* exported x */\nconst x=1;', 'const x=1;'],
  ['move.ts', '// @ts-ignore\nf();\ng();', 'f();\n// @ts-ignore\ng();'],
  ['gap.ts', '// @ts-ignore\nf();', '// @ts-ignore\n\nf();'],
  ['pure.js', 'const x = /*#__PURE__*/ f();', 'const x = f();'],
  [
    'webpack.js',
    'import(/* webpackChunkName: "a" */ "a");',
    'import(/* webpackChunkName: "b" */ "a");',
  ],
  [
    'doc.js',
    '/** @param {number} x */\nfunction f(x) {}',
    '/** @param {string} x */\nfunction f(x) {}',
  ],
  [
    'example.ts',
    '/** @example f(1) */\nfunction f(x) {}',
    '/** @example f(2) */\nfunction f(x) {}',
  ],
  ['license.ts', '// SPDX-License-Identifier: MIT\nconst x=1;', 'const x=1;'],
  ['copyright.ts', '// Copyright Example Authors\nconst x=1;', 'const x=1;'],
  ['bang.ts', '/*! Legal */\nconst x=1;', 'const x=1;'],
  ['shebang.js', '#!/usr/bin/env node\nf();', 'f();'],
  ['reference.ts', '/// <reference path="a.d.ts" />\nconst x=1;', 'const x=1;'],
  [
    'legal-continuation.ts',
    '// Copyright Example Authors\n// All rights reserved.\nconst x=1;',
    '// Copyright Example Authors\nconst x=1;',
  ],
  [
    'legal-lead.ts',
    '// Part of Example.\n// Released under an open licence.\nconst x=1;',
    '// Released under an open licence.\nconst x=1;',
  ],
  [
    'example-continuation.ts',
    '// @example\n// f(1)\nfunction f(x) {}',
    '// @example\nfunction f(x) {}',
  ],
  [
    'directive-reason.ts',
    'f(); // eslint-disable-line no-console --\n     // reason continues\ng();',
    'f(); // eslint-disable-line no-console --\ng();',
  ],
  [
    'trailing.js',
    'f(); // eslint-disable-line\ng();',
    'f();\n// eslint-disable-line\ng();',
  ],
  [
    'biome-gap.ts',
    '// biome-ignore lint: reason\nf();',
    '// biome-ignore lint: reason\n\nf();',
  ],
  ['flow-gap.js', '// $FlowFixMe\nf();', '// $FlowFixMe\n\nf();'],
];

for (const [file, before, after] of rejected) {
  test(`rejects semantic mutation: ${file}`, () =>
    assert.equal(verify(ts, file, before, after).status, 'changed'));
}
for (const comment of [
  '// eslint-disable-next-line no-console',
  '// prettier-ignore',
  '// biome-ignore lint: reason',
  '// oxlint-disable',
  '/* istanbul ignore next */',
  '/* c8 ignore next */',
  '/* v8 ignore next */',
  '// SAFETY: invariant',
  '/* globals a */',
  '/* jshint esversion: 6 */',
  '// deno-lint-ignore no-explicit-any',
  '// deno-fmt-ignore',
  '// dprint-ignore',
  '// cspell:disable-next-line',
  '/* stylelint-disable */',
  '// Stryker disable next-line all',
  '// NOSONAR',
  '// nosemgrep',
  '// lgtm[js/example]',
  '// codeql[js/example]',
  '// gitleaks:allow',
  '// noinspection JSUnusedGlobalSymbols',
  '// tslint:disable',
  '// $FlowFixMe',
  '// Licensed under an example licence',
  '// All rights reserved.',
  '// Permission is hereby granted, free of charge',
  '//# sourceMappingURL=a.js.map',
]) {
  test(`rejects deleting protected comment: ${comment}`, () =>
    assert.equal(
      verify(ts, 'protected.js', `${comment}\nf();`, 'f();').status,
      'changed',
    ));
}
for (const text of [
  'const x = ;',
  'const s = "unterminated',
  'const x=<p>broken',
  'const x = 1; /* unclosed',
  '\0',
]) {
  test(`malformed source fails closed: ${JSON.stringify(text)}`, () =>
    assert.equal(verify(ts, 'broken.tsx', text, text).status, 'error'));
}
test('a rejection locates the first difference without quoting source', () => {
  assert.deepEqual(
    verify(ts, 'a.ts', 'const a=1;\n// @ts-ignore\nf();', 'const a=1;\nf();'),
    {
      status: 'changed',
      detail: 'trivia differ (first difference: baseline line 2, edited none)',
      verifier: 'typescript-5.9.3',
    },
  );
  assert.equal(
    verify(ts, 'a.ts', 'const a=1;\nconst b=2;', 'const a=1;\n\nconst b=3;')
      .detail,
    'tokens differ (first difference: baseline line 2, edited line 3)',
  );
  assert.match(
    verify(ts, 'a.ts', 'const x=1;', 'const x = ;').detail,
    /^edited: parse diagnostic TS\d+ at offset \d+$/,
  );
  assert.match(
    verify(ts, 'a.ts', 'const x = ;', 'const x=1;').detail,
    /^baseline: parse diagnostic/,
  );
});
test('unsupported compiler and missing module fail closed', () => {
  assert.throws(
    () => loadCompiler('/missing/compiler'),
    /cannot load TypeScript/,
  );
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'comment-compiler-'));
  try {
    fs.writeFileSync(
      path.join(dir, 'index.js'),
      'module.exports = {version: "0.0.0"};',
    );
    assert.throws(() => loadCompiler(dir), /unsupported TypeScript version/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
test('Python CLI certifies parser-backed edits, NUL literals and fails on missing dependencies', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'comment-verify-'));
  function git(...args) {
    const result = spawnSync(
      'git',
      [
        '-c',
        'user.name=Test',
        '-c',
        'user.email=test@example.invalid',
        '-c',
        'commit.gpgsign=false',
        ...args,
      ],
      { cwd: dir, encoding: 'utf8' },
    );
    assert.equal(result.status, 0, result.stderr);
  }
  function run(compiler) {
    const result = spawnSync(
      'python3',
      [
        helper,
        '--verify-against',
        'HEAD',
        '--typescript',
        compiler,
        '--json',
        'view.tsx',
        'nul.ts',
      ],
      { cwd: dir, encoding: 'utf8' },
    );
    return [result.status, JSON.parse(result.stdout)];
  }
  try {
    fs.writeFileSync(
      path.join(dir, 'view.tsx'),
      '// history\nconst x=<p>text //literal</p>;',
    );
    fs.writeFileSync(path.join(dir, 'nul.ts'), '// history\nconst s="a\0b";');
    git('init', '-q');
    git('add', '.');
    git('commit', '-qm', 'baseline');
    fs.writeFileSync(
      path.join(dir, 'view.tsx'),
      'const x=<p>text //literal</p>;',
    );
    fs.writeFileSync(path.join(dir, 'nul.ts'), 'const s="a\0b";');
    const [status, report] = run(modulePath);
    assert.equal(status, 0);
    assert.deepEqual(report.statuses, { unchanged: 2 });
    assert.ok(
      report.files.every(
        (f) => f.verifier === 'typescript-5.9.3' && f.density_approximate,
      ),
    );
    const [missingStatus, missing] = run('/missing/compiler');
    assert.equal(missingStatus, 1);
    assert.deepEqual(missing.statuses, { error: 2 });
    fs.writeFileSync(path.join(dir, 'nul.ts'), 'const s="ab";');
    assert.equal(run(modulePath)[0], 1);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
