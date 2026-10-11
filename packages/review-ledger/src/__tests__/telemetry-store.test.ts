import { afterEach, describe, expect, it, vi } from 'vitest';
import * as fs from 'node:fs';
import {
  DefaultGitHubRunner,
  resetGitHubRunner,
  setGitHubRunner,
} from '../github.js';
import {
  activeMetricsStore,
  METRICS_STORE_LABEL,
  metricsStores,
  setupMetricsStore,
  telemetryStore,
} from '../telemetry-store.js';
import {
  buildTelemetryBody,
  buildTelemetryRecord,
  configuredTelemetrySink,
  emitTelemetry,
  enrichTelemetryDuration,
  telemetryRecorded,
} from '../telemetry.js';
import { classifyFiles } from '../changeset.js';
import {
  exportTelemetry,
  storeIssueSource,
  type ExportLine,
} from '../export.js';
import { metricsReport } from '../metrics-report.js';

vi.mock('node:fs', async (importOriginal) => {
  const original = await importOriginal<typeof import('node:fs')>();
  return { ...original, readFileSync: vi.fn(original.readFileSync) };
});

const repo = 'owner/repo';
const record = buildTelemetryRecord({
  repo,
  pr: 7,
  emittedAt: '2026-10-11T12:00:00Z',
  engine: 'codex',
  passType: 'review',
  reviewTier: 'lean',
  trigger: 'interactive',
  round: 2,
  stance: 'convergence',
  status: 'clean',
  baseSha: 'a'.repeat(40),
  headSha: 'b'.repeat(40),
  tokenSource: 'unavailable',
  truncated: false,
  changeset: classifyFiles([
    { path: 'src/a.ts', added: 1, deleted: 0, blank: 0 },
  ]).changeset,
});

class StoreRunner extends DefaultGitHubRunner {
  calls: Array<{ args: string[]; payload: unknown }> = [];
  stores = [{ number: 80, comments: 0, locked: true, state: 'open' }];
  rows: Record<string, unknown>[] = [];
  lockFails = false;
  listFails = false;
  override runGh(args: string[], payload?: unknown): string {
    this.calls.push({ args, payload });
    const endpoint = args.at(-1)!;
    const method = args.includes('-X') ? args[args.indexOf('-X') + 1] : 'GET';
    if (endpoint === 'user') return JSON.stringify({ login: 'maintainer' });
    if (endpoint.includes('/issues?'))
      return JSON.stringify([
        this.stores.filter(
          (row) => endpoint.includes('state=all') || row.state === 'open',
        ),
      ]);
    if (endpoint.includes('/labels?'))
      return JSON.stringify([[{ name: METRICS_STORE_LABEL }]]);
    if (endpoint.endsWith('/issues') && method === 'POST') {
      const created = { number: 81, comments: 0, locked: false, state: 'open' };
      this.stores.push(created);
      return JSON.stringify(created);
    }
    if (endpoint.endsWith('/lock')) {
      if (this.lockFails) throw new Error('cannot lock');
      this.stores.find((row) => endpoint.includes(`/${row.number}/`))!.locked =
        true;
      return '';
    }
    if (endpoint.includes('/comments?')) {
      if (this.listFails) throw new Error('unreadable');
      return JSON.stringify([
        this.rows.filter((row) =>
          endpoint.includes(`/issues/${row['issue']}/`),
        ),
      ]);
    }
    if (endpoint.endsWith('/comments') && method === 'POST') {
      const issue = Number(endpoint.split('/').at(-2));
      const row = {
        id: 500 + this.rows.length,
        issue,
        body: (payload as { body: string }).body,
        user: { login: 'maintainer' },
        author_association: 'MEMBER',
        created_at: record.emittedAt,
        updated_at: record.emittedAt,
      };
      this.rows.push(row);
      this.stores.find((store) => store.number === issue)!.comments++;
      return JSON.stringify(row);
    }
    if (endpoint.includes('/issues/comments/')) {
      const row = this.rows.find(
        (item) => item['id'] === Number(endpoint.split('/').at(-1)),
      )!;
      if (method === 'PATCH') Object.assign(row, payload);
      return JSON.stringify(row);
    }
    if (/\/issues\/\d+$/.test(endpoint)) {
      const issue = this.stores.find(
        (row) => row.number === Number(endpoint.split('/').at(-1)),
      )!;
      if (method === 'PATCH') Object.assign(issue, payload);
      return JSON.stringify(issue);
    }
    throw new Error(`unexpected request: ${args.join(' ')}`);
  }
}

afterEach(() => {
  vi.mocked(fs.readFileSync).mockReset();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  resetGitHubRunner();
});

function fixture(): StoreRunner {
  vi.stubEnv('LOOM_REVIEW_TELEMETRY_STORE', 'issue');
  const runner = new StoreRunner();
  setGitHubRunner(runner);
  return runner;
}

describe('metrics store lifecycle and routing', () => {
  it('reads the installed sidecar and lets an explicit environment override it', () => {
    vi.stubEnv('LOOM_REVIEW_TELEMETRY_STORE', '');
    vi.mocked(fs.readFileSync).mockReturnValue(
      '{"LOOM_REVIEW_TELEMETRY_STORE":"issue"}',
    );
    expect(telemetryStore()).toBe('issue');
    vi.stubEnv('LOOM_REVIEW_TELEMETRY_STORE', 'pr');
    expect(telemetryStore()).toBe('pr');
  });

  it('retains recovery on invalid sidecar configuration without an API write', () => {
    const runner = fixture();
    vi.mocked(fs.readFileSync).mockReturnValue(
      '{"LOOM_REVIEW_TELEMETRY_STORE":"typo"}',
    );
    const outcome = emitTelemetry({
      record,
      sink: configuredTelemetrySink({ repo, pr: 7 }),
    });
    expect(outcome).toMatchObject({ emitted: false, recoveryRecord: record });
    expect(runner.calls).toEqual([]);
  });
  it('retains the legacy default and rejects unknown destinations', () => {
    vi.stubEnv('LOOM_REVIEW_TELEMETRY_STORE', '');
    expect(telemetryStore()).toBe('pr');
    vi.stubEnv('LOOM_REVIEW_TELEMETRY_STORE', 'typo');
    expect(() => telemetryStore()).toThrow('must be pr or issue');
  });

  it.each(['absent', 'unlocked', 'duplicate', 'full'] as const)(
    'skips %s stores, retains recovery, and never writes a PR comment',
    (kind) => {
      const runner = fixture();
      if (kind === 'absent') runner.stores = [];
      if (kind === 'unlocked') runner.stores[0]!.locked = false;
      if (kind === 'duplicate')
        runner.stores.push({ ...runner.stores[0]!, number: 81 });
      if (kind === 'full') runner.stores[0]!.comments = 2500;
      const result = emitTelemetry({
        record,
        sink: configuredTelemetrySink({ repo, pr: 7 }),
      });
      expect(result.emitted).toBe(false);
      expect(result.recoveryRecord).toEqual(record);
      expect(runner.calls.every(({ args }) => !args.includes('-X'))).toBe(true);
    },
  );

  it('writes once to the store, recognizes retries, and enriches only that record', () => {
    const runner = fixture();
    const sink = configuredTelemetrySink({ repo, pr: 7 });
    expect(emitTelemetry({ record, sink }).sink).toBe('store-issue');
    expect(emitTelemetry({ record, sink }).emitted).toBe(true);
    expect(runner.rows).toHaveLength(1);
    expect(
      telemetryRecorded({ repo, pr: 7, idempotencyKey: record.idempotencyKey }),
    ).toBe(true);
    expect(
      enrichTelemetryDuration({
        repo,
        pr: 7,
        idempotencyKey: record.idempotencyKey,
        engine: record.engine,
        round: record.round,
        base: record.baseSha,
        head: record.headSha,
        durationSeconds: 12,
      }).emitted,
    ).toBe(true);
    expect(String(runner.rows[0]!['body'])).toContain('"durationSeconds": 12');
    expect(
      runner.calls.some(({ args }) => args.join(' ').includes('/issues/7/')),
    ).toBe(false);
  });

  it('warns near capacity', () => {
    const runner = fixture();
    runner.stores[0]!.comments = 2000;
    const warn = vi.spyOn(process.stderr, 'write').mockReturnValue(true);
    expect(activeMetricsStore(repo).number).toBe(80);
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining('metrics-rotate'),
    );
  });

  it('sets up explicitly, verifies locking, and keeps setup idempotent', () => {
    const runner = fixture();
    runner.stores = [];
    expect(setupMetricsStore(repo).number).toBe(81);
    expect(setupMetricsStore(repo).number).toBe(81);
    expect(
      runner.calls.filter(({ args }) => args.includes('POST')),
    ).toHaveLength(1);
  });

  it('rotates without deleting old records and exports closed stores', () => {
    const runner = fixture();
    emitTelemetry({ record, sink: configuredTelemetrySink({ repo, pr: 7 }) });
    expect(setupMetricsStore(repo, true).number).toBe(81);
    expect(runner.stores[0]!.state).toBe('closed');
    expect(metricsStores(repo, 'all')).toHaveLength(2);
    const lines: string[] = [];
    const trailer = exportTelemetry({
      repo,
      sources: [storeIssueSource(repo)],
      write: (line) => lines.push(line),
      warn: () => {},
    });
    expect(trailer.complete).toBe(true);
    expect(trailer.records).toBe(1);
    expect(JSON.parse(lines[0]!).source.issue).toBe(80);
  });

  it('preserves the previous active store when locking a replacement fails', () => {
    const runner = fixture();
    runner.lockFails = true;
    expect(() => setupMetricsStore(repo, true)).toThrow('cannot lock');
    expect(activeMetricsStore(repo).number).toBe(80);
    expect(runner.stores[1]!.state).toBe('closed');
  });

  it('fails closed on incomplete export and reports missing measurements honestly', () => {
    const runner = fixture();
    emitTelemetry({ record, sink: configuredTelemetrySink({ repo, pr: 7 }) });
    const lines: ExportLine[] = [];
    const trailer = exportTelemetry({
      repo,
      sources: [storeIssueSource(repo)],
      write: (line) => {
        const parsed = JSON.parse(line) as ExportLine;
        if (parsed.record) lines.push(parsed);
      },
      warn: () => {},
    });
    expect(metricsReport(lines, trailer)).toContain(
      'input=0 (+1 unknown measurements)',
    );
    expect(metricsReport(lines, trailer)).toContain('max review round 2');
    runner.listFails = true;
    expect(
      exportTelemetry({
        repo,
        sources: [storeIssueSource(repo)],
        write: () => {},
        warn: () => {},
      }).complete,
    ).toBe(false);
  });

  it('filters selected records without hiding unread comments from completeness accounting', () => {
    fixture();
    const row = {
      id: 1,
      body: buildTelemetryBody(record),
      author_association: 'MEMBER',
      created_at: record.emittedAt,
      updated_at: record.emittedAt,
    };
    const write = vi.fn();
    const result = exportTelemetry({
      repo,
      sources: [
        () => [
          {
            kind: 'store-issue',
            issue: 80,
            commentsReported: 2,
            comments: [row],
          },
        ],
      ],
      select: () => false,
      write,
      warn: () => {},
    });
    expect(result).toMatchObject({
      records: 0,
      excludedBySelection: 1,
      complete: false,
    });
    expect(write).toHaveBeenCalledTimes(1);
  });
});
