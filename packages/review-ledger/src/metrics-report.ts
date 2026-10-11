import type { ExportLine, ExportTrailer } from './export.js';
const CANONICAL_TOKEN_BUCKETS = [
  'input',
  'output',
  'cacheRead',
  'cacheWrite',
  'reasoning',
] as const;

/** Human-facing measurements, never convergence or control evidence. */
export function metricsReport(
  lines: ExportLine[],
  trailer: ExportTrailer,
): string {
  const engines = new Map<
    string,
    {
      passes: number;
      findings: number;
      fixed: number;
      deferred: number;
      dismissed: number;
      tokens: Record<string, { total: number; missing: number }>;
    }
  >();
  const prs = new Map<
    number,
    {
      rounds: number;
      passes: number;
      clean: boolean;
      input: number;
      output: number;
      unknown: number;
    }
  >();
  for (const { record } of lines) {
    const engine = engines.get(record.engine) ?? {
      passes: 0,
      findings: 0,
      fixed: 0,
      deferred: 0,
      dismissed: 0,
      tokens: Object.fromEntries(
        CANONICAL_TOKEN_BUCKETS.map((key) => [key, { total: 0, missing: 0 }]),
      ),
    };
    engine.passes++;
    engine.findings += record.findings.posted;
    for (const counts of Object.values(record.findings.bySeverityAndOutcome)) {
      engine.fixed += counts.validFixed;
      engine.deferred += counts.validDeferred;
      engine.dismissed += counts.invalidDismissed;
    }
    for (const key of CANONICAL_TOKEN_BUCKETS) {
      const summary = engine.tokens[key]!;
      if (record.tokens.length === 0) summary.missing++;
      for (const bucket of record.tokens) {
        if (bucket[key] === null) summary.missing++;
        else summary.total += bucket[key];
      }
    }
    engines.set(record.engine, engine);
    const pr = prs.get(record.pr) ?? {
      rounds: 0,
      passes: 0,
      clean: false,
      input: 0,
      output: 0,
      unknown: 0,
    };
    pr.passes++;
    if (record.tokens.length === 0) pr.unknown++;
    for (const bucket of record.tokens) {
      pr.input += bucket.input ?? 0;
      pr.output += bucket.output ?? 0;
      if (bucket.input === null || bucket.output === null) pr.unknown++;
    }
    if (record.passType === 'review') {
      pr.rounds = Math.max(pr.rounds, record.round);
      pr.clean ||= record.status === 'clean';
    }
    prs.set(record.pr, pr);
  }
  const output = [
    `Review metrics: ${lines.length} records across ${prs.size} pull requests`,
    `Source enumeration: ${trailer.complete ? 'complete' : 'INCOMPLETE'}; conflicts: ${trailer.conflicts}; rejected authors: ${trailer.rejectedAuthor}; malformed: ${trailer.malformed}`,
    'Counts are reported per pass, not unique findings. Missing usage is unknown, not zero.',
    '',
  ];
  for (const [name, value] of [...engines].sort(([a], [b]) =>
    a.localeCompare(b),
  )) {
    output.push(
      `${name}: ${value.passes} passes; ${value.findings} findings; ${value.fixed} fixed, ${value.deferred} deferred, ${value.dismissed} dismissed`,
    );
    output.push(
      '  Tokens: ' +
        Object.entries(value.tokens)
          .map(
            ([key, count]) =>
              `${key}=${count.total}${count.missing ? ` (+${count.missing} unknown measurements)` : ''}`,
          )
          .join(', '),
    );
  }
  output.push(
    '',
    'Observed rounds (telemetry cannot prove convergence; separate runs may restart numbering):',
  );
  for (const [number, value] of [...prs].sort(([a], [b]) => a - b)) {
    output.push(
      `PR ${number}: max review round ${value.rounds}; ${value.passes} passes; clean pass observed: ${value.clean ? 'yes' : 'no'}; known input/output tokens ${value.input}/${value.output}${value.unknown ? ` (+${value.unknown} unknown measurements)` : ''}`,
    );
  }
  return output.join('\n') + '\n';
}
