import { describe, expect, it } from 'vitest';
import { formatFindings } from '../format.js';
import type { ReviewFinding } from '../types.js';

describe('formatFindings', () => {
  it('returns NO FINDINGS when list is empty', () => {
    expect(formatFindings([])).toBe('NO FINDINGS');
  });

  it('formats review findings as Markdown table', () => {
    const findings: ReviewFinding[] = [
      {
        path: 'src/auth.ts',
        line: 42,
        severity: 'blocking',
        lens: 'security-reviewer',
        message: 'Missing token verification check',
      },
      {
        path: 'src/config.ts',
        fileLevel: true,
        severity: 'minor',
        lens: 'code-reviewer',
        rootCause: 'Inconsistent property naming',
      },
    ];

    const output = formatFindings(findings);
    expect(output).toContain('### Review Findings (2)');
    expect(output).toContain(
      '| **blocking** | `security-reviewer` | `src/auth.ts:42` | Missing token verification check |',
    );
    expect(output).toContain(
      '| **minor** | `code-reviewer` | `src/config.ts` | Inconsistent property naming |',
    );
  });
});
