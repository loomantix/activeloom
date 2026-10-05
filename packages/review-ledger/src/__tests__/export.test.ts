import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { runCli } from '../cli.js';
import { LedgerError } from '../errors.js';
import {
  exportTelemetry,
  parseAllowedLogins,
  parseExportBound,
  parsePullRequestNumbers,
  prCommentSource,
  type ExportLine,
  type ExportTrailer,
  type PullRequestSelection,
} from '../export.js';
import { resetGitHubRunner, setGitHubRunner } from '../github.js';
import {
  buildTelemetryBody,
  buildTelemetryRecord,
  classifyFiles,
  type BuildTelemetryParams,
} from '../index.js';

const REPO = 'owner/repo';

function body(overrides: Partial<BuildTelemetryParams> = {}): string {
  return buildTelemetryBody(
    buildTelemetryRecord({
      emittedAt: '2026-08-20T05:12:33Z',
      repo: REPO,
      pr: 7,
      engine: 'claude',
      engineVersion: null,
      passType: 'review',
      reviewTier: 'lean',
      trigger: 'interactive',
      round: 1,
      stance: 'adversarial',
      status: 'clean',
      baseSha: '2'.repeat(40),
      headSha: '1'.repeat(40),
      promptStackSha256: null,
      repoInstructionsSha256: null,
      tokenSource: 'unavailable',
      tokens: [],
      truncated: false,
      durationSeconds: null,
      changeset: classifyFiles([
        { path: 'src/a.ts', added: 3, deleted: 1, blank: 0 },
      ]).changeset,
      findings: {
        posted: 0,
        bySeverityAndOutcome: {
          blocking: { validFixed: 0, validDeferred: 0, invalidDismissed: 0 },
          major: { validFixed: 0, validDeferred: 0, invalidDismissed: 0 },
          minor: { validFixed: 0, validDeferred: 0, invalidDismissed: 0 },
          nit: { validFixed: 0, validDeferred: 0, invalidDismissed: 0 },
        },
        chainInducedRegressions: 0,
      },
      ...overrides,
    }),
  );
}

let nextId = 100;
function comment(
  text: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  nextId += 1;
  return {
    id: nextId,
    body: text,
    author_association: 'MEMBER',
    user: { login: 'maintainer' },
    created_at: new Date(Date.UTC(2026, 7, 20) + nextId * 1000)
      .toISOString()
      .replace('.000Z', 'Z'),
    updated_at: '2026-08-21T00:00:00Z',
    ...overrides,
  };
}

interface FakeIssue {
  pullRequest?: boolean;
  /** Overrides the count GitHub reports for the issue. */
  reported?: number;
  createdAt?: string;
  comments: Array<Record<string, unknown>> | 'unreadable';
}

/** Serve issues and their comments, recording every `gh` invocation. */
function serve(issues: Record<number, FakeIssue>): string[][] {
  const calls: string[][] = [];
  const issueRow = (number: number, issue: FakeIssue) => ({
    number,
    created_at: issue.createdAt ?? '2026-08-01T00:00:00Z',
    comments:
      issue.reported ??
      (issue.comments === 'unreadable' ? 1 : issue.comments.length),
    ...(issue.pullRequest === false ? {} : { pull_request: {} }),
  });
  setGitHubRunner({
    runGh(args, payload) {
      calls.push(args);
      expect(payload).toBeUndefined();
      const path = args.at(-1)!;
      const comments = path.match(/\/issues\/(\d+)\/comments\?per_page=100$/);
      if (comments) {
        const issue = issues[Number(comments[1])]!;
        if (issue.comments === 'unreadable') {
          throw new LedgerError('GitHub operation failed: HTTP 502');
        }
        return JSON.stringify([issue.comments]);
      }
      const single = path.match(/\/issues\/(\d+)$/);
      if (single) {
        const issue = issues[Number(single[1])];
        if (!issue) {
          throw new LedgerError('GitHub operation failed: HTTP 404');
        }
        return JSON.stringify(issueRow(Number(single[1]), issue));
      }
      if (path.includes('/issues?')) {
        return JSON.stringify([
          Object.entries(issues).map(([number, issue]) =>
            issueRow(Number(number), issue),
          ),
        ]);
      }
      throw new Error(`unexpected gh call: ${args.join(' ')}`);
    },
  });
  return calls;
}

function run(
  selection: PullRequestSelection,
  allowedLogins?: string[],
): { lines: ExportLine[]; trailer: ExportTrailer; warnings: string[] } {
  const written: string[] = [];
  const warnings: string[] = [];
  const trailer = exportTelemetry({
    repo: REPO,
    sources: [prCommentSource({ repo: REPO, selection })],
    allowedLogins,
    write: (line) => written.push(line),
    warn: (message) => warnings.push(message),
  });
  expect(JSON.parse(written.at(-1)!)).toEqual(trailer);
  return {
    lines: written.slice(0, -1).map((line) => JSON.parse(line) as ExportLine),
    trailer,
    warnings,
  };
}

afterEach(() => {
  resetGitHubRunner();
});

describe('exportTelemetry', () => {
  it('writes one line per record and a complete trailer, reading only', () => {
    const first = comment(body());
    const calls = serve({
      7: {
        comments: [
          comment('Runner-verified claude pass 1.'),
          first,
          comment(body({ round: 2 })),
        ],
      },
    });
    const { lines, trailer, warnings } = run({ numbers: [7] });

    expect(lines).toHaveLength(2);
    expect(lines[0]).toMatchObject({
      schema: 'activeloom.review-metrics.export/v1',
      source: {
        kind: 'pr-comment',
        repo: REPO,
        issue: 7,
        commentId: first['id'],
        authorAssociation: 'MEMBER',
        createdAt: first['created_at'],
        updatedAt: '2026-08-21T00:00:00Z',
      },
      record: { version: 1, pr: 7, round: 1 },
    });
    expect(trailer).toEqual({
      schema: 'activeloom.review-metrics.export-trailer/v1',
      sources: [
        { kind: 'pr-comment', issue: 7, commentsReported: 3, commentsRead: 3 },
      ],
      records: 2,
      duplicatesCollapsed: 0,
      conflicts: 0,
      rejectedAuthor: 0,
      malformed: 0,
      complete: true,
    });
    expect(warnings).toEqual([]);
    expect(
      calls.every((args) => args[0] === 'api' && !args.includes('-X')),
    ).toBe(true);
    expect(calls.flat()).not.toContain('user');
  });

  it('matches the published schema field for field', () => {
    serve({ 7: { comments: [comment(body())] } });
    const { lines, trailer } = run({ numbers: [7] });
    const schema = JSON.parse(
      readFileSync(
        new URL(
          '../../protocol/review-metrics-export.v1.schema.json',
          import.meta.url,
        ),
        'utf8',
      ),
    ) as {
      $defs: Record<
        'line' | 'trailer',
        {
          required: string[];
          properties: Record<string, { const?: string }> & {
            source: { required: string[] };
            sources: { items: { required: string[] } };
          };
        }
      >;
    };
    const { line, trailer: trailerSchema } = schema.$defs;
    expect(Object.keys(lines[0]!)).toEqual(line.required);
    expect(Object.keys(lines[0]!.source)).toEqual(
      line.properties.source.required,
    );
    expect(line.properties['schema']!.const).toBe(lines[0]!.schema);
    expect(Object.keys(trailer)).toEqual(trailerSchema.required);
    expect(Object.keys(trailer.sources[0]!)).toEqual(
      trailerSchema.properties.sources.items.required,
    );
    expect(trailerSchema.properties['schema']!.const).toBe(trailer.schema);
  });

  it.each(['NONE', 'CONTRIBUTOR', 'FIRST_TIME_CONTRIBUTOR', undefined])(
    'counts a record from a %s author as rejected',
    (association) => {
      serve({
        7: {
          comments: [
            comment(body(), { author_association: association }),
            comment(body({ round: 2 }), { author_association: 'OWNER' }),
            comment(body({ round: 3 }), { author_association: 'COLLABORATOR' }),
          ],
        },
      });
      const { lines, trailer } = run({ numbers: [7] });
      expect(lines.map((line) => line.record.round)).toEqual([2, 3]);
      expect(trailer).toMatchObject({ records: 2, rejectedAuthor: 1 });
    },
  );

  it('narrows accepted authors to an explicit login list', () => {
    serve({
      7: {
        comments: [
          comment(body(), { user: { login: 'Maintainer' } }),
          comment(body({ round: 2 }), { user: { login: 'someone-else' } }),
          // A listed login never widens the association rule.
          comment(body({ round: 3 }), { author_association: 'NONE' }),
        ],
      },
    });
    const { lines, trailer } = run({ numbers: [7] }, ['maintainer']);
    expect(lines.map((line) => line.record.round)).toEqual([1]);
    expect(trailer).toMatchObject({ records: 1, rejectedAuthor: 2 });
  });

  it('counts a marked comment with no valid record as malformed', () => {
    serve({
      7: {
        comments: [
          comment('<!-- local-review-telemetry:v1 -->\n\n```json\n{}\n```'),
          comment('<!-- local-review-telemetry:v9 -->'),
          // A malformed record from a rejected author stays a rejection.
          comment('<!-- local-review-telemetry:v1 -->', {
            author_association: 'NONE',
          }),
          comment(body()),
        ],
      },
    });
    const { trailer } = run({ numbers: [7] });
    expect(trailer).toMatchObject({
      records: 1,
      malformed: 2,
      rejectedAuthor: 1,
      complete: true,
    });
  });

  it('collapses identical replays across pull requests', () => {
    const original = comment(body({ durationSeconds: 40 }));
    serve({
      7: { comments: [original] },
      8: {
        comments: [
          // A replay differs only in when it was emitted and in a duration it
          // has not measured yet.
          comment(body({ emittedAt: '2026-08-20T06:00:00Z' })),
        ],
      },
    });
    const { lines, trailer, warnings } = run({ numbers: [7, 8] });
    expect(lines.map((line) => line.source.commentId)).toEqual([
      original['id'],
    ]);
    expect(trailer).toMatchObject({
      records: 1,
      duplicatesCollapsed: 1,
      conflicts: 0,
    });
    expect(warnings).toEqual([]);
  });

  it('reports a conflicting replay and keeps the earliest', () => {
    const later = comment(body({ status: 'changed' }), {
      created_at: '2026-08-20T09:00:00Z',
    });
    const earliest = comment(body(), { created_at: '2026-08-20T08:00:00Z' });
    serve({ 7: { comments: [later, earliest] } });
    const { lines, trailer, warnings } = run({ numbers: [7] });
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatchObject({
      source: { commentId: earliest['id'] },
      record: { status: 'clean' },
    });
    expect(trailer).toMatchObject({
      records: 1,
      duplicatesCollapsed: 0,
      conflicts: 1,
    });
    expect(warnings).toEqual([
      expect.stringContaining(
        `kept comment ${String(earliest['id'])}, dropped comment ${String(later['id'])}`,
      ),
    ]);
  });

  it('is incomplete when GitHub reports more comments than were read', () => {
    serve({
      7: { comments: [comment(body())], reported: 2 },
      8: { comments: [comment(body({ round: 2 }))] },
    });
    const { lines, trailer } = run({ numbers: [7, 8] });
    expect(lines).toHaveLength(2);
    expect(trailer.sources[0]).toMatchObject({
      commentsReported: 2,
      commentsRead: 1,
    });
    expect(trailer.complete).toBe(false);
  });

  it('is incomplete when a pull request or its comments cannot be read', () => {
    serve({ 7: { comments: 'unreadable' }, 9: { comments: [], reported: 0 } });
    const { trailer, warnings } = run({ numbers: [7, 8, 9] });
    expect(trailer.sources).toEqual([
      { kind: 'pr-comment', issue: 7, commentsReported: 1, commentsRead: null },
      {
        kind: 'pr-comment',
        issue: 8,
        commentsReported: null,
        commentsRead: null,
      },
      { kind: 'pr-comment', issue: 9, commentsReported: 0, commentsRead: 0 },
    ]);
    expect(trailer.complete).toBe(false);
    expect(warnings).toHaveLength(2);
  });

  it('skips a number that is not a pull request, and says so', () => {
    serve({
      7: { pullRequest: false, comments: [comment(body())] },
      8: { comments: [] },
    });
    const { lines, trailer, warnings } = run({ numbers: [7, 8] });
    expect(lines).toEqual([]);
    expect(trailer.sources.map((source) => source.issue)).toEqual([8]);
    expect(trailer.complete).toBe(true);
    expect(warnings).toEqual(['skipped #7: not a pull request']);
  });

  it('selects pull requests by date', () => {
    const calls = serve({
      5: { comments: [comment(body({ pr: 5 }))] },
      6: { pullRequest: false, comments: [comment(body({ pr: 6 }))] },
      7: {
        createdAt: '2026-09-02T00:00:00Z',
        comments: [comment(body({ pr: 7 }))],
      },
    });
    const { lines, trailer } = run({
      since: '2026-07-01T00:00:00Z',
      until: '2026-09-01T23:59:59Z',
    });
    expect(lines.map((line) => line.source.issue)).toEqual([5]);
    expect(trailer.complete).toBe(true);
    expect(calls[0]!.at(-1)).toContain('&since=2026-07-01T00:00:00Z');
  });

  it('rejects a malformed repository before reading anything', () => {
    const calls = serve({});
    expect(() =>
      exportTelemetry({
        repo: 'owner/repo/issues/1',
        sources: [],
        write: () => undefined,
        warn: () => undefined,
      }),
    ).toThrowError(/owner\/name/);
    expect(calls).toEqual([]);
  });
});

describe('export argument parsing', () => {
  it('expands numbers and ranges into distinct ascending numbers', () => {
    expect(parsePullRequestNumbers('395,372-374,373')).toEqual([
      372, 373, 374, 395,
    ]);
  });

  it.each(['', '0', '5-3', '3-', 'a', '1,,2', '1-2000'])(
    'rejects the selection %j',
    (spec) => {
      expect(() => parsePullRequestNumbers(spec)).toThrowError(/--prs/);
    },
  );

  it('reads a bare date as the whole UTC day', () => {
    expect(parseExportBound('2026-10-04', '--since')).toBe(
      '2026-10-04T00:00:00Z',
    );
    expect(parseExportBound('2026-10-04', '--until')).toBe(
      '2026-10-04T23:59:59Z',
    );
    expect(parseExportBound('2026-10-04T12:30:00Z', '--since')).toBe(
      '2026-10-04T12:30:00Z',
    );
  });

  it.each(['yesterday', '2026-13-01', '2026-10-04T12:30:00+02:00'])(
    'rejects the bound %j',
    (value) => {
      expect(() => parseExportBound(value, '--since')).toThrowError(/--since/);
    },
  );

  it('rejects a login list that is not a list of logins', () => {
    expect(parseAllowedLogins('octocat,review-bot[bot]')).toEqual([
      'octocat',
      'review-bot[bot]',
    ]);
    expect(() => parseAllowedLogins('octocat, other')).toThrowError(
      /--allowed-logins/,
    );
  });

  it.each([
    [['export'], /requires --repo/],
    [['export', '--repo', REPO], /exactly one selection/],
    [
      ['export', '--repo', REPO, '--pr', '1', '--since', '2026-10-04'],
      /exactly one selection/,
    ],
    [
      ['export', '--repo', REPO, '--pr', '1', '--prs', '2'],
      /exactly one selection/,
    ],
  ])('rejects %j', (argv, message) => {
    const stdout = vi.spyOn(process.stdout, 'write');
    try {
      expect(() => runCli(argv)).toThrowError(message);
      expect(stdout).not.toHaveBeenCalled();
    } finally {
      stdout.mockRestore();
    }
  });
});
