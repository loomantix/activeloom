import { HUNK_WITH_LEFT_RE, SUPPORTED_SIDES } from './constants.js';
import { fail } from './errors.js';
import type { SupportedSide } from './types.js';

/**
 * The line numbers a unified diff touches, split by side.
 */
export interface DiffLines {
  leftLines: Set<number>;
  rightLines: Set<number>;
}

/**
 * Parse a unified diff patch string and return sets of valid line numbers on the left and right sides.
 */
export function parseDiffLines(patch: string): DiffLines {
  const leftLines = new Set<number>();
  const rightLines = new Set<number>();
  let left = 0;
  let right = 0;
  let inHunk = false;

  const lines = patch.split(/\r?\n/);
  for (const rawLine of lines) {
    const match = HUNK_WITH_LEFT_RE.exec(rawLine);
    if (match?.groups) {
      left = parseInt(match.groups['left']!, 10);
      right = parseInt(match.groups['right']!, 10);
      inHunk = true;
      continue;
    }
    if (!inHunk || rawLine.startsWith('\\ No newline')) {
      continue;
    }
    const prefix = rawLine.slice(0, 1);
    if (prefix === ' ') {
      leftLines.add(left);
      rightLines.add(right);
      left += 1;
      right += 1;
    } else if (prefix === '-') {
      leftLines.add(left);
      left += 1;
    } else if (prefix === '+') {
      rightLines.add(right);
      right += 1;
    }
  }

  return { leftLines, rightLines };
}

/**
 * Validate that a given path, line, and side correspond to an exact anchor in the PR diff files.
 */
export function validateAnchor(
  files: Record<string, string | null>,
  path: string,
  line: number | null | undefined,
  side: SupportedSide | null | undefined,
): void {
  if (!Object.prototype.hasOwnProperty.call(files, path)) {
    fail(`path is not part of the PR diff: ${path}`);
  }
  if (line === null || line === undefined) {
    return;
  }
  const resolvedSide = side ?? 'RIGHT';
  if (!SUPPORTED_SIDES.includes(resolvedSide)) {
    fail(`side must be one of: ${SUPPORTED_SIDES.join(', ')}`);
  }
  const patch = files[path];
  if (patch === null || patch === undefined) {
    fail('GitHub omitted the file patch; use --file-level only if defensible');
  }
  const { leftLines, rightLines } = parseDiffLines(patch);
  const valid = resolvedSide === 'RIGHT' ? rightLines : leftLines;
  if (valid.has(line)) {
    return;
  }
  const sortedCandidates = Array.from(valid).sort((a, b) => {
    const diffA = Math.abs(a - line);
    const diffB = Math.abs(b - line);
    if (diffA !== diffB) {
      return diffA - diffB;
    }
    return a - b;
  });
  const nearest = sortedCandidates.slice(0, 5);
  const candidates = nearest.join(', ') || 'none';
  fail(
    `line ${line} is not an exact ${resolvedSide} anchor in GitHub's PR patch; nearest valid lines: ${candidates}`,
  );
}
