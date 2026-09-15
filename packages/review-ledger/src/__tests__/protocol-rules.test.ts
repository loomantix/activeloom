import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  assertActor,
  assertThreadScope,
  FINDING_V3_RE,
  loadHistoricalCommentIds,
  loadReviewThreads,
  matchProtocol,
  pairDispositions,
  sameRoundDispositions,
  threadProtocolRecords,
  verifyLedger,
  verifyThreadDispositions,
} from '../index.js';
import {
  assertLiveActor,
  resetGitHubRunner,
  setGitHubRunner,
} from '../github.js';
import type {
  DispositionV3Match,
  FindingV3Match,
  GitHubReviewThreadNode,
  GitHubRunner,
} from '../types.js';

const ACTOR = 'review-bot';
const HEAD = 'a'.repeat(40);
const LATER_HEAD = 'b'.repeat(40);

const sha = (value: string): string =>
  createHash('sha256').update(value, 'utf8').digest('hex');

function findingMarker(options: {
  fingerprint: string;
  occurrence?: number;
  severity?: string;
  content: string;
  head?: string;
  round?: number;
}): string {
  return (
    `<!-- local-review:v3 engine=claude round=${options.round ?? 1} ` +
    `head=${options.head ?? HEAD} fingerprint=${options.fingerprint} ` +
    `occurrence=${options.occurrence ?? 1} severity=${options.severity ?? 'major'} ` +
    `lens=code-reviewer content-sha256=${sha(options.content)} -->`
  );
}

function dispositionMarker(options: {
  fingerprint: string;
  occurrence?: number;
  outcome?: string;
  content: string;
  head?: string;
  round?: number;
}): string {
  return (
    `<!-- local-review-disposition:v3 engine=claude round=${options.round ?? 1} ` +
    `head=${options.head ?? HEAD} fingerprint=${options.fingerprint} ` +
    `occurrence=${options.occurrence ?? 1} outcome=${options.outcome ?? 'fixed'} ` +
    `content-sha256=${sha(options.content)} -->`
  );
}

function comment(body: string, databaseId = 1, login: string | null = ACTOR) {
  return { databaseId, body, author: login === null ? null : { login } };
}

function thread(
  bodies: Array<ReturnType<typeof comment>>,
  overrides: Partial<GitHubReviewThreadNode> = {},
): GitHubReviewThreadNode {
  return {
    id: 'PRRT_1',
    isResolved: true,
    repository: { nameWithOwner: 'loomantix/platform-oss' },
    pullRequest: { number: 7 },
    comments: { nodes: bodies, pageInfo: { hasNextPage: false } },
    ...overrides,
  } as GitHubReviewThreadNode;
}

/** A settled root finding plus its disposition, the minimum valid ledger. */
function settledThread(
  overrides: Partial<GitHubReviewThreadNode> = {},
): GitHubReviewThreadNode {
  const fc = 'the finding\n';
  const dc = 'the fix\n';
  return thread(
    [
      comment(
        `${findingMarker({ fingerprint: 'fp1', content: fc })}\n${fc}`,
        1,
      ),
      comment(
        `${dispositionMarker({ fingerprint: 'fp1', content: dc })}\n${dc}`,
        2,
      ),
    ],
    overrides,
  );
}

class StubRunner implements GitHubRunner {
  actor = ACTOR;
  threadNodes: GitHubReviewThreadNode[] = [];
  prHead = HEAD;

  runGh(args: string[]): string {
    const cmd = args.join(' ');
    if (cmd.includes('pr view') && cmd.includes('headRefOid')) {
      return this.prHead;
    }
    if (cmd.includes('graphql')) {
      return JSON.stringify([
        {
          data: {
            repository: {
              pullRequest: {
                reviewThreads: {
                  nodes: this.threadNodes,
                  pageInfo: { hasNextPage: false, endCursor: null },
                },
              },
            },
          },
        },
      ]);
    }
    return '{}';
  }

  currentActor(): string {
    return this.actor;
  }

  gitCompare(_repo: string, before: string): unknown {
    return { status: 'ahead', merge_base_commit: { sha: before } };
  }
}

let runner: StubRunner;

beforeEach(() => {
  runner = new StubRunner();
  setGitHubRunner(runner);
  delete process.env['AGENT_LOOP_REVIEW_ACTOR'];
  delete process.env['AGENT_LOOP_REVIEW_THREADS_SHA256'];
  delete process.env['AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE'];
});

afterEach(() => {
  resetGitHubRunner();
});

describe('marker layout is authenticated', () => {
  it('rejects a marker that is not the first line of the body', () => {
    const content = 'the finding\n';
    const body = `unhashed prose\n${findingMarker({ fingerprint: 'fp1', content })}\n${content}`;
    expect(() =>
      matchProtocol(body, FINDING_V3_RE, '<!-- local-review:v3'),
    ).toThrowError(/not the first complete line/);
  });

  it('rejects an empty content payload', () => {
    const body = `${findingMarker({ fingerprint: 'fp1', content: '' })}\n`;
    expect(() =>
      matchProtocol(body, FINDING_V3_RE, '<!-- local-review:v3'),
    ).toThrowError(/content is empty/);
  });

  it('rejects a content hash that does not cover the body', () => {
    const body = `${findingMarker({ fingerprint: 'fp1', content: 'a\n' })}\nb\n`;
    expect(() =>
      matchProtocol(body, FINDING_V3_RE, '<!-- local-review:v3'),
    ).toThrowError(/invalid content hash/);
  });

  it('accepts a well-formed record', () => {
    const content = 'the finding\n';
    const body = `${findingMarker({ fingerprint: 'fp1', content })}\n${content}`;
    const match = matchProtocol(body, FINDING_V3_RE, '<!-- local-review:v3');
    expect(match?.groups?.['fingerprint']).toBe('fp1');
  });
});

describe('thread snapshots are sealed', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'review-ledger-'));
  });

  const writeSnapshot = (nodes: GitHubReviewThreadNode[]): string => {
    const file = join(dir, 'threads.json');
    writeFileSync(
      file,
      JSON.stringify([
        {
          data: {
            repository: {
              pullRequest: {
                reviewThreads: {
                  nodes,
                  pageInfo: { hasNextPage: false, endCursor: null },
                },
              },
            },
          },
        },
      ]),
    );
    return file;
  };

  it('refuses a snapshot with no declared digest', () => {
    const file = writeSnapshot([settledThread()]);
    expect(() => loadReviewThreads(file)).toThrowError(
      /requires a sealed SHA-256 digest/,
    );
  });

  it('refuses a snapshot whose digest does not match', () => {
    const file = writeSnapshot([settledThread()]);
    expect(() => loadReviewThreads(file, '0'.repeat(64))).toThrowError(
      /changed after it was sealed/,
    );
  });

  it('accepts a snapshot sealed by its own digest', () => {
    const file = writeSnapshot([settledThread()]);
    const digest = createHash('sha256')
      .update(readFileSync(file))
      .digest('hex');
    expect(loadReviewThreads(file, digest)).toHaveLength(1);
  });

  it('reads the seal from the environment when no flag is passed', () => {
    const file = writeSnapshot([settledThread()]);
    process.env['AGENT_LOOP_REVIEW_THREADS_SHA256'] = '0'.repeat(64);
    expect(() => loadReviewThreads(file)).toThrowError(
      /changed after it was sealed/,
    );
  });
});

describe('threads are scoped to the requested pull request', () => {
  it('rejects a thread from another pull request', () => {
    expect(() =>
      assertThreadScope(
        [settledThread({ pullRequest: { number: 9 } })],
        'loomantix/platform-oss',
        7,
      ),
    ).toThrowError(/outside the requested PR/);
  });

  it('rejects a thread from another repository', () => {
    expect(() =>
      assertThreadScope(
        [settledThread({ repository: { nameWithOwner: 'evil/repo' } })],
        'loomantix/platform-oss',
        7,
      ),
    ).toThrowError(/outside the requested PR/);
  });

  it('rejects a thread that carries no scope at all', () => {
    expect(() =>
      assertThreadScope(
        [settledThread({ repository: undefined, pullRequest: undefined })],
        'loomantix/platform-oss',
        7,
      ),
    ).toThrowError(/outside the requested PR/);
  });
});

describe('actor identity is asserted, never supplied', () => {
  it('fails when the expected actor is not the authenticated one', () => {
    expect(() => assertActor('somebody-else')).toThrowError(
      /authenticated GitHub actor changed/,
    );
  });

  it('passes when the expected actor matches', () => {
    expect(assertActor(ACTOR)).toBe(ACTOR);
  });

  it('keeps the command actor pin separate from live boundary checks', () => {
    setGitHubRunner({
      runGh: () => JSON.stringify({ login: 'rotated-actor' }),
      currentActor: () => ACTOR,
      liveActor: () => 'rotated-actor',
    });

    expect(assertActor(ACTOR)).toBe(ACTOR);
    expect(() => assertLiveActor(ACTOR)).toThrowError(
      /authenticated GitHub actor changed/,
    );
  });

  it('refuses a runner that cannot re-resolve the live actor', () => {
    setGitHubRunner({
      runGh: () => JSON.stringify({ login: 'rotated-actor' }),
      currentActor: () => ACTOR,
    });

    expect(assertActor(ACTOR)).toBe(ACTOR);
    expect(() => assertLiveActor(ACTOR)).toThrowError(
      /cannot re-resolve the live authenticated actor/,
    );
  });

  it('honours the environment pin', () => {
    process.env['AGENT_LOOP_REVIEW_ACTOR'] = 'somebody-else';
    expect(() => assertActor()).toThrowError(
      /authenticated GitHub actor changed/,
    );
  });

  it('honours the environment pin on the live path too', () => {
    setGitHubRunner({
      runGh: () => JSON.stringify({ login: ACTOR }),
      currentActor: () => ACTOR,
      liveActor: () => ACTOR,
    });
    process.env['AGENT_LOOP_REVIEW_ACTOR'] = 'somebody-else';
    expect(() => assertLiveActor()).toThrowError(
      /authenticated GitHub actor changed/,
    );
  });

  it('refuses a present-but-empty environment pin instead of running unpinned', () => {
    process.env['AGENT_LOOP_REVIEW_ACTOR'] = '';
    expect(() => assertActor()).toThrowError(/actor/);
  });
});

describe('protocol records in a thread', () => {
  it('fails on a local-review comment with no resolvable author', () => {
    const content = 'the finding\n';
    const node = thread([
      comment(
        `${findingMarker({ fingerprint: 'fp1', content })}\n${content}`,
        1,
        null,
      ),
    ]);
    expect(() => threadProtocolRecords(node)).toThrowError(
      /could not establish local-review comment ownership/,
    );
  });

  it('fails on a malformed v3 marker rather than ignoring it', () => {
    const node = thread([
      comment('<!-- local-review:v3 engine=claude round=oops -->\nbody\n'),
    ]);
    expect(() => threadProtocolRecords(node)).toThrowError(/malformed/);
  });

  it('fails on an unrecognised local-review marker on the first line', () => {
    const node = thread([comment('<!-- local-review:v9 whatever -->\nbody\n')]);
    expect(() => threadProtocolRecords(node)).toThrowError(
      /marker is malformed or unsupported/,
    );
  });

  it('ignores comments authored by anyone else', () => {
    const content = 'the finding\n';
    const node = thread([
      comment(
        `${findingMarker({ fingerprint: 'fp1', content })}\n${content}`,
        1,
        'someone-else',
      ),
    ]);
    expect(threadProtocolRecords(node).findingsV3).toHaveLength(0);
  });
});

describe('finding and disposition pairing', () => {
  const finding = (
    fingerprint: string,
    occurrence = 1,
    severity: FindingV3Match['severity'] = 'major',
  ): FindingV3Match => ({
    engine: 'claude',
    round: 1,
    head: HEAD,
    fingerprint,
    occurrence,
    severity,
    lens: 'code-reviewer',
    contentSha: sha('x'),
  });
  const disposition = (
    fingerprint: string,
    occurrence = 1,
    outcome: DispositionV3Match['outcome'] = 'fixed',
  ): DispositionV3Match => ({
    engine: 'claude',
    round: 1,
    head: HEAD,
    fingerprint,
    occurrence,
    outcome,
    contentSha: sha('x'),
  });

  it('rejects an orphan disposition', () => {
    expect(() =>
      pairDispositions(
        [[0, finding('fp1')]],
        [
          [1, disposition('fp1')],
          [2, disposition('fp2')],
        ],
      ),
    ).toThrowError(/orphan disposition/);
  });

  it('rejects a finding with no disposition', () => {
    expect(() => pairDispositions([[0, finding('fp1')]], [])).toThrowError(
      /lacks exactly one matching disposition/,
    );
  });

  it('rejects a recurrence opened before the prior disposition', () => {
    expect(() =>
      pairDispositions(
        [
          [0, finding('fp1', 1)],
          [1, finding('fp1', 2)],
        ],
        [
          [2, disposition('fp1', 1)],
          [3, disposition('fp1', 2)],
        ],
      ),
    ).toThrowError(/recurrence was opened before the prior disposition/);
  });

  it('accepts a well-ordered occurrence history', () => {
    const matched = pairDispositions(
      [
        [0, finding('fp1', 1)],
        [2, finding('fp1', 2)],
      ],
      [
        [1, disposition('fp1', 1)],
        [3, disposition('fp1', 2)],
      ],
    );
    expect(matched).toHaveLength(2);
  });

  it('tracks fixed non-blocking findings separately', () => {
    const evidence = sameRoundDispositions(
      { engine: 'claude', round: 1, repo: 'loomantix/platform-oss' },
      [
        [
          finding('fp1', 1, 'minor'),
          { ...disposition('fp1'), head: LATER_HEAD },
        ],
      ],
      { [HEAD]: 0, [LATER_HEAD]: 1 },
    );
    expect(evidence).toEqual([['fp1', true, false, true]]);
  });

  it('validates but excludes findings from before the observed transition', () => {
    const priorHead = 'c'.repeat(40);
    const historicalFinding = { ...finding('fp1'), head: priorHead };
    const laterDisposition = { ...disposition('fp1'), head: LATER_HEAD };
    const evidence = sameRoundDispositions(
      { engine: 'claude', round: 1, repo: 'loomantix/platform-oss' },
      [[historicalFinding, laterDisposition]],
      { [HEAD]: 0, [LATER_HEAD]: 1 },
    );
    expect(evidence).toEqual([]);
  });
});

describe('complete thread verification', () => {
  const scope = { repo: 'loomantix/platform-oss', pr: 7 };

  it('accepts a settled thread', () => {
    expect(
      verifyThreadDispositions([settledThread()], undefined, scope),
    ).toHaveLength(1);
  });

  it('rejects an unresolved finding thread', () => {
    expect(() =>
      verifyThreadDispositions(
        [settledThread({ isResolved: false })],
        undefined,
        scope,
      ),
    ).toThrowError(/is unresolved/);
  });

  it('rejects a disposition with no finding', () => {
    const dc = 'the fix\n';
    const node = thread([
      comment(
        `${dispositionMarker({ fingerprint: 'fp1', content: dc })}\n${dc}`,
      ),
    ]);
    expect(() =>
      verifyThreadDispositions([node], undefined, scope),
    ).toThrowError(/disposition without a finding/);
  });

  it('rejects a root finding that is not the first comment in its thread', () => {
    const fc = 'the finding\n';
    const dc = 'the fix\n';
    const node = thread([
      comment('some unrelated preamble\n', 1),
      comment(
        `${findingMarker({ fingerprint: 'fp1', content: fc })}\n${fc}`,
        2,
      ),
      comment(
        `${dispositionMarker({ fingerprint: 'fp1', content: dc })}\n${dc}`,
        3,
      ),
    ]);
    expect(() =>
      verifyThreadDispositions([node], undefined, scope),
    ).toThrowError(/topology is invalid/);
  });

  it('rejects a blocking finding left deferred', () => {
    const fc = 'the finding\n';
    const dc = 'deferred to a follow-up\n';
    const node = thread([
      comment(
        `${findingMarker({ fingerprint: 'fp1', severity: 'blocking', content: fc })}\n${fc}`,
        1,
      ),
      comment(
        `${dispositionMarker({ fingerprint: 'fp1', outcome: 'deferred', content: dc })}\n${dc}`,
        2,
      ),
    ]);
    expect(() =>
      verifyThreadDispositions([node], undefined, scope),
    ).toThrowError(/blocking local-review findings must be fixed/);
  });

  it('rejects a blocking finding that was dismissed', () => {
    const fc = 'the finding\n';
    const dc = 'not a defect after all\n';
    const node = thread([
      comment(
        `${findingMarker({ fingerprint: 'fp1', severity: 'blocking', content: fc })}\n${fc}`,
        1,
      ),
      comment(
        `${dispositionMarker({ fingerprint: 'fp1', outcome: 'dismissed', content: dc })}\n${dc}`,
        2,
      ),
    ]);
    expect(() =>
      verifyThreadDispositions([node], undefined, scope),
    ).toThrowError(/blocking local-review findings must be fixed/);
  });

  it('rejects one fingerprint split across two threads', () => {
    const fc = 'the finding\n';
    const dc = 'the fix\n';
    const second = thread(
      [
        comment(
          `${findingMarker({ fingerprint: 'fp1', occurrence: 2, content: fc })}\n${fc}`,
          3,
        ),
        comment(
          `${dispositionMarker({ fingerprint: 'fp1', occurrence: 2, content: dc })}\n${dc}`,
          4,
        ),
      ],
      { id: 'PRRT_2' },
    );
    expect(() =>
      verifyThreadDispositions([settledThread(), second], undefined, scope),
    ).toThrowError(/topology is invalid/);
  });
});

describe('historical comment id bounds', () => {
  it('fails closed when no bound is declared', () => {
    expect(loadHistoricalCommentIds()).toEqual(new Set());
  });
});

describe('verifyLedger end to end', () => {
  const base = { repo: 'loomantix/platform-oss', pr: 7, head: HEAD };

  it('verifies a settled ledger fetched live', () => {
    runner.threadNodes = [settledThread()];
    expect(verifyLedger(base)).toEqual({
      actor: ACTOR,
      dispositions: 1,
      verified: true,
    });
  });

  it('refuses to verify against a caller-declared actor', () => {
    runner.threadNodes = [settledThread()];
    expect(() =>
      verifyLedger({ ...base, actor: 'somebody-else' }),
    ).toThrowError(/authenticated GitHub actor changed/);
  });

  it('rejects threads that belong to another pull request', () => {
    runner.threadNodes = [settledThread({ pullRequest: { number: 9 } })];
    expect(() => verifyLedger(base)).toThrowError(/outside the requested PR/);
  });

  it('rejects a head that moved during verification', () => {
    runner.threadNodes = [settledThread()];
    runner.prHead = LATER_HEAD;
    expect(() => verifyLedger(base)).toThrowError(/PR head mismatch/);
  });
});
