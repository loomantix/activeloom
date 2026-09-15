import { beforeEach, describe, expect, it } from 'vitest';
import {
  buildTelemetryBody,
  buildTelemetryRecord,
  classifyFiles,
  emitTelemetry,
  excludeTelemetryComments,
  isTelemetryComment,
  matchTelemetry,
  prCommentSink,
  telemetryIdempotencyKey,
  validateTelemetryRecord,
  type BuildTelemetryParams,
  type TelemetryRecord,
  type TelemetrySink,
} from '../index.js';
import {
  DefaultGitHubRunner,
  resetGitHubRunner,
  setGitHubRunner,
} from '../github.js';

const BASE = '2'.repeat(40);
const HEAD = '1'.repeat(40);
const DIGEST = 'a'.repeat(64);

const CHANGESET = classifyFiles([
  { path: 'src/a.ts', added: 20, deleted: 6, blank: 2 },
  { path: 'src/__tests__/a.test.ts', added: 10, deleted: 0, blank: 0 },
]).changeset;

function params(
  overrides: Partial<BuildTelemetryParams> = {},
): BuildTelemetryParams {
  return {
    emittedAt: '2026-08-20T05:12:33Z',
    repo: 'owner/repo',
    pr: 123,
    engine: 'claude',
    engineVersion: '2.1.237',
    passType: 'review',
    reviewTier: 'deep',
    trigger: 'interactive',
    round: 3,
    stance: 'convergence',
    status: 'changed',
    baseSha: BASE,
    headSha: HEAD,
    promptStackSha256: DIGEST,
    repoInstructionsSha256: DIGEST,
    tokenSource: 'session-log-delta',
    tokens: [
      {
        model: 'claude-opus-5',
        effort: 'high',
        input: 12_000,
        output: 3_400,
        cacheRead: 480_000,
        cacheWrite: 22_000,
        reasoning: 900,
      },
    ],
    truncated: false,
    durationSeconds: 512,
    changeset: CHANGESET,
    findings: {
      posted: 2,
      bySeverityAndOutcome: {
        blocking: { validFixed: 0, validDeferred: 0, invalidDismissed: 0 },
        major: { validFixed: 1, validDeferred: 0, invalidDismissed: 0 },
        minor: { validFixed: 0, validDeferred: 1, invalidDismissed: 0 },
        nit: { validFixed: 0, validDeferred: 0, invalidDismissed: 0 },
      },
      chainInducedRegressions: 1,
    },
    ...overrides,
  };
}

function roundTrip(record: TelemetryRecord): TelemetryRecord {
  return validateTelemetryRecord(JSON.parse(JSON.stringify(record)));
}

describe('buildTelemetryRecord', () => {
  it('assembles a record that survives serialisation unchanged', () => {
    const record = buildTelemetryRecord(params());
    expect(roundTrip(record)).toEqual(record);
    expect(record.version).toBe(1);
    expect(record.changeset.linesChanged.app).toBe(24);
  });

  it('derives an idempotency key that separates pass types', () => {
    const review = buildTelemetryRecord(
      params({ passType: 'review', round: 1 }),
    );
    const refactor = buildTelemetryRecord(
      params({ passType: 'refactor', round: 1, status: 'clean' }),
    );
    expect(review.idempotencyKey).not.toBe(refactor.idempotencyKey);
    expect(review.idempotencyKey).toBe(
      telemetryIdempotencyKey({
        repo: 'owner/repo',
        pr: 123,
        engine: 'claude',
        passType: 'review',
        round: 1,
        headSha: HEAD,
      }),
    );
  });

  it('honours a caller-supplied idempotency key', () => {
    const record = buildTelemetryRecord(
      params({ idempotencyKey: 'owner/repo:123:claude:review:3:head' }),
    );
    expect(record.idempotencyKey).toBe('owner/repo:123:claude:review:3:head');
  });

  it('separates restarted runs at the same head while keeping retries stable', () => {
    const first = buildTelemetryRecord(params({ runId: 'a'.repeat(64) }));
    const retry = buildTelemetryRecord(params({ runId: 'a'.repeat(64) }));
    const restarted = buildTelemetryRecord(params({ runId: 'b'.repeat(64) }));
    expect(first.idempotencyKey).toBe(retry.idempotencyKey);
    expect(first.idempotencyKey).not.toBe(restarted.idempotencyKey);
  });

  it('defaults finding counts to zero but never token counts', () => {
    const record = buildTelemetryRecord(
      params({
        findings: undefined,
        tokens: [{ model: 'claude-opus-5', input: 10 }],
      }),
    );
    expect(record.findings.posted).toBe(0);
    expect(record.findings.bySeverityAndOutcome.nit.validFixed).toBe(0);
    expect(record.tokens[0]?.input).toBe(10);
    expect(record.tokens[0]?.output).toBeNull();
    expect(record.tokens[0]?.cacheWrite).toBeNull();
  });
});

describe('unavailable never collapses into zero', () => {
  it('keeps null and 0 distinguishable through serialisation', () => {
    const record = buildTelemetryRecord(
      params({
        tokens: [
          {
            model: 'gemini-3-flash-preview',
            input: 15_279,
            output: 27,
            cacheRead: 0,
            cacheWrite: null,
            reasoning: 26,
          },
        ],
      }),
    );
    const serialised = JSON.stringify(record);
    expect(serialised).toContain('"cacheRead":0');
    expect(serialised).toContain('"cacheWrite":null');

    const parsed = roundTrip(record);
    expect(parsed.tokens[0]?.cacheRead).toBe(0);
    expect(parsed.tokens[0]?.cacheWrite).toBeNull();
    expect(parsed.tokens[0]?.cacheWrite).not.toBe(0);
  });

  it('rejects a missing count rather than serialising it away', () => {
    const record = JSON.parse(
      JSON.stringify(buildTelemetryRecord(params())),
    ) as Record<string, unknown>;
    const tokens = record['tokens'] as Array<Record<string, unknown>>;
    delete tokens[0]!['cacheWrite'];
    expect(() => validateTelemetryRecord(record)).toThrow(
      /tokens\[\]\.cacheWrite/,
    );
  });

  it('refuses to record zero tokens for a pass with no usage data', () => {
    expect(() =>
      buildTelemetryRecord(
        params({
          tokenSource: 'unavailable',
          tokens: [{ model: 'copilot', input: 0, output: 0 }],
        }),
      ),
    ).toThrow(/unavailable cannot carry token buckets/);
  });

  it('records a findings-only pass as unavailable rather than empty', () => {
    const record = buildTelemetryRecord(
      params({ tokenSource: 'unavailable', tokens: [], passType: 'hosted' }),
    );
    expect(record.tokens).toEqual([]);
    expect(() =>
      buildTelemetryRecord(params({ tokenSource: 'stream-json', tokens: [] })),
    ).toThrow(/must record tokenSource unavailable/);
  });

  it('leaves comment null through the record boundary', () => {
    const record = buildTelemetryRecord(params());
    expect(JSON.stringify(record)).toContain('"comment":null');
    expect(roundTrip(record).changeset.linesChanged.comment).toBeNull();
  });
});

describe('validateTelemetryRecord', () => {
  it('accepts an engine token no release has heard of', () => {
    const record = buildTelemetryRecord(params({ engine: 'grok-5' }));
    expect(record.engine).toBe('grok-5');
  });

  it('rejects an engine token that is not well formed', () => {
    expect(() =>
      buildTelemetryRecord(params({ engine: 'Claude Opus' })),
    ).toThrow(/telemetry engine must match/);
  });

  it('carries provider-specific integer buckets alongside the canonical five', () => {
    const record = buildTelemetryRecord(
      params({
        tokens: [
          {
            model: 'claude-opus-5',
            input: 1,
            providerBuckets: { tool_result_tokens: 42 },
          },
        ],
      }),
    );
    expect(record.tokens[0]?.providerBuckets).toEqual({
      tool_result_tokens: 42,
    });
  });

  it('refuses a provider bucket that restates a canonical one', () => {
    expect(() =>
      buildTelemetryRecord(
        params({
          tokens: [
            { model: 'claude-opus-5', input: 1, providerBuckets: { input: 5 } },
          ],
        }),
      ),
    ).toThrow(/must not restate the canonical bucket/);
  });

  it('refuses a provider bucket key that could carry prose', () => {
    expect(() =>
      buildTelemetryRecord(
        params({
          tokens: [
            {
              model: 'claude-opus-5',
              input: 1,
              providerBuckets: { 'finding title': 1 },
            },
          ],
        }),
      ),
    ).toThrow(/providerBuckets key must match/);
  });

  it('accepts an open lens token and requires lanes to be absent, not empty', () => {
    const record = buildTelemetryRecord(
      params({ lanes: [{ lens: 'silent-failure-hunter', output: 12 }] }),
    );
    expect(record.lanes?.[0]?.lens).toBe('silent-failure-hunter');
    expect(record.lanes?.[0]?.input).toBeNull();

    const withoutLanes = buildTelemetryRecord(params());
    expect('lanes' in withoutLanes).toBe(false);
    expect(() => validateTelemetryRecord({ ...record, lanes: [] })).toThrow(
      /absent rather than empty/,
    );
  });

  it('preserves a field a newer writer added', () => {
    const record = buildTelemetryRecord(params());
    const forward = validateTelemetryRecord({
      ...JSON.parse(JSON.stringify(record)),
      cacheHitRatio: 0.94,
    }) as TelemetryRecord & { cacheHitRatio?: number };
    expect(forward.cacheHitRatio).toBe(0.94);
    expect(buildTelemetryBody(forward)).not.toContain('cacheHitRatio');
  });

  it('refuses to render a record with the wrong protocol discriminator', () => {
    const record = buildTelemetryRecord(params());
    expect(() =>
      buildTelemetryBody({
        ...record,
        version: 2,
      } as unknown as TelemetryRecord),
    ).toThrow(/version must be 1/);
  });

  it('rejects a timestamp that is not an RFC 3339 UTC second', () => {
    expect(() =>
      buildTelemetryRecord(params({ emittedAt: '2026-08-20T05:12:33.412Z' })),
    ).toThrow(/emittedAt/);
    expect(() =>
      buildTelemetryRecord(params({ emittedAt: '2026-08-20T05:12:33-04:00' })),
    ).toThrow(/emittedAt/);
    expect(() =>
      buildTelemetryRecord(params({ emittedAt: '2026-02-31T05:12:33Z' })),
    ).toThrow(/emittedAt/);
  });

  it('rejects a repo that is not owner/name', () => {
    expect(() => buildTelemetryRecord(params({ repo: 'platform' }))).toThrow(
      /owner\/name/,
    );
  });

  it('rejects a range that is not pinned to full SHAs', () => {
    expect(() => buildTelemetryRecord(params({ headSha: 'abc1234' }))).toThrow(
      /full commit SHAs/,
    );
  });

  it('rejects dispositions that exceed the findings posted', () => {
    expect(() =>
      buildTelemetryRecord(
        params({
          findings: {
            posted: 1,
            bySeverityAndOutcome: {
              blocking: {
                validFixed: 0,
                validDeferred: 0,
                invalidDismissed: 0,
              },
              major: { validFixed: 2, validDeferred: 0, invalidDismissed: 0 },
              minor: { validFixed: 0, validDeferred: 0, invalidDismissed: 0 },
              nit: { validFixed: 0, validDeferred: 0, invalidDismissed: 0 },
            },
            chainInducedRegressions: 0,
          },
        }),
      ),
    ).toThrow(/exceed the findings posted/);
  });

  it('records a skip, but not one carrying reviewable work', () => {
    const skipped = buildTelemetryRecord(
      params({
        status: 'skipped',
        changeset: classifyFiles([{ path: 'README.md', added: 8, deleted: 1 }])
          .changeset,
        tokens: [{ model: 'claude-opus-5', input: 8_000, output: 120 }],
        findings: undefined,
      }),
    );
    expect(skipped.changeset.linesChanged.docsConfig).toBe(9);
    expect(skipped.tokens[0]?.input).toBe(8_000);

    expect(() => buildTelemetryRecord(params({ status: 'skipped' }))).toThrow(
      /skipped pass cannot carry review-significant files/,
    );

    const lockfile = classifyFiles([
      { path: 'pnpm-lock.yaml', added: 10, deleted: 2 },
    ]).changeset;
    expect(() =>
      buildTelemetryRecord(params({ status: 'skipped', changeset: lockfile })),
    ).toThrow(/skipped pass cannot carry review-significant files/);

    expect(() =>
      buildTelemetryRecord(
        params({
          status: 'skipped',
          changeset: { ...CHANGESET, reviewSignificantFiles: 0 },
        }),
      ),
    ).toThrow(/must cover app\/test files/);

    expect(() =>
      buildTelemetryRecord(
        params({ changeset: { ...CHANGESET, reviewSignificantFiles: 3 } }),
      ),
    ).toThrow(/not exceed total files/);
  });

  it('rejects two buckets for the same model and effort', () => {
    expect(() =>
      buildTelemetryRecord(
        params({
          tokens: [
            { model: 'claude-opus-5', effort: 'high', input: 1 },
            { model: 'claude-opus-5', effort: 'high', input: 2 },
          ],
        }),
      ),
    ).toThrow(/one bucket per model and effort/);
  });

  it('does not collide distinct model and effort token pairs', () => {
    const record = buildTelemetryRecord(
      params({
        tokens: [
          { model: 'a:b', effort: 'c', input: 1 },
          { model: 'a', effort: 'b:c', input: 1 },
          { model: 'a', effort: 'null', input: 1 },
          { model: 'a', effort: null, input: 1 },
        ],
      }),
    );
    expect(record.tokens).toHaveLength(4);
  });
});

describe('marker body', () => {
  it('round-trips a record through the comment body', () => {
    const record = buildTelemetryRecord(params());
    const body = buildTelemetryBody(record);
    expect(body.startsWith('<!-- local-review-telemetry:v1 -->')).toBe(true);
    expect(matchTelemetry(body)).toEqual(record);
  });

  it('reports a body carrying no telemetry as absent', () => {
    expect(matchTelemetry('<!-- local-review-roster:v2 ... -->')).toBeNull();
  });

  it('fails on a corrupted record rather than losing it silently', () => {
    const body = ['<!-- local-review-telemetry:v1 -->', '', 'not json'].join(
      '\n',
    );
    expect(() => matchTelemetry(body)).toThrow(/not valid JSON/);
  });

  it('fails on an unsupported telemetry version', () => {
    expect(() =>
      matchTelemetry('<!-- local-review-telemetry:v2 -->\n\n{}'),
    ).toThrow(/unsupported version/);
  });

  it('fails on two telemetry markers in one comment', () => {
    const record = buildTelemetryRecord(params());
    const body = `${buildTelemetryBody(record)}\n${buildTelemetryBody(record)}`;
    expect(() => matchTelemetry(body)).toThrow(/more than one/);
  });

  it('fails when an unsupported marker precedes a v1 marker', () => {
    const record = buildTelemetryRecord(params());
    const body = `<!-- local-review-telemetry:v2 -->\n${buildTelemetryBody(record)}`;
    expect(() => matchTelemetry(body)).toThrow(/more than one/);
  });
});

describe('reviewer context exclusion', () => {
  it('filters telemetry by marker prefix, not by known marker', () => {
    const rows = [
      { body: buildTelemetryBody(buildTelemetryRecord(params())) },
      { body: '<!-- local-review-telemetry:v9 -->\npayload' },
      { body: '<!-- local-review-complete:v3 engine=claude round=1 -->' },
      { body: 'a plain review comment' },
    ];
    expect(isTelemetryComment(rows[0]!.body)).toBe(true);
    expect(isTelemetryComment(rows[2]!.body)).toBe(false);
    expect(excludeTelemetryComments(rows).map((row) => row.body)).toEqual([
      rows[2]!.body,
      rows[3]!.body,
    ]);
  });
});

describe('emitTelemetry', () => {
  const record = buildTelemetryRecord(params());

  it('reports a sink failure instead of failing the review', () => {
    const sink: TelemetrySink = {
      name: 'exploding',
      emit() {
        throw new Error('GitHub is having a day');
      },
    };
    expect(emitTelemetry({ record, sink })).toEqual({
      emitted: false,
      sink: 'exploding',
      reference: null,
      idempotencyKey: record.idempotencyKey,
      error: 'GitHub is having a day',
    });
  });

  it('hands the rendered body to whatever sink it is given', () => {
    const seen: string[] = [];
    const sink: TelemetrySink = {
      name: 'memory',
      emit({ body }) {
        seen.push(body);
        return { sink: 'memory', reference: 'note-1' };
      },
    };
    const result = emitTelemetry({ record, sink });
    expect(result).toMatchObject({ emitted: true, reference: 'note-1' });
    expect(matchTelemetry(seen[0]!)).toEqual(record);
  });
});

describe('prCommentSink', () => {
  const record = buildTelemetryRecord(params());

  class MockRunner extends DefaultGitHubRunner {
    public authenticatedActor = 'test-actor';
    public postActor: string | null = null;
    public issueComments: Array<Record<string, unknown>> = [];
    public actorSequence: string[] = [];
    public posts = 0;
    public calls: string[] = [];
    public deleteFails = false;

    runGh(args: string[], payload?: unknown): string {
      const cmd = args.join(' ');
      this.calls.push(cmd);
      if (cmd.startsWith('api user')) {
        return JSON.stringify({
          login: this.actorSequence.shift() ?? this.authenticatedActor,
        });
      }
      if (cmd.includes('/issues/') && cmd.includes('/comments?per_page=100')) {
        return JSON.stringify([this.issueComments]);
      }
      if (cmd.includes('-X POST') && cmd.includes('/issues/')) {
        this.posts += 1;
        const row = {
          id: 900 + this.posts,
          user: { login: this.postActor ?? this.authenticatedActor },
          body: (payload as { body: string }).body,
        };
        this.issueComments.push(row);
        return JSON.stringify(row);
      }
      if (cmd.includes('-X DELETE') && cmd.includes('/issues/comments/')) {
        if (this.deleteFails) {
          throw new Error('delete failed');
        }
        const id = parseInt(args.at(-1)!.split('/').pop()!, 10);
        this.issueComments = this.issueComments.filter(
          (row) => row['id'] !== id,
        );
        return '';
      }
      if (cmd.includes('/issues/comments/')) {
        const id = parseInt(args[1]!.split('/').pop()!, 10);
        return JSON.stringify(
          this.issueComments.find((row) => row['id'] === id) ?? {},
        );
      }
      throw new Error(`unexpected gh call: ${cmd}`);
    }
  }

  /** Classify one recorded `gh` invocation so a test can pin the exact order. */
  function callKind(call: string): string {
    if (call.startsWith('api user')) return 'user';
    if (call.includes('/comments?per_page=100')) return 'list';
    if (call.includes('-X POST')) return 'post';
    if (call.includes('-X DELETE')) return 'delete';
    if (call.includes('/issues/comments/')) return 'get';
    return call;
  }

  beforeEach(() => {
    resetGitHubRunner();
  });

  it('posts one comment per pass and replays an identical key', () => {
    const runner = new MockRunner();
    setGitHubRunner(runner);
    const sink = prCommentSink({ repo: 'owner/repo', pr: 123 });

    const first = emitTelemetry({ record, sink });
    expect(first).toMatchObject({ emitted: true, sink: 'pr-comment' });
    expect(runner.posts).toBe(1);
    // Assert the whole sequence, not offsets around it. Relative assertions are
    // satisfied by any neighbouring identity read, so adding or removing a pin
    // silently retargets every drift test below instead of failing here.
    expect(runner.calls.map(callKind)).toEqual([
      'user', // seed the pin
      'user', // pre-fetch live pin
      'list',
      'user', // post-fetch live re-pin
      'user', // pre-POST live pin
      'post',
      'user', // pre-readback live pin
      'get',
      'user', // post-readback live pin
    ]);

    const replay = emitTelemetry({ record, sink });
    expect(replay.reference).toBe(first.reference);
    expect(runner.posts).toBe(1);

    const nextRound = buildTelemetryRecord(params({ round: 4 }));
    emitTelemetry({ record: nextRound, sink });
    expect(runner.posts).toBe(2);
  });

  it('ignores an identical replay marker authored by another actor', () => {
    const runner = new MockRunner();
    runner.issueComments.push({
      id: 700,
      user: { login: 'untrusted-commenter' },
      body: buildTelemetryBody(record),
    });
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({ repo: 'owner/repo', pr: 123 }),
    });

    expect(result).toMatchObject({ emitted: true, reference: '901' });
    expect(runner.posts).toBe(1);
  });

  it('ignores a conflicting replay marker authored by another actor', () => {
    const runner = new MockRunner();
    const conflict = buildTelemetryRecord(params({ durationSeconds: 999 }));
    runner.issueComments.push({
      id: 700,
      user: { login: 'untrusted-commenter' },
      body: buildTelemetryBody(conflict),
    });
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({ repo: 'owner/repo', pr: 123 }),
    });

    expect(result).toMatchObject({ emitted: true, reference: '901' });
    expect(runner.posts).toBe(1);
  });

  it('accepts a replay only when the authenticated actor owns it', () => {
    const runner = new MockRunner();
    runner.issueComments.push({
      id: 700,
      user: { login: runner.authenticatedActor },
      body: buildTelemetryBody(record),
    });
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({ repo: 'owner/repo', pr: 123 }),
    });

    expect(result).toMatchObject({ emitted: true, reference: '700' });
    expect(runner.posts).toBe(0);
  });

  it('rejects replay when credentials change during comment listing', () => {
    const runner = new MockRunner();
    runner.issueComments.push({
      id: 700,
      user: { login: runner.authenticatedActor },
      body: buildTelemetryBody(record),
    });
    runner.actorSequence = [
      runner.authenticatedActor,
      runner.authenticatedActor,
      'different-actor',
    ];
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({ repo: 'owner/repo', pr: 123 }),
    });

    expect(result).toMatchObject({
      emitted: false,
      reference: null,
      error: expect.stringMatching(/authenticated GitHub actor changed/),
    });
    expect(runner.posts).toBe(0);
  });

  it('rejects credential drift before posting', () => {
    const runner = new MockRunner();
    runner.actorSequence = [
      runner.authenticatedActor,
      runner.authenticatedActor,
      runner.authenticatedActor,
      'different-actor',
    ];
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({ repo: 'owner/repo', pr: 123 }),
    });

    expect(result).toMatchObject({
      emitted: false,
      reference: null,
      error: expect.stringMatching(/authenticated GitHub actor changed/),
    });
    expect(runner.posts).toBe(0);
  });

  it('rejects a newly posted comment not owned by the pinned actor', () => {
    const runner = new MockRunner();
    runner.postActor = 'different-actor';
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({ repo: 'owner/repo', pr: 123 }),
    });

    expect(result).toMatchObject({
      emitted: false,
      reference: null,
      error: expect.stringMatching(/could not verify PR comment/),
    });
    expect(runner.posts).toBe(1);
    expect(runner.issueComments).toEqual([]);
  });

  it('rejects credential drift after comment readback', () => {
    const runner = new MockRunner();
    runner.actorSequence = [
      runner.authenticatedActor,
      runner.authenticatedActor,
      runner.authenticatedActor,
      runner.authenticatedActor,
      runner.authenticatedActor,
      'different-actor',
    ];
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({ repo: 'owner/repo', pr: 123 }),
    });

    expect(result).toMatchObject({
      emitted: false,
      reference: null,
      error: expect.stringMatching(/authenticated GitHub actor changed/),
    });
    expect(runner.posts).toBe(1);
    expect(runner.issueComments).toEqual([]);
  });

  it('reports an unverifiable rollback without hiding a foreign row', () => {
    const runner = new MockRunner();
    runner.postActor = 'different-actor';
    runner.deleteFails = true;
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({ repo: 'owner/repo', pr: 123 }),
    });

    expect(result).toMatchObject({
      emitted: false,
      reference: null,
      error: expect.stringMatching(/rollback could not be verified/),
    });
    // The residue must be locatable from the message alone: `emitTelemetry`
    // reports `error.message` and never walks the cause chain.
    expect(result.error).toContain('comment 901 on owner/repo#123');
    expect(result.error).toMatch(
      /verification failed with: .*could not verify/,
    );
    expect(runner.issueComments).toHaveLength(1);
    expect(runner.issueComments[0]).toMatchObject({
      id: 901,
      user: { login: 'different-actor' },
    });
  });

  it('fails closed when the caller pins an actor the session is not', () => {
    const runner = new MockRunner();
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({
        repo: 'owner/repo',
        pr: 123,
        actor: 'somebody-else',
      }),
    });

    expect(result).toMatchObject({
      emitted: false,
      reference: null,
      error: expect.stringMatching(/authenticated GitHub actor changed/),
    });
    expect(runner.posts).toBe(0);
  });

  it('emits under a caller pin that matches the session', () => {
    const runner = new MockRunner();
    setGitHubRunner(runner);

    const result = emitTelemetry({
      record,
      sink: prCommentSink({
        repo: 'owner/repo',
        pr: 123,
        actor: runner.authenticatedActor,
      }),
    });

    expect(result).toMatchObject({ emitted: true, reference: '901' });
    expect(runner.posts).toBe(1);
  });

  it('reports a same-key record whose measurements conflict', () => {
    const runner = new MockRunner();
    setGitHubRunner(runner);
    const sink = prCommentSink({ repo: 'owner/repo', pr: 123 });
    expect(emitTelemetry({ record, sink }).emitted).toBe(true);

    const conflict = buildTelemetryRecord(params({ durationSeconds: 999 }));
    expect(emitTelemetry({ record: conflict, sink })).toMatchObject({
      emitted: false,
      error: expect.stringMatching(/idempotency key conflicts/),
    });
    expect(runner.posts).toBe(1);
  });

  it('replays semantically identical buckets and lanes in another order', () => {
    const runner = new MockRunner();
    setGitHubRunner(runner);
    const sink = prCommentSink({ repo: 'owner/repo', pr: 123 });
    const first = buildTelemetryRecord(
      params({
        tokens: [
          {
            model: 'model-b',
            input: 2,
            providerBuckets: { zeta: 2, alpha: 1 },
          },
          { model: 'model-a', input: 1 },
        ],
        lanes: [
          { lens: 'security', model: 'model-b', output: 2 },
          { lens: 'correctness', model: 'model-a', output: 1 },
        ],
      }),
    );
    const replay = buildTelemetryRecord(
      params({
        tokens: [
          { model: 'model-a', input: 1 },
          {
            model: 'model-b',
            input: 2,
            providerBuckets: { alpha: 1, zeta: 2 },
          },
        ],
        lanes: [
          { lens: 'correctness', model: 'model-a', output: 1 },
          { lens: 'security', model: 'model-b', output: 2 },
        ],
      }),
    );
    expect(emitTelemetry({ record: first, sink }).emitted).toBe(true);
    expect(emitTelemetry({ record: replay, sink })).toMatchObject({
      emitted: true,
      reference: '901',
    });
    expect(runner.posts).toBe(1);
  });

  it('ignores malformed and future records while looking for a replay', () => {
    const runner = new MockRunner();
    runner.issueComments.push(
      {
        id: 700,
        user: { login: runner.authenticatedActor },
        body: '<!-- local-review-telemetry:v1 -->\nnot-json',
      },
      {
        id: 701,
        user: { login: runner.authenticatedActor },
        body: '<!-- local-review-telemetry:v2 -->\n{}',
      },
    );
    setGitHubRunner(runner);
    const result = emitTelemetry({
      record,
      sink: prCommentSink({ repo: 'owner/repo', pr: 123 }),
    });
    expect(result.emitted).toBe(true);
    expect(runner.posts).toBe(1);
  });
});
