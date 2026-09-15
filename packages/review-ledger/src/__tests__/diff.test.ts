import { describe, expect, it } from 'vitest';
import { parseDiffLines, validateAnchor } from '../diff.js';

describe('diff parsing and anchor validation', () => {
  const samplePatch = `
@@ -10,6 +10,7 @@
 context line 10
-removed line 11
+added line 11
+added line 12
 context line 12
@@ -20,4 +21,4 @@
 unchanged
-deleted 21
+modified 22
`.trim();

  it('parses left and right valid line numbers from patch hunks', () => {
    const { leftLines, rightLines } = parseDiffLines(samplePatch);
    expect(leftLines.has(10)).toBe(true);
    expect(leftLines.has(11)).toBe(true);
    expect(rightLines.has(10)).toBe(true);
    expect(rightLines.has(11)).toBe(true);
    expect(rightLines.has(12)).toBe(true);
    expect(rightLines.has(13)).toBe(true);
  });

  it('validates anchor on right and left side lines', () => {
    const files = { 'src/app.ts': samplePatch };
    expect(() =>
      validateAnchor(files, 'src/app.ts', 11, 'RIGHT'),
    ).not.toThrow();
    expect(() => validateAnchor(files, 'src/app.ts', 11, 'LEFT')).not.toThrow();
  });

  it('allows file-level anchor without line number', () => {
    const files = { 'src/app.ts': samplePatch };
    expect(() => validateAnchor(files, 'src/app.ts', null, null)).not.toThrow();
  });

  it('fails if path is not in PR diff', () => {
    const files = { 'src/app.ts': samplePatch };
    expect(() =>
      validateAnchor(files, 'src/other.ts', 10, 'RIGHT'),
    ).toThrowError('path is not part of the PR diff: src/other.ts');
  });

  it('fails if line is not in patch with nearest candidates', () => {
    const files = { 'src/app.ts': samplePatch };
    expect(() =>
      validateAnchor(files, 'src/app.ts', 999, 'RIGHT'),
    ).toThrowError(/line 999 is not an exact RIGHT anchor/);
  });

  it('rejects inherited Object.prototype keys as diff paths', () => {
    const files = { 'src/app.ts': samplePatch };
    for (const inherited of [
      'toString',
      'constructor',
      'valueOf',
      'hasOwnProperty',
    ]) {
      expect(() => validateAnchor(files, inherited, null, null)).toThrowError(
        `path is not part of the PR diff: ${inherited}`,
      );
      expect(() => validateAnchor(files, inherited, 11, 'RIGHT')).toThrowError(
        `path is not part of the PR diff: ${inherited}`,
      );
    }
  });

  it('rejects an unsupported side at runtime', () => {
    const files = { 'src/app.ts': samplePatch };
    expect(() =>
      validateAnchor(files, 'src/app.ts', 11, 'RIGTH' as 'RIGHT'),
    ).toThrowError(/side must be one of/);
  });
});
