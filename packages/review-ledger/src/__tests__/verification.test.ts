import { createHash } from 'node:crypto';
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  attest,
  finalize,
  LedgerError,
  currentActor,
  getThreadState,
  readContent,
  reconcile,
  rowsHaveHistoricalMarkers,
  verifyForwardTransitionOrFail,
  verifyGitTransition,
  verifyLedger,
  verifyReviewBase,
  writeResult,
  recoverResult,
  readResult,
  writeBlockedResult,
} from '../index.js';
import { resetGitHubRunner, setGitHubRunner } from '../github.js';
import type { GitHubReviewThreadNode, GitHubRunner } from '../types.js';

/**
 * Negative coverage for the verification core.
 *
 * The rule for everything here: deleting the check under test must fail the
 * test. The suites that predate this file all drive stubs that succeed
 * unconditionally, so the rules they exercise could each be removed with the
 * suite still green.
 */

const ACTOR = 'review-bot';
const REPO = 'loomantix/platform-oss';
const PR = 7;
const BASE = '0'.repeat(40);
const BEFORE = 'a'.repeat(40);
const HEAD = 'b'.repeat(40);
const OTHER = 'c'.repeat(40);

const sha = (value: string): string =>
  createHash('sha256').update(value, 'utf8').digest('hex');

function findingMarker(o: {
  fingerprint: string;
  occurrence?: number;
  severity?: string;
  content: string;
  head?: string;
  round?: number;
  engine?: string;
}): string {
  return (
    `<!-- local-review:v3 engine=${o.engine ?? 'claude'} round=${o.round ?? 1} ` +
    `head=${o.head ?? BEFORE} fingerprint=${o.fingerprint} ` +
    `occurrence=${o.occurrence ?? 1} severity=${o.severity ?? 'major'} ` +
    `lens=code-reviewer content-sha256=${sha(o.content)} -->`
  );
}

function dispositionMarker(o: {
  fingerprint: string;
  occurrence?: number;
  outcome?: string;
  content: string;
  head?: string;
  round?: number;
  engine?: string;
}): string {
  return (
    `<!-- local-review-disposition:v3 engine=${o.engine ?? 'claude'} round=${o.round ?? 1} ` +
    `head=${o.head ?? HEAD} fingerprint=${o.fingerprint} ` +
    `occurrence=${o.occurrence ?? 1} outcome=${o.outcome ?? 'fixed'} ` +
    `content-sha256=${sha(o.content)} -->`
  );
}

/** One fingerprint, posted at BEFORE and fixed at HEAD: a real changed round. */
function fixedThread(
  fingerprint = 'fp1',
  severity = 'major',
  round = 1,
): GitHubReviewThreadNode {
  const fc = 'the finding\n';
  const dc = 'the fix\n';
  return {
    id: `PRRT_${fingerprint}`,
    isResolved: true,
    repository: { nameWithOwner: REPO },
    pullRequest: { number: PR },
    comments: {
      nodes: [
        {
          databaseId: 1,
          body: `${findingMarker({ fingerprint, severity, round, content: fc })}\n${fc}`,
          author: { login: ACTOR },
        },
        {
          databaseId: 2,
          body: `${dispositionMarker({ fingerprint, round, content: dc })}\n${dc}`,
          author: { login: ACTOR },
        },
      ],
      pageInfo: { hasNextPage: false },
    },
  } as GitHubReviewThreadNode;
}

/**
 * A stub whose every verification input can be made to fail.
 *
 * Each knob exists because the check it feeds had no failing test.
 */
class ConfigurableRunner implements GitHubRunner {
  actor: string = ACTOR;
  actorRaw: string | null = null;
  threadNodes: GitHubReviewThreadNode[] = [];
  reviewComments: Array<Record<string, unknown>> = [];
  issueComments: Array<Record<string, unknown>> = [];
  prHead = HEAD;
  prBase = BASE;
  localHead = HEAD;
  ancestor = true;
  compareStatus = 'ahead';
  compareMergeBase: string | null = null;
  revList: string[] | null = null;
  commentIdSeq = 900;
  /** `git diff --name-status` body for the pass's change range. */
  diffNameStatus = '';
  /** `git diff --unified=0` body, keyed by path. */
  diffPatches: Record<string, string> = {};
  sourceBlobs: Record<string, string> = {};

  runGh(args: string[], payload?: unknown): string {
    const cmd = args.join(' ');
    if (cmd === 'api user') {
      return this.actorRaw ?? JSON.stringify({ login: this.actor });
    }
    if (cmd.includes('pr view') && cmd.includes('baseRefOid')) {
      return this.prBase;
    }
    if (cmd.includes('pr view')) {
      return this.prHead;
    }
    if (cmd.includes('/issues/') && cmd.includes('comments?per_page=100')) {
      return JSON.stringify([this.issueComments]);
    }
    if (cmd.includes('/pulls/') && cmd.includes('comments?per_page=100')) {
      return JSON.stringify([this.reviewComments]);
    }
    if (cmd.includes('-X POST') && cmd.includes('/issues/')) {
      const id = this.commentIdSeq++;
      const data = payload as { body?: string };
      this.issueComments.push({
        id,
        databaseId: id,
        user: { login: this.actor },
        body: data?.body ?? '',
      });
      return JSON.stringify({ id });
    }
    if (cmd.includes('-X DELETE') && cmd.includes('/issues/comments/')) {
      const id = parseInt(args[args.length - 1]!.split('/').pop()!, 10);
      this.issueComments = this.issueComments.filter((c) => c['id'] !== id);
      return '{}';
    }
    if (cmd.includes('/issues/comments/')) {
      const id = parseInt(args[1]!.split('/').pop()!, 10);
      const row = this.issueComments.find((c) => c['id'] === id);
      return JSON.stringify(
        row ?? { id, user: { login: this.actor }, body: '' },
      );
    }
    if (cmd.includes('graphql')) {
      const p = payload as {
        query?: string;
        variables?: { threadId?: string };
      };
      if (p?.query?.includes('PullRequestReviewThread')) {
        return JSON.stringify({
          data: {
            node:
              this.threadNodes.find((t) => t.id === p.variables?.threadId) ??
              null,
          },
        });
      }
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
    return {
      status: this.compareStatus,
      merge_base_commit: { sha: this.compareMergeBase ?? before },
    };
  }

  gitRevList(_before: string, head: string): string[] {
    return this.revList ?? [head];
  }

  runGit(args: string[]): string {
    if (args[0] === 'show') {
      const source = this.sourceBlobs[args[1]!];
      if (source === undefined) throw new Error('source blob unavailable');
      return source;
    }
    if (args.includes('--summary')) return '';
    if (args[0] === 'rev-parse' && args[1] === 'HEAD') return this.localHead;
    if (args[0] === 'rev-parse' && args[1] === '--verify') {
      return args[2]!.replace(/\^\{commit\}$/, '');
    }
    if (args[0] === 'diff' && args.includes('--name-status')) {
      return this.diffNameStatus;
    }
    if (args[0] === 'diff') {
      return this.diffPatches[args[args.length - 1]!] ?? '';
    }
    return '';
  }

  isAncestor(): boolean {
    return this.ancestor;
  }
}

let runner: ConfigurableRunner;
let dir: string;

beforeEach(() => {
  runner = new ConfigurableRunner();
  setGitHubRunner(runner);
  dir = mkdtempSync(join(tmpdir(), 'review-ledger-verify-'));
  delete process.env['AGENT_LOOP_REVIEW_ACTOR'];
  delete process.env['AGENT_LOOP_REVIEW_THREADS_SHA256'];
  delete process.env['AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE'];
});

afterEach(() => {
  resetGitHubRunner();
});

const resultPath = (): string => join(dir, 'review-result.json');

function writeResultJson(value: Record<string, unknown>): string {
  const path = resultPath();
  const sorted: Record<string, unknown> = {};
  for (const key of Object.keys(value).sort()) sorted[key] = value[key];
  writeFileSync(path, JSON.stringify(sorted) + '\n', 'utf8');
  return path;
}

const changedResult = (
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  version: 3,
  status: 'changed',
  engine: 'claude',
  round: 1,
  baseSha: BASE,
  beforeSha: BEFORE,
  afterSha: HEAD,
  classification: 'material',
  findingFingerprints: ['fp1'],
  finalLaneComplete: true,
  ...overrides,
});

describe('forward-only transition predicate', () => {
  it('rejects a comparison that is not ahead', () => {
    runner.compareStatus = 'behind';
    expect(() =>
      verifyForwardTransitionOrFail(REPO, BEFORE, HEAD, 'not forward'),
    ).toThrow('not forward');
  });

  it('rejects a comparison whose merge base is not the before commit', () => {
    runner.compareMergeBase = OTHER;
    expect(() =>
      verifyForwardTransitionOrFail(REPO, BEFORE, HEAD, 'not forward'),
    ).toThrow('not forward');
  });

  it('accepts a strictly forward comparison', () => {
    expect(() =>
      verifyForwardTransitionOrFail(REPO, BEFORE, HEAD, 'not forward'),
    ).not.toThrow();
  });
});

describe('review base and git transition binding', () => {
  it('rejects a pinned base unrelated to the current pull request base', () => {
    runner.prBase = OTHER;
    runner.isAncestor = (...args: string[]) => args[1] !== OTHER;
    expect(() => verifyReviewBase(REPO, PR, BASE, BEFORE)).toThrow(
      /diverged from the pinned base/,
    );
  });

  it('asks for a fetch only when the ancestry check itself fails', () => {
    runner.prBase = OTHER;
    runner.isAncestor = (...args: string[]) => {
      if (args[1] === OTHER) {
        throw new LedgerError('Git ancestry check failed: unknown revision');
      }
      return true;
    };
    expect(() => verifyReviewBase(REPO, PR, BASE, BEFORE)).toThrow(
      /unknown revision; fetch the target branch before retrying/,
    );
    runner.isAncestor = (...args: string[]) => args[1] !== OTHER;
    expect(() => verifyReviewBase(REPO, PR, BASE, BEFORE)).not.toThrow(
      /fetch the target branch/,
    );
  });

  it('rejects a pull request base that is not a commit SHA', () => {
    runner.prBase = '';
    expect(() => verifyReviewBase(REPO, PR, BASE, BEFORE)).toThrow(
      /not a commit SHA/,
    );
  });

  it('keeps a valid pinned review when the target branch advances', () => {
    runner.prBase = OTHER;
    expect(() => verifyReviewBase(REPO, PR, BASE, BEFORE)).not.toThrow();
  });

  it('rejects a before commit that is not an ancestor of the base', () => {
    runner.ancestor = false;
    expect(() => verifyReviewBase(REPO, PR, BASE, BEFORE)).toThrow();
  });

  it('rejects a local checkout that is not at the reviewed head', () => {
    runner.localHead = OTHER;
    expect(() => verifyGitTransition(BEFORE, HEAD, HEAD)).toThrow();
  });

  it('rejects a before commit that is not an ancestor of the head', () => {
    runner.ancestor = false;
    expect(() => verifyGitTransition(BEFORE, HEAD, HEAD)).toThrow();
  });
});

describe('result evidence is matched against real ledger evidence', () => {
  beforeEach(() => {
    runner.threadNodes = [fixedThread('fp1')];
  });

  it('rejects a result claiming a fingerprint the ledger does not show', () => {
    const file = writeResultJson(
      changedResult({ findingFingerprints: ['fp1', 'fp-not-in-ledger'] }),
    );
    expect(() =>
      verifyLedger({
        repo: REPO,
        pr: PR,
        head: HEAD,
        engine: 'claude',
        round: 1,
        base: BASE,
        before: BEFORE,
        resultFile: file,
      }),
    ).toThrow('do not equal the complete same-round disposition set');
  });

  it('rejects a result omitting a fingerprint the ledger does show', () => {
    runner.threadNodes = [fixedThread('fp1'), fixedThread('fp2')];
    const file = writeResultJson(changedResult());
    expect(() =>
      verifyLedger({
        repo: REPO,
        pr: PR,
        head: HEAD,
        engine: 'claude',
        round: 1,
        base: BASE,
        before: BEFORE,
        resultFile: file,
      }),
    ).toThrow('do not equal the complete same-round disposition set');
  });

  it('rejects a clean result that claims a fingerprint', () => {
    runner.threadNodes = [];
    const file = writeResultJson(
      changedResult({
        status: 'clean',
        classification: null,
        beforeSha: HEAD,
        findingFingerprints: ['fp1'],
      }),
    );
    expect(() =>
      verifyLedger({
        repo: REPO,
        pr: PR,
        head: HEAD,
        engine: 'claude',
        round: 1,
        base: BASE,
        before: HEAD,
        resultFile: file,
      }),
    ).toThrow('do not equal the complete same-round disposition set');
  });

  it('accepts a fixed major finding classified as minor when the fix moved no executing line', () => {
    // Severity describes the finding; classification describes the diff. A
    // comment correction is `minor` however serious the thread was.
    runner.diffNameStatus = 'M\tinfra/eks/main.tf\n';
    runner.diffPatches = {
      'infra/eks/main.tf': [
        '@@ -3 +3 @@',
        '-# the node floor is 3',
        '+# the node floor is 2',
      ].join('\n'),
    };
    const file = writeResultJson(changedResult({ classification: 'minor' }));
    expect(() =>
      verifyLedger({
        repo: REPO,
        pr: PR,
        head: HEAD,
        engine: 'claude',
        round: 1,
        base: BASE,
        before: BEFORE,
        resultFile: file,
      }),
    ).not.toThrow();
  });

  it('rejects a minor classification whose range moved an executing line', () => {
    runner.diffNameStatus = 'M\tinfra/eks/main.tf\n';
    runner.diffPatches = {
      'infra/eks/main.tf': [
        '@@ -8 +8 @@',
        '-  min_size = 3',
        '+  min_size = 2',
      ].join('\n'),
    };
    const file = writeResultJson(changedResult({ classification: 'minor' }));
    expect(() =>
      verifyLedger({
        repo: REPO,
        pr: PR,
        head: HEAD,
        engine: 'claude',
        round: 1,
        base: BASE,
        before: BEFORE,
        resultFile: file,
      }),
    ).toThrow('minor classification requires a non-behavioral change range');
  });

  it('rejects a minor classification for a behavioral range whatever the severity', () => {
    // The evasion the old severity coupling could not close: posting the
    // finding `nit` and attesting `minor` after editing a conditional.
    runner.threadNodes = [fixedThread('fp1', 'nit')];
    runner.diffNameStatus = 'M\tsrc/user.ts\n';
    runner.diffPatches = {
      'src/user.ts': [
        '@@ -4 +4 @@',
        '-  return name.trim();',
        '+  return name.trim().toLowerCase();',
      ].join('\n'),
    };
    const file = writeResultJson(changedResult({ classification: 'minor' }));
    expect(() =>
      verifyLedger({
        repo: REPO,
        pr: PR,
        head: HEAD,
        engine: 'claude',
        round: 1,
        base: BASE,
        before: BEFORE,
        resultFile: file,
      }),
    ).toThrow('minor classification requires a non-behavioral change range');
  });

  it('rejects a convergence round that fixed a non-blocking finding', () => {
    runner.threadNodes = [fixedThread('fp1', 'minor', 3)];
    const file = writeResultJson(changedResult({ round: 3 }));
    expect(() =>
      verifyLedger({
        repo: REPO,
        pr: PR,
        head: HEAD,
        engine: 'claude',
        round: 3,
        base: BASE,
        before: BEFORE,
        resultFile: file,
      }),
    ).toThrow('convergence review results cannot fix non-blocking findings');
  });

  it('rejects a transition whose revision walk does not reach the head', () => {
    runner.revList = [OTHER];
    const file = writeResultJson(changedResult());
    expect(() =>
      verifyLedger({
        repo: REPO,
        pr: PR,
        head: HEAD,
        engine: 'claude',
        round: 1,
        base: BASE,
        before: BEFORE,
        resultFile: file,
      }),
    ).toThrow('review result transition is not forward-only');
  });

  it('accepts a result that exactly matches the ledger', () => {
    const file = writeResultJson(changedResult());
    expect(
      verifyLedger({
        repo: REPO,
        pr: PR,
        head: HEAD,
        engine: 'claude',
        round: 1,
        base: BASE,
        before: BEFORE,
        resultFile: file,
      }).verified,
    ).toBe(true);
  });
});

describe('writeResult rejects results its evidence does not support', () => {
  const params = (overrides: Record<string, unknown> = {}) =>
    ({
      repo: REPO,
      pr: PR,
      head: HEAD,
      engine: 'claude' as const,
      round: 1,
      base: BASE,
      before: BEFORE,
      resultFile: resultPath(),
      classification: 'material' as const,
      ...overrides,
    }) as Parameters<typeof writeResult>[0];

  it('binds the declared base to the live pull request', () => {
    runner.threadNodes = [fixedThread('fp1')];
    runner.prBase = OTHER;
    runner.isAncestor = (...args: string[]) => args[1] !== OTHER;
    expect(() => writeResult(params())).toThrow();
  });

  it('binds the declared head to the local checkout', () => {
    runner.threadNodes = [fixedThread('fp1')];
    runner.localHead = OTHER;
    expect(() => writeResult(params())).toThrow();
  });

  it('rejects a changed result with no classification', () => {
    runner.threadNodes = [fixedThread('fp1')];
    expect(() => writeResult(params({ classification: undefined }))).toThrow(
      'changed review result requires --classification',
    );
  });

  it('rejects a changed result with no ledger evidence', () => {
    runner.threadNodes = [];
    expect(() => writeResult(params())).toThrow(
      'changed review results require ledger evidence',
    );
  });

  it('writes a clean result for a round that moved nothing', () => {
    runner.threadNodes = [];
    const out = writeResult(
      params({ before: HEAD, classification: undefined }),
    );
    expect(out.status).toBe('clean');
    expect(out.classification).toBeNull();
    expect(out.findingFingerprints).toEqual([]);
  });

  it('rejects a round 3 changed result that is not material', () => {
    runner.threadNodes = [fixedThread('fp1')];
    expect(() =>
      writeResult(params({ round: 3, classification: 'minor' })),
    ).toThrow(
      'round 3+ changed review results require material classification',
    );
  });

  it('rejects a minor write whose range moved an executing line', () => {
    runner.threadNodes = [fixedThread('fp1')];
    runner.diffNameStatus = 'M\tsrc/user.ts\n';
    runner.diffPatches = {
      'src/user.ts': ['@@ -4 +4 @@', '-  return a;', '+  return b;'].join('\n'),
    };
    expect(() => writeResult(params({ classification: 'minor' }))).toThrow(
      'minor classification requires a non-behavioral change range',
    );
  });

  it('rejects a minor result when a multiline JSDoc type changed', () => {
    runner.threadNodes = [fixedThread('fp1')];
    runner.diffNameStatus = 'M\tsrc/settings.js\n';
    runner.sourceBlobs = {
      [`${BEFORE}:src/settings.js`]:
        '/**\n * @type {{\n *   enabled: boolean\n * }}\n */\nconst settings = { enabled: true };',
      [`${HEAD}:src/settings.js`]:
        '/**\n * @type {{\n *   enabled: string\n * }}\n */\nconst settings = { enabled: true };',
    };
    expect(() => writeResult(params({ classification: 'minor' }))).toThrow(
      'minor classification requires a non-behavioral change range',
    );
    expect(readResult(resultPath()).status).toBe('blocked');
  });

  it('writes a minor result when the fix moved no executing line', () => {
    runner.threadNodes = [fixedThread('fp1')];
    runner.diffNameStatus = 'M\tsrc/user.ts\n';
    runner.diffPatches = {
      'src/user.ts': ['@@ -4 +4 @@', '-// old note', '+// new note'].join('\n'),
    };
    runner.sourceBlobs = {
      [`${BEFORE}:src/user.ts`]: '// old note\nconst value = 1;',
      [`${HEAD}:src/user.ts`]: '// new note\nconst value = 1;',
    };
    const out = writeResult(params({ classification: 'minor' }));
    expect(out.status).toBe('changed');
    expect(out.classification).toBe('minor');
  });

  it('writes a minor result for prose in a CommonJS file with a top-level return', () => {
    runner.threadNodes = [fixedThread('fp1')];
    runner.diffNameStatus = 'M\tsrc/settings.cjs\n';
    runner.sourceBlobs = {
      [`${BEFORE}:src/settings.cjs`]:
        '// Old prose.\nif (process.env.SKIP_FIXTURE) return;\nmodule.exports = 42;',
      [`${HEAD}:src/settings.cjs`]:
        '// New prose.\nif (process.env.SKIP_FIXTURE) return;\nmodule.exports = 42;',
    };
    const out = writeResult(params({ classification: 'minor' }));
    expect(out.status).toBe('changed');
    expect(out.classification).toBe('minor');
  });

  it.each([
    ['const value = 1;', '// Preserve the value.\nconst value = 1;'],
    ['// Preserve the value.\nconst value = 1;', 'const value = 1;'],
    [
      '// Preserve the value.\nconst value = 1;',
      '// Preserve the\n// value.\nconst value = 1;',
    ],
  ])('finalizes a minor prose-reshaping result', (before, after) => {
    runner.threadNodes = [fixedThread('fp1')];
    runner.diffNameStatus = 'M\tsrc/identity.ts\n';
    runner.sourceBlobs = {
      [`${BEFORE}:src/identity.ts`]: before,
      [`${HEAD}:src/identity.ts`]: after,
    };
    const result = writeResult(params({ classification: 'minor' }));
    expect(result).toMatchObject({
      status: 'changed',
      classification: 'minor',
      finalLaneComplete: true,
      findingFingerprints: ['fp1'],
    });
    expect(existsSync(`${resultPath()}.recovery.json`)).toBe(false);
  });

  it('rejects a minor result for executable code hidden by a script parse', () => {
    runner.threadNodes = [fixedThread('fp1')];
    runner.diffNameStatus = 'M\tsrc/identity.mjs\n';
    runner.sourceBlobs = {
      [`${BEFORE}:src/identity.mjs`]:
        'const r = await /[//]/.source; x = [\n 0]',
      [`${HEAD}:src/identity.mjs`]: 'const r = await /[//]/.flags; y = [\n 0]',
    };
    expect(() => writeResult(params({ classification: 'minor' }))).toThrow();
  });

  it('rejects a minor result when an inline Flow comment type changed', () => {
    runner.threadNodes = [fixedThread('fp1')];
    runner.diffNameStatus = 'M\tsrc/settings.js\n';
    runner.sourceBlobs = {
      [`${BEFORE}:src/settings.js`]: 'export const value /*: number */ = 1;',
      [`${HEAD}:src/settings.js`]: 'export const value /*: string */ = 1;',
    };
    expect(() => writeResult(params({ classification: 'minor' }))).toThrow(
      'minor classification requires a non-behavioral change range',
    );
    expect(readResult(resultPath()).status).toBe('blocked');
  });

  it('writes a result the ledger supports', () => {
    runner.threadNodes = [fixedThread('fp1')];
    const out = writeResult(params());
    expect(out.status).toBe('changed');
    expect(out.findingFingerprints).toEqual(['fp1']);
    expect(out.resultSha256).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe('completed result finalization recovery', () => {
  const params = () => ({
    repo: REPO,
    pr: PR,
    head: HEAD,
    engine: 'claude' as const,
    round: 1,
    base: BASE,
    before: BEFORE,
    resultFile: resultPath(),
    historicalCommentIdsFile: join(dir, 'historical.json'),
  });
  const receiptPath = () => `${resultPath()}.recovery.json`;
  const prepare = () => {
    writeFileSync(params().historicalCommentIdsFile, '[]\n');
    runner.threadNodes = [fixedThread('fp1')];
    runner.diffNameStatus = 'M\tsrc/identity.ts\n';
    // The review is complete, but an unavailable blob prevents the final
    // non-behavioral proof. No reviewer must run when the blob becomes available.
    expect(() => writeResult({ ...params(), classification: 'minor' })).toThrow(
      'non-behavioral',
    );
    runner.sourceBlobs = {
      [`${BEFORE}:src/identity.ts`]:
        '/**\n * Existing guard.\n */\nconst value = 1;',
      [`${HEAD}:src/identity.ts`]:
        '/**\n * Clarified existing guard.\n */\nconst value = 1;',
    };
    return {
      ...params(),
      expectedRecoverySha256: sha(readFileSync(receiptPath(), 'utf8')),
    };
  };

  it('preserves the blocked bytes and complete candidate, then rechecks and replays recovery', () => {
    const recovery = prepare();
    const blocked = readFileSync(resultPath(), 'utf8');
    const receipt = readFileSync(receiptPath(), 'utf8');
    expect(readResult(resultPath()).status).toBe('blocked');
    expect(statSync(receiptPath()).mode & 0o777).toBe(0o600);
    expect(JSON.parse(receipt).blockedResult).toBe(blocked);
    const result = recoverResult(recovery);
    expect(result).toMatchObject({
      status: 'changed',
      classification: 'minor',
      findingFingerprints: ['fp1'],
      finalLaneComplete: true,
    });
    expect(recoverResult(recovery)).toEqual(result);
    expect(readFileSync(receiptPath(), 'utf8')).toBe(receipt);
    expect(runner.issueComments).toEqual([]);
  });

  it.each([
    'digest',
    'snapshot',
    'actor',
    'repo',
    'pr',
    'round',
    'base',
    'before',
    'head',
    'result',
    'ledger',
    'live-head',
    'source',
  ])('rejects changed %s evidence without rewriting the result', (change) => {
    const recovery = prepare();
    if (change === 'digest') recovery.expectedRecoverySha256 = '0'.repeat(64);
    if (change === 'snapshot')
      writeFileSync(recovery.historicalCommentIdsFile, '[1]\n');
    if (change === 'actor') runner.actor = 'different-actor';
    if (change === 'repo') recovery.repo = 'example/different';
    if (change === 'pr') recovery.pr += 1;
    if (change === 'round') recovery.round += 1;
    if (change === 'base') recovery.base = OTHER;
    if (change === 'before') recovery.before = OTHER;
    if (change === 'head') recovery.head = OTHER;
    if (change === 'result')
      writeBlockedResult({
        ...params(),
        blocker: 'Review has an unresolved defect.',
      });
    if (change === 'ledger') runner.threadNodes = [];
    if (change === 'live-head') runner.prHead = OTHER;
    if (change === 'source') runner.sourceBlobs = {};
    const original = readFileSync(resultPath(), 'utf8');
    expect(() => recoverResult(recovery)).toThrow();
    expect(readFileSync(resultPath(), 'utf8')).toBe(original);
    expect(runner.issueComments).toEqual([]);
  });

  it('does not create a completed candidate for an unfinished review', () => {
    writeFileSync(params().historicalCommentIdsFile, '[]\n');
    expect(() => writeResult({ ...params(), classification: 'minor' })).toThrow(
      'ledger evidence',
    );
    writeBlockedResult({ ...params(), blocker: 'Review has not finished.' });
    expect(existsSync(receiptPath())).toBe(false);
    expect(() =>
      recoverResult({ ...params(), expectedRecoverySha256: '0'.repeat(64) }),
    ).toThrow();
  });
});

describe('explicit result retry', () => {
  it('archives an earlier recovery receipt when the new result succeeds', () => {
    const params = {
      repo: REPO,
      pr: PR,
      head: HEAD,
      engine: 'claude' as const,
      round: 1,
      base: BASE,
      before: BEFORE,
      resultFile: resultPath(),
      classification: 'minor' as const,
    };
    runner.threadNodes = [fixedThread('fp1')];
    runner.diffNameStatus = 'M\tsrc/identity.ts\n';
    expect(() => writeResult(params)).toThrow('non-behavioral');
    const path = `${resultPath()}.recovery.json`;
    const original = readFileSync(path, 'utf8');
    runner.sourceBlobs = {
      [`${BEFORE}:src/identity.ts`]: '// Old prose\nconst value = 1;',
      [`${HEAD}:src/identity.ts`]: '// New prose\nconst value = 1;',
    };
    expect(writeResult(params).status).toBe('changed');
    expect(existsSync(path)).toBe(false);
    expect(
      readFileSync(`${resultPath()}.recovery.${sha(original)}.json`, 'utf8'),
    ).toBe(original);
  });
});

describe('attestation identity is one per engine and round', () => {
  const attestParams = (file: string, digest: string) =>
    ({
      repo: REPO,
      pr: PR,
      head: HEAD,
      engine: 'claude' as const,
      round: 1,
      base: BASE,
      before: BEFORE,
      resultFile: file,
      expectedResultSha256: digest,
    }) as Parameters<typeof attest>[0];

  const seal = (value: Record<string, unknown>): [string, string] => {
    const file = writeResultJson(value);
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) sorted[key] = value[key];
    return [file, sha(JSON.stringify(sorted) + '\n')];
  };

  beforeEach(() => {
    runner.threadNodes = [fixedThread('fp1')];
  });

  it('rejects a result file that changed after it was sealed', () => {
    const [file] = seal(changedResult());
    expect(() => attest(attestParams(file, sha('other')))).toThrow(
      'review result changed before attestation',
    );
  });

  it('replays an identical attestation instead of posting a second one', () => {
    const [file, digest] = seal(changedResult());
    const first = attest(attestParams(file, digest));
    expect(first.replayed).toBe(false);
    const second = attest(attestParams(file, digest));
    expect(second.replayed).toBe(true);
    expect(second.comment_id).toBe(first.comment_id);
    expect(runner.issueComments).toHaveLength(1);
  });

  it('refuses a second attestation for the same engine and round', () => {
    const [file, digest] = seal(changedResult());
    attest(attestParams(file, digest));

    // Same round, different evidence: a contradiction, not a new record.
    runner.threadNodes = [fixedThread('fp1'), fixedThread('fp2')];
    const [file2, digest2] = seal(
      changedResult({ findingFingerprints: ['fp1', 'fp2'] }),
    );
    expect(() => attest(attestParams(file2, digest2))).toThrow(
      'local-review attestation identity conflicts with existing evidence',
    );
    expect(runner.issueComments).toHaveLength(1);
  });

  const declareRun = (
    tier: 'lean' | 'deep',
    base: string,
    content = 'Run the bounded review.\n',
  ): void => {
    const maxRounds = tier === 'deep' ? 4 : 2;
    const runId = sha(
      JSON.stringify({
        base,
        content,
        max_rounds: maxRounds,
        start_head: HEAD,
        supersedes: null,
        tier,
      }),
    );
    runner.issueComments.push({
      id: runner.commentIdSeq++,
      user: { login: ACTOR },
      body: `<!-- local-review-run:v1 id=${runId} tier=${tier} max-rounds=${maxRounds} base=${base} start-head=${HEAD} supersedes=none content-sha256=${runId} -->\n${content}`,
    });
  };

  it('refuses a result whose base is not the current run base', () => {
    declareRun('deep', OTHER);
    const [file, digest] = seal(changedResult());
    expect(() => attest(attestParams(file, digest))).toThrow(
      'saved review result does not belong to the current run base and round budget',
    );
    expect(runner.issueComments).toHaveLength(1);
  });

  it('refuses a round above the current run budget', () => {
    declareRun('lean', BASE);
    runner.threadNodes = [fixedThread('fp1', 'blocking', 3)];
    const [file, digest] = seal(changedResult({ round: 3 }));
    expect(() => attest({ ...attestParams(file, digest), round: 3 })).toThrow(
      'saved review result does not belong to the current run base and round budget',
    );
    expect(runner.issueComments).toHaveLength(1);
  });

  it('rolls back an attestation when the run changes during finalization', () => {
    const [file, digest] = seal(changedResult());
    const original = runner.runGh.bind(runner);
    runner.runGh = (args: string[], payload?: unknown): string => {
      const cmd = args.join(' ');
      const out = original(args, payload);
      if (cmd.includes('-X POST') && cmd.includes('/issues/')) {
        declareRun('deep', BASE);
      }
      return out;
    };
    expect(() => attest(attestParams(file, digest))).toThrow(
      'review run changed during finalization',
    );
    expect(runner.issueComments).toHaveLength(1);
    expect(String(runner.issueComments[0]!['body'])).toMatch(
      /^<!-- local-review-run:v1 /,
    );
  });

  it('replays the earliest delivered attestation among identical retries', () => {
    const [file, digest] = seal(changedResult());
    const first = attest(attestParams(file, digest));
    const marker = String(runner.issueComments[0]!['body']).split('\n')[0]!;
    runner.issueComments.push({
      id: runner.commentIdSeq++,
      user: { login: ACTOR },
      body: `${marker}\nExplanation from a later in-flight retry.\n`,
    });
    const replay = attest(attestParams(file, digest));
    expect(replay.replayed).toBe(true);
    expect(replay.comment_id).toBe(first.comment_id);
    expect(runner.issueComments).toHaveLength(2);
  });

  it('attests a restarted round without contradicting historical evidence', () => {
    const [file, digest] = seal(changedResult());
    const first = attest(attestParams(file, digest));
    const content = 'Continue the bounded review.\n';
    const runId = sha(
      JSON.stringify({
        base: BASE,
        content,
        max_rounds: 4,
        start_head: HEAD,
        supersedes: null,
        tier: 'deep',
      }),
    );
    runner.issueComments.push({
      id: runner.commentIdSeq++,
      user: { login: ACTOR },
      body: `<!-- local-review-run:v1 id=${runId} tier=deep max-rounds=4 base=${BASE} start-head=${HEAD} supersedes=none content-sha256=${runId} -->\n${content}`,
    });
    runner.threadNodes = [fixedThread('fp1'), fixedThread('fp2')];
    const [file2, digest2] = seal(
      changedResult({ findingFingerprints: ['fp1', 'fp2'] }),
    );
    const restarted = attest(attestParams(file2, digest2));
    expect(restarted.comment_id).not.toBe(first.comment_id);
    expect(runner.issueComments).toHaveLength(3);
    expect(attest(attestParams(file2, digest2)).replayed).toBe(true);
  });

  it.each([false, true])(
    'scopes interrupted POST recovery to the active run (created=%s)',
    (created) => {
      const [file, digest] = seal(changedResult());
      const params = attestParams(file, digest);
      const historical = attest(params);
      const content = 'Restart the bounded review.\n';
      const runId = sha(
        JSON.stringify({
          base: BASE,
          content,
          max_rounds: 4,
          start_head: HEAD,
          supersedes: null,
          tier: 'deep',
        }),
      );
      runner.issueComments.push({
        id: runner.commentIdSeq++,
        user: { login: ACTOR },
        body: `<!-- local-review-run:v1 id=${runId} tier=deep max-rounds=4 base=${BASE} start-head=${HEAD} supersedes=none content-sha256=${runId} -->\n${content}`,
      });
      const nextCommentId = runner.commentIdSeq;
      const original = runner.runGh.bind(runner);
      runner.runGh = (args: string[], payload?: unknown): string => {
        const cmd = args.join(' ');
        if (cmd.includes('-X POST') && cmd.includes('/issues/')) {
          if (created) original(args, payload);
          throw new LedgerError('Interrupted attestation POST');
        }
        return original(args, payload);
      };

      if (created) {
        const recovered = attest(params);
        expect(recovered.verified).toBe(true);
        expect(recovered.replayed).toBe(true);
        expect(recovered.comment_id).toBe(nextCommentId);
        expect(recovered.comment_id).not.toBe(historical.comment_id);
        expect(runner.issueComments).toHaveLength(3);
      } else {
        expect(() => attest(params)).toThrow('Interrupted attestation POST');
        expect(runner.issueComments).toHaveLength(2);
      }
    },
  );

  it.each([false, true])(
    'recovers delivered prose without accepting different evidence (conflict=%s)',
    (conflict) => {
      const [file, digest] = seal(changedResult());
      const params = attestParams(file, digest);
      const original = runner.runGh.bind(runner);
      let deliveredBody = '';
      const deliveredId = runner.commentIdSeq;
      runner.runGh = (args: string[], payload?: unknown): string => {
        const cmd = args.join(' ');
        if (cmd.includes('-X POST') && cmd.includes('/issues/')) {
          const { body } = payload as { body: string };
          const marker = body.split('\n')[0]!;
          deliveredBody = `${conflict ? marker.replace(digest, sha('different result')) : marker}\nExplanation from another in-flight retry.\n`;
          original(args, { body: deliveredBody });
          throw new LedgerError('Interrupted attestation POST');
        }
        return original(args, payload);
      };

      if (conflict) {
        expect(() => attest(params)).toThrow(
          'local-review attestation identity conflicts with existing evidence',
        );
      } else {
        const recovered = attest(params);
        expect(recovered.verified).toBe(true);
        expect(recovered.replayed).toBe(true);
        expect(recovered.comment_id).toBe(deliveredId);
      }
      expect(runner.issueComments).toHaveLength(1);
      expect(runner.issueComments[0]!['body']).toBe(deliveredBody);
    },
  );

  it('replays sealed evidence when only the explanatory prose changes', () => {
    const [file, digest] = seal(changedResult());
    const first = attest(attestParams(file, digest));
    const originalBody = runner.issueComments[0]!['body'];
    const note = join(dir, 'recovery-note.md');
    writeFileSync(note, 'Recovered after an interrupted controller.\n');
    const replay = attest({ ...attestParams(file, digest), contentFile: note });
    expect(replay.comment_id).toBe(first.comment_id);
    expect(replay.replayed).toBe(true);
    expect(runner.issueComments[0]!['body']).toBe(originalBody);
  });

  it('finalizes an interrupted pass using identity from the original result', () => {
    const [file] = seal(changedResult());
    const recovered = finalize({ repo: REPO, pr: PR, resultFile: file });
    expect(recovered.verified).toBe(true);
    expect(finalize({ repo: REPO, pr: PR, resultFile: file }).replayed).toBe(
      true,
    );
    runner.prHead = OTHER;
    expect(() => finalize({ repo: REPO, pr: PR, resultFile: file })).toThrow();
    expect(runner.issueComments).toHaveLength(1);
  });

  it('does not turn a blocked result into a completed review during recovery', () => {
    const [file] = seal(
      changedResult({
        status: 'blocked',
        classification: null,
        findingFingerprints: [],
        finalLaneComplete: false,
        blocker: 'Reviewer did not finish.',
      }),
    );
    expect(() => finalize({ repo: REPO, pr: PR, resultFile: file })).toThrow(
      /blocked review results/,
    );
    expect(runner.issueComments).toHaveLength(0);
  });

  it('removes an attestation whose read-back fails', () => {
    const [file, digest] = seal(changedResult());
    const params = attestParams(file, digest);
    // The head moves between the evidence check and the post-publication check.
    let seen = 0;
    const original = runner.runGh.bind(runner);
    runner.runGh = (args: string[], payload?: unknown): string => {
      const cmd = args.join(' ');
      if (cmd.includes('pr view') && !cmd.includes('baseRefOid')) {
        seen += 1;
        if (seen > 1) return OTHER;
      }
      return original(args, payload);
    };
    expect(() => attest(params)).toThrow();
    expect(runner.issueComments).toHaveLength(0);
  });
});

describe('reconcile reports the action the ledger actually needs', () => {
  it('tells a caller to post a fingerprint that has never been posted', () => {
    const out = reconcile({
      repo: REPO,
      pr: PR,
      head: HEAD,
      fingerprint: 'never-posted',
    });
    expect(out.sequenceValid).toBe(true);
    expect(out.ledgerValid).toBe(true);
    expect(out.nextAction).toBe('post-finding');
    expect(out.nextOccurrence).toBe(1);
  });

  it('does not count another engine or round as disposing an occurrence', () => {
    const fc = 'the finding\n';
    const dc = 'the fix\n';
    runner.reviewComments = [
      {
        id: 1,
        databaseId: 1,
        user: { login: ACTOR },
        body: `${findingMarker({ fingerprint: 'fp1', content: fc })}\n${fc}`,
      },
      {
        id: 2,
        databaseId: 2,
        user: { login: ACTOR },
        // Same fingerprint and occurrence, different engine.
        body: `${dispositionMarker({ fingerprint: 'fp1', content: dc, engine: 'codex' })}\n${dc}`,
      },
    ];
    const out = reconcile({
      repo: REPO,
      pr: PR,
      head: HEAD,
      fingerprint: 'fp1',
    });
    expect(out.ledgerValid).toBe(false);
    expect(out.nextAction).toBe('repair-sequence');
  });
});

describe('authenticated actor resolution', () => {
  it('rejects a user response with no login', () => {
    resetGitHubRunner();
    const bare = new ConfigurableRunner();
    bare.actorRaw = JSON.stringify({ id: 1 });
    // Force the module-level resolution path rather than the runner seam.
    setGitHubRunner({ runGh: (args) => bare.runGh(args) } as GitHubRunner);
    expect(() => currentActor()).toThrow(
      'GitHub returned an invalid authenticated-user response',
    );
  });

  it('rejects the literal null a jq projection would print', () => {
    resetGitHubRunner();
    setGitHubRunner({ runGh: () => 'null' } as GitHubRunner);
    expect(() => currentActor()).toThrow(
      'GitHub returned an invalid authenticated-user response',
    );
  });
});

describe('historical marker detection', () => {
  it('flags legacy v1 records', () => {
    expect(
      rowsHaveHistoricalMarkers([
        { body: '<!-- local-review:v1 engine=claude -->\nlegacy' },
      ]),
    ).toBe(true);
  });

  it('flags a v3 marker that does not parse', () => {
    expect(
      rowsHaveHistoricalMarkers([
        { body: '<!-- local-review:v3 engine=claude round=nope -->\nbroken' },
      ]),
    ).toBe(true);
  });

  it('does not flag an ordinary comment', () => {
    expect(rowsHaveHistoricalMarkers([{ body: 'just a comment' }])).toBe(false);
  });

  it('does not flag a well-formed v3 finding', () => {
    const content = 'the finding\n';
    expect(
      rowsHaveHistoricalMarkers([
        {
          body: `${findingMarker({ fingerprint: 'fp1', content })}\n${content}`,
        },
      ]),
    ).toBe(false);
  });
});

describe('content files are decoded strictly', () => {
  it('rejects invalid UTF-8 rather than substituting replacement characters', () => {
    const path = join(dir, 'content.txt');
    writeFileSync(path, Buffer.from([0x68, 0x69, 0xff, 0xfe, 0x0a]));
    expect(() => readContent(path)).toThrow('content file must be valid UTF-8');
  });

  it('accepts valid multi-byte UTF-8 unchanged', () => {
    const path = join(dir, 'content-ok.txt');
    writeFileSync(path, 'findings — café 日本語\n', 'utf8');
    expect(readContent(path)).toBe('findings — café 日本語\n');
  });
});

describe('thread read-back is bound to its pull request and root comment', () => {
  const scoped = (
    overrides: Partial<GitHubReviewThreadNode> = {},
  ): GitHubReviewThreadNode =>
    ({
      id: 'PRRT_x',
      isResolved: false,
      repository: { nameWithOwner: REPO },
      pullRequest: { number: PR },
      comments: {
        nodes: [{ databaseId: 11 }, { databaseId: 12 }],
        pageInfo: { hasNextPage: false },
      },
      ...overrides,
    }) as GitHubReviewThreadNode;

  it('rejects a thread belonging to another repository', () => {
    runner.threadNodes = [
      scoped({ repository: { nameWithOwner: 'someone/else' } }),
    ];
    expect(() =>
      getThreadState('PRRT_x', undefined, { repo: REPO, pr: PR }),
    ).toThrow(`does not belong to ${REPO}#${PR}`);
  });

  it('rejects a thread belonging to another pull request', () => {
    runner.threadNodes = [scoped({ pullRequest: { number: 999 } })];
    expect(() =>
      getThreadState('PRRT_x', undefined, { repo: REPO, pr: PR }),
    ).toThrow(`does not belong to ${REPO}#${PR}`);
  });

  it('rejects a comment that is not the root of the thread', () => {
    runner.threadNodes = [scoped()];
    expect(() => getThreadState('PRRT_x', 12, { repo: REPO, pr: PR })).toThrow(
      '--comment-id is not the root comment of --thread-id',
    );
  });

  it('accepts the thread root in the requested pull request', () => {
    runner.threadNodes = [scoped()];
    expect(getThreadState('PRRT_x', 11, { repo: REPO, pr: PR })).toBe(false);
  });
});
