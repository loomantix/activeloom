import { afterEach, describe, expect, it } from 'vitest';
import { classifyRangeEffect } from '../effect.js';
import { resetGitHubRunner, setGitHubRunner } from '../github.js';
import type { GitHubRunner } from '../types.js';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BEFORE = 'a'.repeat(40);
const AFTER = 'b'.repeat(40);

/**
 * Drive `classifyRangeEffect` against a fixed `git diff` transcript.
 *
 * `nameStatus` is the `--name-status` body; `patches` maps a path to the
 * `--unified=0` patch the second call returns for it.
 */
function withDiff(
  nameStatus: string,
  patches: Record<string, string> = {},
  sources: Record<string, string> = {},
): void {
  const runner: GitHubRunner = {
    runGh() {
      throw new Error('unexpected gh call');
    },
    runGit(args: string[]): string {
      if (args[0] === 'show') {
        const source = sources[args[1]!];
        if (source === undefined) throw new Error('missing source blob');
        return source;
      }
      if (args.includes('--summary')) return '';
      if (args.includes('--name-status')) {
        return `${nameStatus}\n`;
      }
      const path = args[args.length - 1]!;
      return patches[path] ?? '';
    },
  };
  setGitHubRunner(runner);
}

afterEach(() => {
  resetGitHubRunner();
});

describe('classifyRangeEffect', () => {
  it('accepts a comment-only Terraform range', () => {
    // The reported failure: five findings fixed, every changed line a comment,
    // and the plan still reporting no changes.
    withDiff('M\tinfra/eks/main.tf', {
      'infra/eks/main.tf': [
        '@@ -3 +3 @@',
        '-# the node floor is 3',
        '+# the node floor is 2',
        '@@ -11 +11 @@',
        '-// applied by CI',
        '+// hand-applied by an admin',
      ].join('\n'),
    });
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('non-behavioral');
  });

  it('rejects a range that also changes a non-comment line', () => {
    withDiff('M\tinfra/eks/main.tf', {
      'infra/eks/main.tf': [
        '@@ -3 +3 @@',
        '-# the node floor is 3',
        '+# the node floor is 2',
        '@@ -8 +8 @@',
        '-  min_size = 3',
        '+  min_size = 2',
      ].join('\n'),
    });
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
  });

  it('accepts a test-only range', () => {
    withDiff('M\tpackages/app/src/__tests__/user.test.ts');
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('non-behavioral');
  });

  it('rejects a range that touches a test and app code', () => {
    withDiff(
      [
        'M\tpackages/app/src/__tests__/user.test.ts',
        'M\tpackages/app/src/user.ts',
      ].join('\n'),
      {
        'packages/app/src/user.ts': [
          '@@ -4 +4 @@',
          '-  return name.trim();',
          '+  return name.trim().toLowerCase();',
        ].join('\n'),
      },
    );
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
  });

  it('accepts a docs range but not a prompt-surface one', () => {
    withDiff('M\tdocs/how-to/deploy.md');
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('non-behavioral');

    // `.claude/**` is source whatever the extension: the model reads it as
    // instructions, so editing it changes behavior.
    withDiff('M\t.claude/skills/critique/SKILL.md');
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
  });

  it('treats every engine prompt directory as source, not just its own', () => {
    // This helper is vendored into each engine repo, so a rule naming only the
    // reading engine would make the same SKILL edit inert in the other two.
    for (const path of [
      '.claude/skills/critique/SKILL.md',
      '.codex/skills/critique/SKILL.md',
      '.agents/skills/critique/SKILL.md',
      '.codex/references/local-review-ledger.md',
    ]) {
      withDiff(`M\t${path}`);
      expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
    }
  });

  it('treats executing config as source whatever extension it wears', () => {
    // A workflow is `.yml` and a manifest is `.json`. Reading them as inert
    // config would let a rewritten pipeline or a bumped dependency attest
    // `minor`, so the other engine never re-reads the new head.
    for (const path of [
      '.github/workflows/ci.yml',
      '.github/actions/setup/action.yml',
      '.github/CODEOWNERS',
      'package.json',
      'pnpm-lock.yaml',
      'package-lock.json',
    ]) {
      withDiff(`M\t${path}`);
      expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
    }

    // Deleting one is not inert either.
    withDiff('D\t.github/workflows/publish.yml');
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
  });

  it('still reads a script under docs/ with the comment prover', () => {
    // A `docs/` segment is not a blanket answer for something that executes.
    withDiff('M\tdocs/scripts/bootstrap.sh', {
      'docs/scripts/bootstrap.sh': [
        '@@ -4 +4 @@',
        '-rm -rf "$target"',
        '+rm -rf "$target" "$cache"',
      ].join('\n'),
    });
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');

    // ...and a comment-only edit to that same script is still inert.
    withDiff('M\tdocs/scripts/bootstrap.sh', {
      'docs/scripts/bootstrap.sh': [
        '@@ -4 +4 @@',
        '-# clears the build cache',
        '+# clears the build cache and the target dir',
      ].join('\n'),
    });
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('non-behavioral');
  });

  it('treats a marker hidden in a string as code', () => {
    withDiff('M\tscripts/deploy.sh', {
      'scripts/deploy.sh': [
        '@@ -2 +2 @@',
        '-echo "# starting"',
        '+echo "# starting deploy"',
      ].join('\n'),
    });
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
  });

  it('accepts a balanced block comment and rejects code sharing its line', () => {
    withDiff(
      'M\tsrc/user.ts',
      {
        'src/user.ts': [
          '@@ -1 +1 @@',
          '-/* old note */',
          '+/* new note */',
        ].join('\n'),
      },
      {
        [`${BEFORE}:src/user.ts`]: '/* old note */',
        [`${AFTER}:src/user.ts`]: '/* new note */',
      },
    );
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('non-behavioral');

    withDiff('M\tsrc/user.ts', {
      'src/user.ts': [
        '@@ -1 +1 @@',
        '-/* note */ const a = 1;',
        '+/* note */ const a = 2;',
      ].join('\n'),
    });
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
  });

  it.each([
    [
      'JSDoc interior',
      '/**\n * Existing guard.\n */\nexport const identity = (value: string) => value;\n',
      '/**\n * The existing guard preserves input.\n */\nexport const identity = (value: string) => value;\n',
      'non-behavioral',
    ],
    [
      'multiline prose',
      '/* Old prose. */\nconst value = 1;',
      '/* New prose. */\nconst value = 1;',
      'non-behavioral',
    ],
    [
      'template contents',
      'const value = `\n/* Old prose. */\n`;',
      'const value = `\n/* New prose. */\n`;',
      'behavioral',
    ],
    [
      'template interpolation',
      'const value = `text ${1}`;',
      'const value = `text ${2}`;',
      'behavioral',
    ],
    [
      'regular expression',
      'const value = /old\\/*/;',
      'const value = /new\\/*/;',
      'behavioral',
    ],
    [
      'multiplication',
      'const value = 1\n * 2;',
      'const value = 1\n * 3;',
      'behavioral',
    ],
    [
      'runtime edit beside comment',
      '/* Prose. */ const value = 1;',
      '/* New prose. */ const value = 2;',
      'behavioral',
    ],
    [
      'semicolon insertion',
      'function value() { return /* prose */ 1; }',
      'function value() { return /* prose\n */ 1; }',
      'behavioral',
    ],
    [
      'type directive',
      '// @ts-check\nconst value = 1;',
      '// @ts-nocheck\nconst value = 1;',
      'behavioral',
    ],
    [
      'JSDoc type',
      '/** @type {string} */\nlet value;',
      '/** @type {number} */\nlet value;',
      'behavioral',
    ],
    [
      'coverage directive',
      '/* istanbul ignore next */\nconst value = 1;',
      '/* prose */\nconst value = 1;',
      'behavioral',
    ],
    [
      'unclosed comment',
      '/* prose */\nconst value = 1;',
      '/* prose\nconst value = 1;',
      'behavioral',
    ],
  ])('reads full lexical context for %s', (_label, before, after, expected) => {
    withDiff(
      'M\tsrc/identity.ts',
      {},
      {
        [`${BEFORE}:src/identity.ts`]: before,
        [`${AFTER}:src/identity.ts`]: after,
      },
    );
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe(expected);
  });

  it.each([
    [
      'inline prose insertion',
      'export const value = 1;',
      'export /* Preserve the value. */ const value = 1;',
      'non-behavioral',
    ],
    [
      'prose removal between separate tokens',
      'const value = /* Preserve the value. */ 1;',
      'const value = 1;',
      'non-behavioral',
    ],
    [
      'block and line comment reshaping',
      'const a = 1;\n/* Preserve the next value. */\nconst b = 2;',
      'const a = 1;\n// Preserve the\n// next value.\nconst b = 2;',
      'non-behavioral',
    ],
    [
      'prose after a hashbang',
      '#!/usr/bin/env node\nconst value = 1;',
      '#!/usr/bin/env node\n// Preserve the value.\nconst value = 1;',
      'non-behavioral',
    ],
    [
      'removal that merges operator tokens',
      'let value = 1; value+/* prose */+ +value;',
      'let value = 1; value++ +value;',
      'behavioral',
    ],
    [
      'removal that merges identifier tokens',
      'const value = 1, typeofvalue = 2; typeof/* prose */value;',
      'const value = 1, typeofvalue = 2; typeofvalue;',
      'behavioral',
    ],
    [
      'postfix semicolon insertion',
      'let a = 1, b = 2; a/* prose */++\nb;',
      'let a = 1, b = 2; a/* prose\n */++\nb;',
      'behavioral',
    ],
    [
      'prose inserted between a directive and code',
      '/* istanbul ignore next */\nconst value = 1;',
      '/* istanbul ignore next */\n// Inserted prose.\nconst value = 1;',
      'behavioral',
    ],
    [
      'directive moved between statements',
      '/*#__PURE__*/ run();\nother();',
      'run();\n/*#__PURE__*/ other();',
      'behavioral',
    ],
    [
      'trailing directive',
      'const value = 1;\n//# sourceMappingURL=one.map',
      'const value = 1;\n//# sourceMappingURL=two.map',
      'behavioral',
    ],
    [
      'numeric token spelling',
      'const n = 0x10;',
      'const n = 16;',
      'behavioral',
    ],
    [
      'string token spelling',
      'const value = "\\x61";',
      'const value = "a";',
      'behavioral',
    ],
  ])('preserves token boundaries for %s', (_label, before, after, expected) => {
    withDiff(
      'M\tsrc/identity.ts',
      {},
      {
        [`${BEFORE}:src/identity.ts`]: before,
        [`${AFTER}:src/identity.ts`]: after,
      },
    );
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe(expected);
  });

  it('accepts real Git prose insertion, removal, and rewrapping while rejecting executable changes', () => {
    const directory = mkdtempSync(join(tmpdir(), 'review-comment-shape-'));
    const git = (args: string[]) =>
      execFileSync('git', args, { cwd: directory, encoding: 'utf8' });
    const code = 'export const value = 1;\n';
    const commit = (source: string) => {
      writeFileSync(join(directory, 'identity.ts'), source);
      git(['add', 'identity.ts']);
      git([
        '-c',
        'user.name=Example',
        '-c',
        'user.email=example@example.invalid',
        '-c',
        'commit.gpgsign=false',
        'commit',
        '-qm',
        'comment fixture',
      ]);
      return git(['rev-parse', 'HEAD']).trim();
    };
    try {
      git(['init', '-q']);
      let before = commit(code);
      setGitHubRunner({
        runGh() {
          throw new Error('unexpected gh call');
        },
        runGit: git,
      });
      for (const source of [
        `// Preserve the existing value.\n${code}`,
        `// Preserve the\n// existing value.\n${code}`,
        code,
        `/**\n * Preserve the existing value.\n */\n${code}`,
        code,
        'export /* ordinary prose */ const value = 1;\n',
        'export const value = /* ordinary prose */ 1;\n',
        code,
        `${code}// Trailing prose.\n`,
        code,
      ]) {
        const after = commit(source);
        expect(classifyRangeEffect(before, after)).toBe('non-behavioral');
        before = after;
      }
      expect(
        classifyRangeEffect(before, commit('export const value = 2;\n')),
      ).toBe('behavioral');
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it.each(['.mjs', '.mts', '.js', '.jsx', '.ts', '.tsx'])(
    'rejects executable module changes hidden by a script parse in %s',
    (extension) => {
      const path = `src/identity${extension}`;
      withDiff(
        `M\t${path}`,
        {},
        {
          [`${BEFORE}:${path}`]: 'const r = await /[//]/.source; x = [\n 0]',
          [`${AFTER}:${path}`]: 'const r = await /[//]/.flags; y = [\n 0]',
        },
      );
      expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
    },
  );

  it.each(['.mjs', '.mts'])(
    'accepts prose around an unchanged explicit module expression in %s',
    (extension) => {
      const path = `src/identity${extension}`;
      const source = 'const r = await /[//]/.source; x = [\n 0]';
      withDiff(
        `M\t${path}`,
        {},
        {
          [`${BEFORE}:${path}`]: `// Read the pattern.\n${source}`,
          [`${AFTER}:${path}`]: `// Read the existing pattern.\n${source}`,
        },
      );
      expect(classifyRangeEffect(BEFORE, AFTER)).toBe('non-behavioral');
    },
  );

  it('rejects the module regression with real Git blobs accepted by Node', () => {
    const directory = mkdtempSync(join(tmpdir(), 'review-module-comment-'));
    const git = (args: string[]) =>
      execFileSync('git', args, { cwd: directory, encoding: 'utf8' });
    const commit = (source: string) => {
      execFileSync(process.execPath, ['--input-type=module', '--check'], {
        input: source,
      });
      writeFileSync(join(directory, 'identity.mjs'), source);
      git(['add', 'identity.mjs']);
      git([
        '-c',
        'user.name=Example',
        '-c',
        'user.email=example@example.invalid',
        '-c',
        'commit.gpgsign=false',
        'commit',
        '-qm',
        'module fixture',
      ]);
      return git(['rev-parse', 'HEAD']).trim();
    };
    try {
      git(['init', '-q']);
      const before = commit('const r = await /[//]/.source; x = [\n 0]');
      const after = commit('const r = await /[//]/.flags; y = [\n 0]');
      setGitHubRunner({
        runGh() {
          throw new Error('unexpected gh call');
        },
        runGit: git,
      });
      expect(classifyRangeEffect(before, after)).toBe('behavioral');
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it.each([
    [
      'CommonJS top-level return',
      'cjs',
      '// Old prose.\nif (process.env.SKIP_FIXTURE) return;\nmodule.exports = 42;',
      '// New prose.\nif (process.env.SKIP_FIXTURE) return;\nmodule.exports = 42;',
      'non-behavioral',
    ],
    [
      'CommonJS new.target',
      'cjs',
      '// Old prose.\nmodule.exports = new.target;',
      '// New prose.\nmodule.exports = new.target;',
      'non-behavioral',
    ],
    [
      'CommonJS executable change',
      'cjs',
      '// Old prose.\nif (process.env.SKIP_FIXTURE) return;\nmodule.exports = 42;',
      '// New prose.\nif (process.env.SKIP_FIXTURE) return;\nmodule.exports = 43;',
      'behavioral',
    ],
    [
      'CommonJS directive change',
      'cjs',
      '// @ts-check\nif (process.env.SKIP_FIXTURE) return;\nmodule.exports = 42;',
      '// @ts-nocheck\nif (process.env.SKIP_FIXTURE) return;\nmodule.exports = 42;',
      'behavioral',
    ],
    [
      'invalid ESM top-level return',
      'mjs',
      '// Old prose.\nreturn;',
      '// New prose.\nreturn;',
      'behavioral',
    ],
    [
      'TypeScript HTML-like comment',
      'ts',
      'run();\n// Old prose.',
      'run();\n<!-- Old prose.',
      'behavioral',
    ],
    [
      'ESM HTML-like comment edit',
      'mjs',
      'globalThis.value = limit <!--first;',
      'globalThis.value = limit <!--second;',
      'behavioral',
    ],
    [
      'CommonJS HTML-like comment hiding code',
      'cjs',
      'x <!--/*\nfirst()\n/*/ z\n*/w/i\n',
      'x <!--/*\nsecond()\n/*/ z\n*/w/i\n',
      'behavioral',
    ],
    [
      'script HTML-like comment hiding code',
      'js',
      'x <!--/*\nfirst()\n/*/ z\n*/w/i\n',
      'x <!--/*\nsecond()\n/*/ z\n*/w/i\n',
      'behavioral',
    ],
    [
      'module regular-expression spacing in JavaScript',
      'js',
      'const s = "axb";\nconsole.log(await / x /g.test(s));\n',
      'const s = "axb";\nconsole.log(await /x/g.test(s));\n',
      'behavioral',
    ],
    [
      'module regular-expression spacing in TypeScript',
      'ts',
      'const s = "axb";\nconsole.log(await / x /g.test(s));\n',
      'const s = "axb";\nconsole.log(await /x/g.test(s));\n',
      'behavioral',
    ],
    [
      'strict-mode-only script hiding module code',
      'js',
      'delete x; const r = await /[//]/.source; x = [\n 0]',
      'delete x; const r = await /[//]/.flags; y = [\n 0]',
      'behavioral',
    ],
    [
      'JSX prose in JavaScript',
      'js',
      'export const view = <div />; // Old prose.',
      'export const view = <div />; // New prose.',
      'non-behavioral',
    ],
    [
      'JSX executable change in JavaScript',
      'js',
      'export const view = <div />; // Old prose.',
      'export const view = <span />; // Old prose.',
      'behavioral',
    ],
  ])('respects %s', (_label, extension, before, after, expected) => {
    const path = `src/settings.${extension}`;
    withDiff(
      `M\t${path}`,
      {},
      { [`${BEFORE}:${path}`]: before, [`${AFTER}:${path}`]: after },
    );
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe(expected);
  });

  it.each([
    [
      'multiline JSDoc type',
      '/**\n * @type {{\n *   enabled: boolean\n * }}\n */\nconst settings = { enabled: true };',
      '/**\n * @type {{\n *   enabled: string\n * }}\n */\nconst settings = { enabled: true };',
    ],
    [
      'multiline lint directive',
      '/* eslint\n  no-console: "off"\n */\nconsole.log("ready");',
      '/* eslint\n  no-console: "error"\n */\nconsole.log("ready");',
    ],
    [
      'JSDoc opening delimiter',
      '/*\n * @type {string}\n */\nlet value = 1;',
      '/**\n * @type {string}\n */\nlet value = 1;',
    ],
    [
      'webpack magic',
      "export const load = () => import(/* webpackIgnore: false */ './x');",
      "export const load = () => import(/* webpackIgnore: true */ './x');",
    ],
    [
      'triple-slash reference',
      '/// <reference types="node" />\nexport const value = 1;',
      '/// <reference types="bun" />\nexport const value = 1;',
    ],
    [
      'Flow type annotation',
      'export const value /*: number */ = 1;',
      'export const value /*: string */ = 1;',
    ],
    [
      'Flow type include',
      'export class Settings { /*:: enabled: boolean; */ }',
      'export class Settings { /*:: enabled: string; */ }',
    ],
    [
      'Flow named type include',
      'export class Settings { /*flow-include enabled: boolean; */ }',
      'export class Settings { /*flow-include enabled: string; */ }',
    ],
    [
      'multiline Flow type include',
      '/* ::\n type Settings = {\n enabled: boolean\n };\n */\nexport const value = 1;',
      '/* ::\n type Settings = {\n enabled: string\n };\n */\nexport const value = 1;',
    ],
  ])('preserves the complete %s comment', (_label, before, after) => {
    withDiff(
      'M\tsrc/settings.js',
      {},
      {
        [`${BEFORE}:src/settings.js`]: before,
        [`${AFTER}:src/settings.js`]: after,
      },
    );
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
  });

  it.each([
    '/* #__PURE__ */\nconst value = 1;',
    '//# sourceMappingURL=original.js.map\nconst value = 1;',
    '//# debugId=synthetic-debug-reference\nconst value = 1;',
    '/* #tool_annotation */\nconst value = 1;',
  ])('preserves hash annotations as directives: %s', (before) => {
    const after = before.startsWith('/*')
      ? '/* prose */\nconst value = 1;'
      : '// prose\nconst value = 1;';
    withDiff(
      'M\tsrc/identity.ts',
      {},
      {
        [`${BEFORE}:src/identity.ts`]: before,
        [`${AFTER}:src/identity.ts`]: after,
      },
    );
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
  });

  it('classifies a real JSDoc edit with Markdown and issue references, and rejects a mode change', () => {
    const directory = mkdtempSync(join(tmpdir(), 'review-effect-'));
    const git = (args: string[]) =>
      execFileSync('git', args, { cwd: directory, encoding: 'utf8' });
    try {
      git(['init', '-q']);
      const commit = () => {
        git(['add', '.']);
        git([
          '-c',
          'user.name=Example',
          '-c',
          'user.email=example@example.invalid',
          '-c',
          'commit.gpgsign=false',
          'commit',
          '-qm',
          'fixture',
        ]);
        return git(['rev-parse', 'HEAD']).trim();
      };
      writeFileSync(
        join(directory, 'identity.ts'),
        '/**\n * # Notes\n * Existing guard.\n * See issue #42.\n */\nexport const identity = (value: string) => value;\n',
      );
      const before = commit();
      writeFileSync(
        join(directory, 'identity.ts'),
        '/**\n * # Notes\n * The existing guard preserves input.\n * See issue #42.\n */\nexport const identity = (value: string) => value;\n',
      );
      const after = commit();
      setGitHubRunner({
        runGh() {
          throw new Error('unexpected gh call');
        },
        runGit: git,
      });
      expect(classifyRangeEffect(before, after)).toBe('non-behavioral');
      git(['update-index', '--chmod=+x', 'identity.ts']);
      git([
        '-c',
        'user.name=Example',
        '-c',
        'user.email=example@example.invalid',
        '-c',
        'commit.gpgsign=false',
        'commit',
        '-qm',
        'mode fixture',
      ]);
      expect(
        classifyRangeEffect(after, git(['rev-parse', 'HEAD']).trim()),
      ).toBe('behavioral');
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it('fails closed on an unknown extension, a rename, and a missing git seam', () => {
    withDiff('M\tsrc/thing.zig');
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');

    withDiff('R100\tsrc/a.ts\tsrc/b.ts');
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');

    setGitHubRunner({
      runGh() {
        throw new Error('unexpected gh call');
      },
    });
    expect(classifyRangeEffect(BEFORE, AFTER)).toBe('behavioral');
  });

  it('does not consult severity in either direction', () => {
    // The function takes only a range. There is no argument by which a `major`
    // finding could force `behavioral` or a `nit` could force `non-behavioral`.
    expect(classifyRangeEffect.length).toBe(2);
  });
});
