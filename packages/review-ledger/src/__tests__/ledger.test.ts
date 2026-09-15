import { beforeEach, describe, expect, it } from 'vitest';
import {
  dispose,
  postFinding,
  preflightAnchor,
  reconcile,
  reopenOccurrence,
  resolve,
  verifyLedger,
  writeResult,
} from '../index.js';
import { setGitHubRunner } from '../github.js';
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

  runGh(args: string[], payload?: unknown): string {
    const cmd = args.join(' ');
    if (cmd.includes('api user --jq .login')) {
      return this.actor;
    }
    if (cmd.includes('pr view') && cmd.includes('baseRefOid')) {
      return this.prBase;
    }
    if (cmd.includes('pr view')) {
      return this.prHead;
    }
    if (cmd.includes('repos/') && cmd.includes('/files?per_page=100')) {
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
    return '{}';
  }

  currentActor(): string {
    return this.actor;
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
    mock = new MockGitHubRunner();
    mock.prFiles = { 'src/index.ts': samplePatch };
    setGitHubRunner(mock);
  });

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
