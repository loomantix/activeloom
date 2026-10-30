import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  dispose,
  postFinding,
  preflightAnchor,
  preflightFix,
  preflightPublish,
  buildFindingBody,
  buildDispositionBody,
  sha256Text,
  reconcile,
  reopenOccurrence,
  resolve,
  verifyLedger,
  writeResult,
} from '../index.js';
import { resetGitHubRunner, setGitHubRunner } from '../github.js';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { existsSync, unlinkSync, writeFileSync } from 'node:fs';
import type { GitHubReviewThreadNode, GitHubRunner } from '../types.js';

class MockGitHubRunner implements GitHubRunner {
  public actor = 'test-actor';
  public prFiles: Record<string, string | null> = {};
  public reviewComments: Array<Record<string, unknown>> = [];
  public issueComments: Array<Record<string, unknown>> = [];
  public threads: GitHubReviewThreadNode[] = [];
  public commentIdSeq = 200;
  public prHead = '1111111111111111111111111111111111111111';
  public prBase = '2222222222222222222222222222222222222222';
  public driftAfterInventory = false;
  public calls: string[] = [];

  runGh(args: string[], payload?: unknown): string {
    const cmd = args.join(' ');
    if (args[0] === 'api' && args[1] === 'user') {
      this.calls.push('user');
      return JSON.stringify({ login: this.actor });
    }
    if (cmd.includes('pr view') && cmd.includes('baseRefOid')) {
      return this.prBase;
    }
    if (cmd.includes('pr view')) {
      return this.prHead;
    }
    if (cmd.includes('repos/') && cmd.includes('/files?per_page=100')) {
      this.calls.push('files');
      const rows = Object.entries(this.prFiles).map(([filename, patch]) => ({
        filename,
        patch,
      }));
      return JSON.stringify([rows]);
    }
    if (
      cmd.includes('repos/') &&
      cmd.includes('/pulls/') &&
      cmd.includes('/comments?per_page=100')
    ) {
      return JSON.stringify([this.reviewComments]);
    }
    if (
      cmd.includes('repos/') &&
      cmd.includes('/issues/') &&
      cmd.includes('/comments?per_page=100')
    ) {
      this.calls.push('inventory');
      if (this.driftAfterInventory) this.actor = 'different-actor';
      return JSON.stringify([this.issueComments]);
    }
    if (cmd.includes('repos/') && cmd.includes('/pulls/comments/')) {
      const id = parseInt(args[1]!.split('/').pop()!, 10);
      const row = this.reviewComments.find((c) => c['id'] === id);
      return JSON.stringify(
        row ?? { id, user: { login: this.actor }, body: '' },
      );
    }
    if (cmd.includes('repos/') && cmd.includes('/issues/comments/')) {
      const id = parseInt(args[1]!.split('/').pop()!, 10);
      const row = this.issueComments.find((c) => c['id'] === id);
      return JSON.stringify(
        row ?? { id, user: { login: this.actor }, body: '' },
      );
    }
    if (
      cmd.includes('-X POST') &&
      (cmd.includes('/pulls/') || cmd.includes('/issues/'))
    ) {
      const id = this.commentIdSeq++;
      const data = payload as {
        body?: string;
        commit_id?: string;
        path?: string;
        line?: number;
        side?: string;
      };
      const row = {
        id,
        databaseId: id,
        user: { login: this.actor },
        author: { login: this.actor },
        body: data?.body ?? '',
        commit_id: data?.commit_id,
        path: data?.path,
        line: data?.line,
        side: data?.side,
      };
      if (cmd.includes('/pulls/')) {
        this.reviewComments.push(row);
      } else {
        this.issueComments.push(row);
      }
      return JSON.stringify({ id });
    }
    if (cmd.includes('api graphql')) {
      if (
        args.some(
          (arg) =>
            arg.startsWith('query=') && arg.includes('issueOrPullRequest'),
        )
      ) {
        return JSON.stringify([
          {
            data: {
              repository: {
                issueOrPullRequest: {
                  comments: {
                    totalCount: this.issueComments.length,
                    pageInfo: { hasNextPage: false, endCursor: null },
                    nodes: this.issueComments.map((row) => ({
                      databaseId: row['id'],
                      body: row['body'],
                      author: row['user'],
                      createdAt: '2026-01-01T00:00:00Z',
                      lastEditedAt: null,
                    })),
                  },
                },
              },
            },
          },
        ]);
      }
      if (cmd.includes('reviewThreads(first:100')) {
        return JSON.stringify([
          {
            data: {
              repository: {
                pullRequest: {
                  reviewThreads: {
                    nodes: this.threads,
                    pageInfo: { hasNextPage: false, endCursor: null },
                  },
                },
              },
            },
          },
        ]);
      }
      const p = payload as {
        query?: string;
        variables?: { threadId?: string };
      };
      if (p?.query?.includes('PullRequestReviewThread')) {
        const t = this.threads.find((th) => th.id === p.variables?.threadId);
        return JSON.stringify({
          data: {
            node: t ?? {
              id: p.variables?.threadId,
              isResolved: false,
              comments: { nodes: [], pageInfo: { hasNextPage: false } },
            },
          },
        });
      }
      if (p?.query?.includes('resolveReviewThread')) {
        const t = this.threads.find((th) => th.id === p.variables?.threadId);
        if (t) t.isResolved = true;
        return JSON.stringify({
          data: {
            resolveReviewThread: {
              thread: { id: p.variables?.threadId, isResolved: true },
            },
          },
        });
      }
      if (p?.query?.includes('unresolveReviewThread')) {
        const t = this.threads.find((th) => th.id === p.variables?.threadId);
        if (t) t.isResolved = false;
        return JSON.stringify({
          data: {
            unresolveReviewThread: {
              thread: { id: p.variables?.threadId, isResolved: false },
            },
          },
        });
      }
    }
    throw new Error(`Unexpected mock GitHub operation: ${cmd}`);
  }

  gitRevList(before: string, head: string): string[] {
    return [head];
  }

  gitCompare(repo: string, before: string, after: string): unknown {
    return { status: 'ahead', merge_base_commit: { sha: before } };
  }

  runGit(args: string[]): string {
    if (args[0] === 'rev-parse' && args[1] === 'HEAD') return this.prHead;
    if (args[0] === 'rev-parse' && args[1] === '--verify') {
      return args[2]!.replace(/\^\{commit\}$/, '');
    }
    return '';
  }

  isAncestor(): boolean {
    return true;
  }
}

describe('ledger operations and workflow verification', () => {
  let mock: MockGitHubRunner;
  const headSha = '1111111111111111111111111111111111111111';
  const samplePatch = '@@ -1,5 +1,6 @@\n context\n+new line\n';

  beforeEach(() => {
    resetGitHubRunner();
    mock = new MockGitHubRunner();
    mock.prFiles = { 'src/index.ts': samplePatch };
    setGitHubRunner(mock);
  });

  afterEach(() => resetGitHubRunner());

  it('verifies anchor with preflightAnchor', () => {
    const res = preflightAnchor({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      path: 'src/index.ts',
      line: 2,
      side: 'RIGHT',
    });
    expect(res.verified).toBe(true);
    expect(res.anchor).toBe('RIGHT:2');
  });

  it('rejects actor drift during preflight inventory before reading the diff', () => {
    mock.driftAfterInventory = true;
    expect(() =>
      preflightAnchor({
        repo: 'example/project',
        pr: 10,
        head: headSha,
        path: 'src/index.ts',
        line: 2,
        side: 'RIGHT',
      }),
    ).toThrow(/authenticated GitHub actor changed/);
    expect(mock.calls).toEqual(['user', 'inventory', 'user']);
    expect(mock.reviewComments).toEqual([]);
    expect(mock.issueComments).toEqual([]);
  });

  describe('preflight-fix', () => {
    const params = {
      repo: 'example/repo',
      pr: 10,
      head: headSha,
      engine: 'codex' as const,
      round: 3,
      fingerprint: 'window',
      occurrence: 1,
    };
    function post(severity: 'blocking' | 'major' | 'minor' | 'nit', round = 3) {
      mock.reviewComments.push({
        id: 201,
        user: { login: mock.actor },
        body: buildFindingBody({
          ...params,
          round,
          severity,
          lens: 'code-reviewer',
          content: 'Reachable defect.',
        }).body,
      });
    }

    it.each(['major', 'minor', 'nit'] as const)(
      'rejects a convergence %s before any write',
      (severity) => {
        post(severity);
        const before = JSON.stringify(mock.reviewComments);
        expect(() => preflightFix(params)).toThrow(
          /convergence fixes require a posted blocking finding/,
        );
        expect(JSON.stringify(mock.reviewComments)).toBe(before);
        expect(mock.commentIdSeq).toBe(200);
      },
    );

    it('permits a posted blocker in convergence', () => {
      post('blocking');
      expect(preflightFix(params)).toMatchObject({
        verified: true,
        severity: 'blocking',
        stance: 'convergence',
      });
    });

    it('permits a major in an adversarial round', () => {
      post('major', 1);
      expect(preflightFix({ ...params, round: 1 })).toMatchObject({
        stance: 'adversarial',
      });
    });

    it('uses the authenticated Lean run for the round-two boundary', () => {
      const content = 'Authorized run.\n';
      const digest = sha256Text(
        JSON.stringify({
          base: mock.prBase,
          content,
          max_rounds: 2,
          start_head: headSha,
          supersedes: null,
          tier: 'lean',
        }),
      );
      mock.issueComments = [
        {
          id: 20,
          user: { login: mock.actor },
          body: `<!-- local-review-run:v1 id=${digest} tier=lean max-rounds=2 base=${mock.prBase} start-head=${headSha} supersedes=none content-sha256=${digest} -->\n${content}`,
        },
      ];
      post('major', 2);
      expect(() => preflightFix({ ...params, round: 2 })).toThrow(
        /convergence fixes require/,
      );
    });

    it.each([
      { head: 'a'.repeat(40) },
      { engine: 'claude' as const },
      { round: 4 },
      { fingerprint: 'other' },
      { occurrence: 2 },
    ])('rejects an identity mismatch: %j', (change) => {
      post('blocking');
      expect(() => preflightFix({ ...params, ...change })).toThrow();
    });

    it('rejects a missing or foreign-owned finding', () => {
      expect(() => preflightFix(params)).toThrow(
        /exactly one authenticated finding/,
      );
      post('blocking');
      mock.reviewComments[0]!['user'] = { login: 'someone-else' };
      expect(() => preflightFix(params)).toThrow(
        /exactly one authenticated finding/,
      );
    });

    it('rejects tampered content', () => {
      post('blocking');
      mock.reviewComments[0]!['body'] += 'tampered';
      expect(() => preflightFix(params)).toThrow(/digest|hash/);
    });

    it('rejects duplicate, superseded, and already disposed findings', () => {
      post('blocking');
      post('blocking');
      expect(() => preflightFix(params)).toThrow(/exactly one/);
      mock.reviewComments.pop();
      mock.reviewComments.push({
        id: 202,
        user: { login: mock.actor },
        body: buildFindingBody({
          ...params,
          occurrence: 2,
          severity: 'blocking',
          lens: 'code-reviewer',
          content: 'Later occurrence.',
        }).body,
      });
      expect(() => preflightFix(params)).toThrow(/latest finding occurrence/);
      mock.reviewComments.pop();
      mock.reviewComments.push({
        id: 202,
        user: { login: mock.actor },
        body: buildDispositionBody({
          ...params,
          outcome: 'deferred',
          content: 'Follow-up.',
        }).body,
      });
      expect(() => preflightFix(params)).toThrow(/undisposed finding/);
    });
  });

  describe('preflight-publish', () => {
    const params = {
      repo: 'example/repo',
      pr: 10,
      head: headSha,
      engine: 'codex' as const,
      round: 3,
    };
    let nextId = 300;
    function post(
      fingerprint: string,
      severity: 'blocking' | 'major' | 'minor' | 'nit',
      round = 3,
    ) {
      mock.reviewComments.push({
        id: nextId++,
        user: { login: mock.actor },
        body: buildFindingBody({
          ...params,
          round,
          fingerprint,
          occurrence: 1,
          severity,
          lens: 'code-reviewer',
          content: 'Reachable defect.',
        }).body,
      });
    }
    const fixes = (...fingerprints: string[]) =>
      fingerprints.map((fingerprint) => ({ fingerprint, occurrence: 1 }));

    it('permits posted blockers in convergence and reports each severity', () => {
      post('window', 'blocking');
      post('lease', 'blocking');
      expect(
        preflightPublish({ ...params, findings: fixes('window', 'lease') }),
      ).toMatchObject({
        stance: 'convergence',
        verified: true,
        findings: [
          { fingerprint: 'window', severity: 'blocking' },
          { fingerprint: 'lease', severity: 'blocking' },
        ],
      });
    });

    it('refuses a convergence push that fixes a non-blocking finding', () => {
      post('window', 'blocking');
      post('label', 'major');
      const before = JSON.stringify(mock.reviewComments);
      expect(() =>
        preflightPublish({ ...params, findings: fixes('window', 'label') }),
      ).toThrow(
        /label occurrence 1 is major: defer it and leave its change out/,
      );
      expect(JSON.stringify(mock.reviewComments)).toBe(before);
    });

    it('permits a non-blocking fix in an adversarial round', () => {
      post('cleanup', 'nit', 1);
      expect(
        preflightPublish({ ...params, round: 1, findings: fixes('cleanup') }),
      ).toMatchObject({ stance: 'adversarial' });
    });

    it('refuses a fix whose finding was not posted before the push', () => {
      post('window', 'major', 1);
      expect(() =>
        preflightPublish({
          ...params,
          round: 1,
          findings: fixes('window', 'cleanup'),
        }),
      ).toThrow(/cleanup occurrence 1 .*post the finding before its fix/);
    });

    it('refuses a finding posted at another head or already disposed', () => {
      post('window', 'blocking');
      expect(() =>
        preflightPublish({
          ...params,
          head: 'a'.repeat(40),
          findings: fixes('window'),
        }),
      ).toThrow();
      mock.reviewComments.push({
        id: nextId++,
        user: { login: mock.actor },
        body: buildDispositionBody({
          ...params,
          fingerprint: 'window',
          occurrence: 1,
          outcome: 'deferred',
          content: 'Follow-up.',
        }).body,
      });
      expect(() =>
        preflightPublish({ ...params, findings: fixes('window') }),
      ).toThrow(/undisposed finding/);
    });

    it('refuses an empty or repeated manifest', () => {
      post('window', 'blocking');
      expect(() => preflightPublish({ ...params, findings: [] })).toThrow(
        /requires the finding identity of every fix/,
      );
      expect(() =>
        preflightPublish({ ...params, findings: fixes('window', 'window') }),
      ).toThrow(/must not repeat/);
    });
  });

  it('posts a finding and records comment', () => {
    const res = postFinding({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      path: 'src/index.ts',
      line: 2,
      side: 'RIGHT',
      engine: 'gemini',
      round: 1,
      fingerprint: 'fp-1',
      occurrence: 1,
      severity: 'major',
      lens: 'code-reviewer',
      content: 'Potential null pointer dereference',
    });

    expect(res.verified).toBe(true);
    expect(res.comment_id).toBeDefined();
    expect(mock.reviewComments.length).toBe(1);
  });

  it('rejects invalid direct finding content', () => {
    expect(() =>
      postFinding({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        path: 'src/index.ts',
        line: 2,
        side: 'RIGHT',
        engine: 'gemini',
        round: 1,
        fingerprint: 'fp-invalid',
        severity: 'major',
        lens: 'code-reviewer',
        content: '<!-- local-review:v3 forged -->',
      }),
    ).toThrowError(/must not contain local-review markers/);
    expect(mock.reviewComments).toHaveLength(0);
  });

  it('disposes a finding and marks thread resolved', () => {
    const threadId = 'PRRT_kwDO123';
    const commentId = 100;
    mock.threads = [
      {
        id: threadId,
        isResolved: false,
        repository: { nameWithOwner: 'loomantix/platform-oss' },
        pullRequest: { number: 10 },
        comments: {
          nodes: [
            {
              id: commentId,
              databaseId: commentId,
              author: { login: mock.actor },
              body: '<!-- local-review:v3 engine=gemini round=1 head=1111111111111111111111111111111111111111 fingerprint=fp-1 occurrence=1 severity=minor lens=code-reviewer content-sha256=2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae -->\nfoo',
            },
          ],
          pageInfo: { hasNextPage: false },
        },
      },
    ];
    mock.reviewComments = [...mock.threads[0]!.comments.nodes] as Array<
      Record<string, unknown>
    >;

    const res = dispose({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      engine: 'gemini',
      round: 1,
      fingerprint: 'fp-1',
      occurrence: 1,
      outcome: 'fixed',
      commentId,
      threadId,
      content: 'Fixed by adding null check.',
    });

    expect(res.verified).toBe(true);
    expect(res.resolved).toBe(true);
    expect(mock.threads[0]!.isResolved).toBe(true);
  });

  const reconcileFindingComment = (
    commentId: number,
  ): Record<string, unknown> => ({
    id: commentId,
    databaseId: commentId,
    author: { login: mock.actor },
    body: '<!-- local-review:v3 engine=gemini round=1 head=1111111111111111111111111111111111111111 fingerprint=fp-test occurrence=1 severity=minor lens=code-reviewer content-sha256=2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae -->\nfoo',
  });

  it('reconciles findings and dispositions', () => {
    const commentId = 100;
    const comment = reconcileFindingComment(commentId);
    mock.reviewComments = [comment];
    mock.threads = [
      {
        id: 'PRRT_reconcile',
        isResolved: false,
        repository: { nameWithOwner: 'loomantix/platform-oss' },
        pullRequest: { number: 10 },
        comments: { nodes: [comment], pageInfo: { hasNextPage: false } },
      },
    ] as unknown as typeof mock.threads;

    const report = reconcile({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      fingerprint: 'fp-test',
    });

    expect(report.sequenceValid).toBe(true);
    expect(report.undisposedOccurrences).toEqual([1]);
    expect(report.nextAction).toBe('dispose');
    // Recovery needs an actionable thread, not just a verdict.
    expect(report.threadId).toBe('PRRT_reconcile');
    expect(report.threadResolved).toBe(false);
  });

  it('reports no thread for a fingerprint that was never posted', () => {
    mock.reviewComments = [];
    mock.threads = [];

    const report = reconcile({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      fingerprint: 'fp-never-posted',
    });

    expect(report.nextAction).toBe('post-finding');
    expect(report.threadId).toBeNull();
    expect(report.threadResolved).toBeNull();
  });

  it('resolves the root thread despite an unrelated truncated discussion', () => {
    // A 100-comment discussion elsewhere on the PR says nothing about this
    // fingerprint. Requiring its full history to answer a root-thread question
    // would make recovery fail on exactly the long-lived PRs it is for.
    const commentId = 100;
    const comment = reconcileFindingComment(commentId);
    mock.reviewComments = [comment];
    mock.threads = [
      {
        id: 'PRRT_unrelated',
        isResolved: false,
        repository: { nameWithOwner: 'loomantix/platform-oss' },
        pullRequest: { number: 10 },
        comments: {
          nodes: [{ databaseId: 999, author: { login: mock.actor }, body: '' }],
          pageInfo: { hasNextPage: true },
        },
      },
      {
        id: 'PRRT_reconcile',
        isResolved: true,
        repository: { nameWithOwner: 'loomantix/platform-oss' },
        pullRequest: { number: 10 },
        comments: { nodes: [comment], pageInfo: { hasNextPage: false } },
      },
    ] as unknown as typeof mock.threads;

    const report = reconcile({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      fingerprint: 'fp-test',
    });

    expect(report.threadId).toBe('PRRT_reconcile');
    expect(report.threadResolved).toBe(true);
  });

  it('refuses to guess when the root comment matches no review thread', () => {
    mock.reviewComments = [reconcileFindingComment(100)];
    mock.threads = [];

    expect(() =>
      reconcile({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        fingerprint: 'fp-test',
      }),
    ).toThrow(/exactly one root review thread/);
  });

  it('resolves a review thread', () => {
    mock.threads = [
      {
        id: 'PRRT_test',
        isResolved: false,
        repository: { nameWithOwner: 'loomantix/platform-oss' },
        pullRequest: { number: 10 },
        comments: { nodes: [], pageInfo: { hasNextPage: false } },
      },
    ];
    const res = resolve({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      threadId: 'PRRT_test',
    });
    expect(res.resolved).toBe(true);
    expect(mock.threads[0]!.isResolved).toBe(true);
  });

  it('refuses to resolve a thread outside the requested pull request', () => {
    mock.threads = [
      {
        id: 'PRRT_other',
        isResolved: false,
        repository: { nameWithOwner: 'loomantix/platform-oss' },
        pullRequest: { number: 10 },
        comments: { nodes: [], pageInfo: { hasNextPage: false } },
      },
    ];
    expect(() =>
      resolve({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        threadId: 'PRRT_not-in-pr',
      }),
    ).toThrowError(/does not belong to the requested PR/);
  });

  it('asserts the actor before writing a result', () => {
    expect(() =>
      writeResult({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        engine: 'codex',
        round: 1,
        base: mock.prBase,
        before: headSha,
        resultFile: join(tmpdir(), 'unused-review-result.json'),
        actor: 'somebody-else',
      }),
    ).toThrowError(/authenticated GitHub actor changed/);
  });

  it('verifies result evidence by reading the result file', () => {
    const resultFile = join(tmpdir(), `verify-result-${Date.now()}.json`);
    try {
      writeFileSync(
        resultFile,
        `${JSON.stringify({
          version: 3,
          status: 'clean',
          engine: 'codex',
          round: 1,
          baseSha: mock.prBase,
          beforeSha: headSha,
          afterSha: headSha,
          classification: null,
          findingFingerprints: [],
          finalLaneComplete: true,
        })}\n`,
      );
      expect(
        verifyLedger({
          repo: 'loomantix/platform-oss',
          pr: 10,
          head: headSha,
          engine: 'codex',
          round: 1,
          base: mock.prBase,
          before: headSha,
          resultFile,
        }).verified,
      ).toBe(true);
    } finally {
      if (existsSync(resultFile)) unlinkSync(resultFile);
    }
  });
});
