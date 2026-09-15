import { beforeEach, describe, expect, it } from 'vitest';
import {
  CHANGESET_CLASSIFIER_VERSION,
  classifyFiles,
  classifyPath,
  classifyRange,
  parseDiffPatch,
} from '../index.js';
import { resetGitHubRunner, setGitHubRunner } from '../github.js';
import type { GitHubRunner } from '../types.js';

const BASE = '2'.repeat(40);
const HEAD = '1'.repeat(40);

describe('classifyPath', () => {
  it.each([
    ['packages/review-ledger/src/ledger.ts', 'app', true],
    ['services/api/main.py', 'app', true],
    ['scripts/deploy.sh', 'app', true],
    ['src/__tests__/ledger.test.ts', 'test', true],
    ['tests/conftest.py', 'test', true],
    ['internal/store/store_test.go', 'test', true],
    ['README.md', 'docsConfig', false],
    ['docs/architecture/decisions.md', 'docsConfig', false],
    ['handbook/onboarding.md', 'docsConfig', false],
    ['.gitignore', 'docsConfig', false],
    ['src/payloads/claim.fixture.ts', 'docsConfig', false],
    ['fixtures/sample.json', 'docsConfig', false],
    ['src/fixtures/sample.json', 'docsConfig', false],
    ['pnpm-lock.yaml', 'generated', true],
    ['packages/x/dist/index.js', 'generated', false],
    ['vendor/review-ledger.bundle.js', 'generated', false],
    ['src/__snapshots__/render.snap', 'generated', false],
    ['.github/workflows/ci.yml', 'app', true],
    ['package.json', 'app', true],
    ['infra/eks/main.tf', 'app', true],
    ['db/migrations/0007_add_column.sql', 'app', true],
    ['prisma/schema.prisma', 'app', true],
    ['config/regions.yaml', 'app', true],
  ])('classifies %s as %s', (path, cls, reviewSignificant) => {
    const classification = classifyPath(path);
    expect(classification.class).toBe(cls);
    expect(classification.reviewSignificant).toBe(reviewSignificant);
  });

  it('treats prompt surface Markdown as source, not docs', () => {
    const skill = classifyPath('.claude/skills/critique/SKILL.md');
    expect(skill.class).toBe('app');
    expect(skill.reviewSignificant).toBe(true);
    expect(classifyPath('AGENTS.md').reviewSignificant).toBe(true);
  });

  it('accepts a repository-specific prompt surface', () => {
    const options = { promptSurfaces: ['prompts/'] };
    expect(classifyPath('prompts/reviewer.md', options).class).toBe('app');
    expect(classifyPath('.claude/skills/x/SKILL.md', options).class).toBe(
      'docsConfig',
    );
  });

  it('treats an unrecognised path as source rather than skipping it', () => {
    const classification = classifyPath('tools/weird-thing.xyz');
    expect(classification.class).toBe('app');
    expect(classification.reviewSignificant).toBe(true);
    expect(classification.language).toBeNull();
  });

  it('normalises a leading ./ and Windows separators', () => {
    expect(classifyPath('./src/a.ts').path).toBe('src/a.ts');
    expect(classifyPath('src\\a.ts').path).toBe('src/a.ts');
  });
});

describe('classifyFiles', () => {
  it('partitions churn across the classes without double counting', () => {
    const report = classifyFiles([
      { path: 'src/a.ts', added: 10, deleted: 4, blank: 2 },
      { path: 'src/__tests__/a.test.ts', added: 8, deleted: 0, blank: 1 },
      { path: 'README.md', added: 3, deleted: 3, blank: 0 },
      { path: 'pnpm-lock.yaml', added: 900, deleted: 100, blank: 0 },
    ]);
    const lines = report.changeset.linesChanged;
    expect(lines.app).toBe(12);
    expect(lines.test).toBe(7);
    expect(lines.docsConfig).toBe(6);
    expect(lines.generated).toBe(1000);
    expect(lines.blank).toBe(3);
    expect(
      lines.app + lines.test + lines.docsConfig + lines.generated + lines.blank,
    ).toBe(1028);
    expect(report.changeset.files).toEqual({
      app: 1,
      test: 1,
      docsConfig: 1,
      generated: 1,
    });
    expect(report.changeset.classifierVersion).toBe(
      CHANGESET_CLASSIFIER_VERSION,
    );
  });

  it('leaves comment null rather than guessing at it', () => {
    const report = classifyFiles([{ path: 'src/a.ts', added: 5, deleted: 0 }]);
    expect(report.changeset.linesChanged.comment).toBeNull();
  });

  it('keeps generated churn out of the language mix', () => {
    const report = classifyFiles([
      { path: 'src/a.ts', added: 4, deleted: 0 },
      { path: 'ui/Panel.tsx', added: 6, deleted: 0 },
      { path: 'pnpm-lock.yaml', added: 500, deleted: 0 },
    ]);
    expect(report.changeset.linesByLanguage).toEqual({ ts: 4, tsx: 6 });
  });

  it('skips only when nothing in the range is review-significant', () => {
    expect(
      classifyFiles([
        { path: 'README.md', added: 40, deleted: 2 },
        { path: 'docs/guide.md', added: 10, deleted: 0 },
      ]).skip,
    ).toBe(true);
    expect(
      classifyFiles([
        { path: 'README.md', added: 40, deleted: 2 },
        { path: 'src/a.ts', added: 1, deleted: 0 },
      ]).skip,
    ).toBe(false);
  });

  it('reviews a lockfile-only change while keeping it out of the ratio', () => {
    const report = classifyFiles([
      { path: 'pnpm-lock.yaml', added: 300, deleted: 120 },
    ]);
    expect(report.skip).toBe(false);
    expect(report.reviewSignificantFiles).toBe(1);
    expect(report.changeset.linesChanged.app).toBe(0);
    expect(report.changeset.linesChanged.generated).toBe(420);
  });

  it('rejects a blank count larger than the churn it belongs to', () => {
    expect(() =>
      classifyFiles([{ path: 'src/a.ts', added: 1, deleted: 0, blank: 2 }]),
    ).toThrow(/invalid blank count/);
  });

  it.each([
    { added: -1, deleted: 2 },
    { added: 2, deleted: -1 },
    { added: 0.5, deleted: 1 },
    { added: 1, deleted: Number.MAX_SAFE_INTEGER + 1 },
  ])('rejects invalid component churn counts: %o', ({ added, deleted }) => {
    expect(() => classifyFiles([{ path: 'src/a.ts', added, deleted }])).toThrow(
      /invalid churn count/,
    );
  });

  it('rejects churn whose component sum is not a safe integer', () => {
    expect(() =>
      classifyFiles([
        { path: 'src/a.ts', added: Number.MAX_SAFE_INTEGER, deleted: 1 },
      ]),
    ).toThrow(/invalid churn total/);
  });
});

describe('parseDiffPatch', () => {
  it('parses a standard unified diff without Git extension headers', () => {
    const patch = [
      '--- a/src/a.ts',
      '+++ b/src/a.ts',
      '@@ -1 +1 @@',
      '-const oldValue = 1;',
      '+const newValue = 1;',
    ].join('\n');
    expect(parseDiffPatch(patch)).toEqual([
      { path: 'src/a.ts', added: 1, deleted: 1, blank: 0 },
    ]);
  });

  it('parses multiple files in a standard unified diff', () => {
    const patch = [
      '--- a/src/a.ts',
      '+++ b/src/a.ts',
      '@@ -1 +1 @@',
      '-old a',
      '+new a',
      '--- a/src/b.ts',
      '+++ b/src/b.ts',
      '@@ -0,0 +1 @@',
      '+new b',
    ].join('\n');
    expect(parseDiffPatch(patch)).toEqual([
      { path: 'src/a.ts', added: 1, deleted: 1, blank: 0 },
      { path: 'src/b.ts', added: 1, deleted: 0, blank: 0 },
    ]);
  });

  it('rejects non-empty input with no readable file records', () => {
    expect(() => parseDiffPatch('not a unified diff')).toThrow(
      /no readable file records/,
    );
  });

  it('counts churn and blank churn per file', () => {
    const patch = [
      'diff --git a/src/a.ts b/src/a.ts',
      'index 111..222 100644',
      '--- a/src/a.ts',
      '+++ b/src/a.ts',
      '@@ -1,2 +1,4 @@',
      ' const kept = 1;',
      '-const gone = 2;',
      '+const added = 2;',
      '+',
      '+const other = 3;',
      '',
    ].join('\n');
    expect(parseDiffPatch(patch)).toEqual([
      { path: 'src/a.ts', added: 3, deleted: 1, blank: 1 },
    ]);
  });

  it('reads the destination path of a rename and a deletion', () => {
    const patch = [
      'diff --git a/src/old.ts b/src/new.ts',
      'similarity index 90%',
      'rename from src/old.ts',
      'rename to src/new.ts',
      '--- a/src/old.ts',
      '+++ b/src/new.ts',
      '@@ -1 +1 @@',
      '-const a = 1;',
      '+const a = 2;',
      'diff --git a/src/dropped.ts b/src/dropped.ts',
      'deleted file mode 100644',
      '--- a/src/dropped.ts',
      '+++ /dev/null',
      '@@ -1 +0,0 @@',
      '-const b = 1;',
    ].join('\n');
    expect(parseDiffPatch(patch).map((file) => file.path)).toEqual([
      'src/new.ts',
      'src/dropped.ts',
    ]);
  });

  it('keeps a binary file in the changeset with no lines', () => {
    const patch = [
      'diff --git a/assets/logo.png b/assets/logo.png',
      'index 111..222 100644',
      'Binary files a/assets/logo.png and b/assets/logo.png differ',
    ].join('\n');
    expect(parseDiffPatch(patch)).toEqual([
      { path: 'assets/logo.png', added: 0, deleted: 0, blank: 0 },
    ]);
  });

  it('rejects an ambiguous unquoted binary path instead of truncating it', () => {
    const patch = [
      'diff --git a/foo b/bar b/foo b/bar',
      'Binary files a/foo b/bar and b/foo b/bar differ',
    ].join('\n');
    expect(() => parseDiffPatch(patch)).toThrow(/no readable path/);
  });

  it('decodes a quoted path', () => {
    const patch = [
      'diff --git "a/src/od\\303\\251.ts" "b/src/od\\303\\251.ts"',
      '--- "a/src/od\\303\\251.ts"',
      '+++ "b/src/od\\303\\251.ts"',
      '@@ -0,0 +1 @@',
      '+const a = 1;',
    ].join('\n');
    expect(parseDiffPatch(patch)[0]?.path).toBe('src/odé.ts');
  });

  it('ignores hunk context and the no-newline marker', () => {
    const patch = [
      'diff --git a/a.txt b/a.txt',
      '--- a/a.txt',
      '+++ b/a.txt',
      '@@ -1,2 +1,2 @@',
      ' context',
      '-old',
      '+new',
      '\\ No newline at end of file',
    ].join('\n');
    expect(parseDiffPatch(patch)).toEqual([
      { path: 'a.txt', added: 1, deleted: 1, blank: 0 },
    ]);
  });

  it('does not parse header-like hunk content as file metadata', () => {
    const patch = [
      'diff --git a/src/query.sql b/src/query.sql',
      '--- a/src/query.sql',
      '+++ b/src/query.sql',
      '@@ -1 +1 @@',
      '--- README.md',
      '+++ not-a-header',
    ].join('\n');
    expect(parseDiffPatch(patch)).toEqual([
      { path: 'src/query.sql', added: 1, deleted: 1, blank: 0 },
    ]);
  });
});

describe('classifyRange', () => {
  class GitRunner implements GitHubRunner {
    public lastArgs: string[] = [];
    runGh(): string {
      throw new Error('classifyRange must not touch GitHub');
    }
    runGit(args: string[]): string {
      this.lastArgs = args;
      return [
        'diff --git a/src/a.ts b/src/a.ts',
        '--- a/src/a.ts',
        '+++ b/src/a.ts',
        '@@ -1 +1 @@',
        '-const a = 1;',
        '+const a = 2;',
      ].join('\n');
    }
  }

  beforeEach(() => {
    resetGitHubRunner();
  });

  it('classifies the pinned range and never the working tree', () => {
    const runner = new GitRunner();
    setGitHubRunner(runner);
    const report = classifyRange({ base: BASE, head: HEAD });
    expect(runner.lastArgs).toContain(`${BASE}..${HEAD}`);
    expect(report.changeset.linesChanged.app).toBe(2);
    expect(report.skip).toBe(false);
  });

  it('refuses a range that is not pinned to full SHAs', () => {
    setGitHubRunner(new GitRunner());
    expect(() => classifyRange({ base: 'HEAD~1', head: HEAD })).toThrow(
      /--base must be a full 40-character lowercase commit SHA/,
    );
  });
});
