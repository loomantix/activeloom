import type { ReviewFinding } from './types.js';

/**
 * Format an array of review findings into structured Markdown.
 */
export function formatFindings(findings: ReviewFinding[]): string {
  if (findings.length === 0) {
    return 'NO FINDINGS';
  }

  const lines: string[] = [
    `### Review Findings (${findings.length})`,
    '',
    '| Severity | Lens | File:Line | Description |',
    '| :--- | :--- | :--- | :--- |',
  ];

  for (const finding of findings) {
    const severity = finding.severity ?? 'minor';
    const lens = finding.lens ?? 'code-reviewer';
    const location =
      finding.fileLevel || finding.line === undefined
        ? `\`${finding.path}\``
        : `\`${finding.path}:${finding.line}\``;
    const desc = (
      finding.rootCause ??
      finding.message ??
      finding.rule ??
      finding.content ??
      ''
    )
      .trim()
      .replace(/\r?\n/g, ' ')
      .replace(/\|/g, '\\|');
    lines.push(`| **${severity}** | \`${lens}\` | ${location} | ${desc} |`);
  }

  return lines.join('\n');
}
