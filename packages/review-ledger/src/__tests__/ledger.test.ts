import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  correctSeverity,
  revalidateOccurrence,
  matchRevalidation,
  dispose,
  postFinding,
  preflightAnchor,
  preflightFix,
  preflightPublish,
  buildFindingBody,
  buildDispositionBody,
  buildSeverityCorrectionBody,
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
  public moveAfterResolution = false;
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
        in_reply_to_id?: number;
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
        in_reply_to_id: cmd.includes('/replies')
          ? Number(args[3]?.split('/').at(-2))
          : data?.in_reply_to_id,
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
      if (
        p?.query?.includes('resolveReviewThread') &&
        !p.query.includes('unresolveReviewThread')
      ) {
        const t = this.threads.find((th) => th.id === p.variables?.threadId);
        if (t) t.isResolved = true;
        if (this.moveAfterResolution) this.prHead = 'b'.repeat(40);
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

  describe('carry-forward revalidation', () => {
    const oldHead = '3333333333333333333333333333333333333333';
    const params = {
      repo: 'example/project',
      pr: 10,
      head: headSha,
      engine: 'codex' as const,
      round: 1,
      fingerprint: 'inherited',
      occurrence: 1,
      commentId: 101,
      threadId: 'carry-thread',
      content: `The defect remains reachable at ${headSha}: the same input bypasses the guard.\n`,
    };
    function run(cap = 4, actor = mock.actor) {
      const content = 'Authorized review run.\n';
      const digest = sha256Text(
        JSON.stringify({
          base: mock.prBase,
          content,
          max_rounds: cap,
          start_head: headSha,
          supersedes: null,
          tier: 'deep',
        }),
      );
      mock.issueComments = [
        {
          id: 50,
          user: { login: actor },
          body: `<!-- local-review-run:v1 id=${digest} tier=deep max-rounds=${cap} base=${mock.prBase} start-head=${headSha} supersedes=none content-sha256=${digest} -->\n${content}`,
        },
      ];
    }
    function syncThread() {
      mock.threads[0]!.comments.nodes = mock.reviewComments.map((row) => ({
        ...row,
        databaseId: row['id'] as number,
        author: row['user'] as { login: string },
      }));
    }
    beforeEach(() => {
      run();
      const finding = buildFindingBody({
        ...params,
        head: oldHead,
        severity: 'blocking',
        lens: 'correctness',
        content: 'Original reproduction.\n',
      });
      mock.reviewComments = [
        {
          id: 101,
          user: { login: mock.actor },
          body: finding.body,
          createdAt: '2025-01-01T00:00:00Z',
        },
      ];
      mock.threads = [
        {
          id: params.threadId,
          isResolved: false,
          repository: { nameWithOwner: params.repo },
          pullRequest: { number: params.pr },
          comments: { nodes: [], pageInfo: { hasNextPage: false } },
        },
      ];
      syncThread();
    });

    it('records an evidence-backed dismissal and resolves the blocking thread', () => {
      revalidateOccurrence(params);
      syncThread();
      const result = dispose({
        ...params,
        outcome: 'dismissed',
        content:
          'The input is rejected before the write, disproving the finding.',
      });
      expect(result).toMatchObject({ verified: true, resolved: true });
      expect(mock.threads[0]!.isResolved).toBe(true);
      syncThread();
      expect(verifyLedger(params).dispositions).toBe(1);
    });

    it('revalidates, authorizes, publishes, disposes, verifies and writes the current-head result without changing history or budget', () => {
      const original = structuredClone(mock.reviewComments[0]);
      const budget = structuredClone(mock.issueComments);
      const receipt = revalidateOccurrence(params);
      expect(receipt).toMatchObject({ verified: true, replayed: false });
      expect(revalidateOccurrence(params)).toMatchObject({ replayed: true });
      expect(mock.reviewComments).toHaveLength(2);
      expect(mock.reviewComments[0]).toEqual(original);
      expect(mock.issueComments).toEqual(budget);
      expect(preflightFix(params)).toMatchObject({
        severity: 'blocking',
        verified: true,
      });
      expect(
        preflightPublish({
          ...params,
          findings: [{ fingerprint: params.fingerprint, occurrence: 1 }],
        }),
      ).toMatchObject({ verified: true });
      syncThread();
      expect(reconcile(params)).toMatchObject({
        ledgerValid: true,
        undisposedOccurrences: [1],
        findings: [{ head: headSha, occurrence: 1 }],
        revalidations: [{ fromHead: oldHead, head: headSha, maxRounds: 4 }],
      });
      expect(() =>
        dispose({
          ...params,
          outcome: 'fixed',
          content: 'A false same-head fix.\n',
        }),
      ).toThrow(/published forward fix head/);
      const after = '4444444444444444444444444444444444444444';
      mock.prHead = after;
      dispose({
        ...params,
        head: after,
        outcome: 'fixed',
        content: 'Fixed the guard and verified the reproduction.\n',
      });
      syncThread();
      expect(verifyLedger({ ...params, head: after })).toMatchObject({
        verified: true,
      });
      const resultFile = join(tmpdir(), `carry-result-${process.pid}.json`);
      try {
        expect(
          writeResult({
            ...params,
            head: after,
            before: headSha,
            base: mock.prBase,
            classification: 'material',
            resultFile,
          }),
        ).toMatchObject({
          status: 'changed',
          findingFingerprints: ['inherited'],
        });
      } finally {
        if (existsSync(resultFile)) unlinkSync(resultFile);
      }
      expect(mock.reviewComments[0]).toEqual(original);
      expect(mock.issueComments).toEqual(budget);
      // A real recurrence remains available after the carried occurrence is settled.
      reopenOccurrence({
        ...params,
        head: after,
        occurrence: 2,
        severity: 'blocking',
        lens: 'correctness',
        content: 'A later recurrence.\n',
      });
      syncThread();
      expect(reconcile({ ...params, head: after })).toMatchObject({
        ledgerValid: true,
        undisposedOccurrences: [2],
      });
    });

    it.each([
      'foreign-finding',
      'foreign-run',
      'no-run',
      'ended-run',
      'stale-head',
      'same-head',
      'duplicate-root',
      'disposed',
      'fabricated-disposition',
      'cap-change',
      'round-cap',
      'wrong-thread',
      'empty-evidence',
    ])('rejects %s without writing', (scenario) => {
      let request = { ...params };
      if (scenario === 'foreign-finding')
        mock.reviewComments[0]!['user'] = { login: 'other-actor' };
      if (scenario === 'foreign-run') run(4, 'other-actor');
      if (scenario === 'no-run') mock.issueComments = [];
      if (scenario === 'ended-run') {
        const id = /id=([a-f0-9]+)/.exec(
          String(mock.issueComments[0]!['body']),
        )![1];
        mock.issueComments.push({
          id: 51,
          user: { login: mock.actor },
          body: `<!-- local-review-run-end:v1 id=${id} outcome=aborted head=${headSha} -->\nStopped.\n`,
        });
      }
      if (scenario === 'stale-head') request.head = oldHead;
      if (scenario === 'same-head') mock.prHead = request.head = oldHead;
      if (scenario === 'duplicate-root')
        mock.reviewComments.push({ ...mock.reviewComments[0]!, id: 102 });
      if (scenario === 'disposed' || scenario === 'fabricated-disposition') {
        mock.reviewComments.push({
          id: 103,
          user: { login: mock.actor },
          body: buildDispositionBody({
            ...params,
            head: oldHead,
            round: scenario === 'disposed' ? 1 : 2,
            outcome: 'fixed',
            content: 'Claimed fixed.\n',
          }).body,
        });
      }
      if (scenario === 'cap-change') run(3);
      if (scenario === 'round-cap') request.round = 5;
      if (scenario === 'wrong-thread') request.threadId = 'unrelated';
      if (scenario === 'empty-evidence') request.content = ' \n';
      syncThread();
      const before = structuredClone(mock.reviewComments);
      expect(() => revalidateOccurrence(request)).toThrow();
      expect(mock.reviewComments).toEqual(before);
    });

    it('rejects tampered run-cap evidence in every reader and fix gate', () => {
      revalidateOccurrence(params);
      const row = mock.reviewComments[1]!;
      row['body'] = String(row['body']).replace(
        '"maxRounds":4',
        '"maxRounds":3',
      );
      syncThread();
      expect(() => preflightFix(params)).toThrow(/cap|run/);
      expect(() => reconcile(params)).toThrow(/cap|run/);
      expect(() => verifyLedger(params)).toThrow(/cap|run/);
    });

    it('keeps the source marker and upward severity correction when revalidating', () => {
      mock.prHead = oldHead;
      mock.reviewComments[0]!['body'] = buildFindingBody({
        ...params,
        head: oldHead,
        severity: 'major',
        lens: 'correctness',
        content: 'Original major finding.\n',
      }).body;
      syncThread();
      correctSeverity({
        ...params,
        head: oldHead,
        severity: 'blocking',
        from: 'major',
        content: 'Blocking impact.\n',
      });
      const history = structuredClone(mock.reviewComments);
      mock.prHead = headSha;
      revalidateOccurrence(params);
      expect(preflightFix(params)).toMatchObject({ severity: 'blocking' });
      expect(mock.reviewComments.slice(0, 2)).toEqual(history);
      expect(
        matchRevalidation(String(mock.reviewComments[2]!['body'])),
      ).toMatchObject({ head: headSha, fromHead: oldHead });
    });

    it('can recover an undisposed carried finding after restart at the same head', () => {
      revalidateOccurrence(params);
      const oldRunId = /id=([a-f0-9]+)/.exec(
        String(mock.issueComments[0]!['body']),
      )![1];
      mock.issueComments.push({
        id: 51,
        user: { login: mock.actor },
        body: `<!-- local-review-run-end:v1 id=${oldRunId} outcome=aborted head=${headSha} -->`,
      });
      const content = 'Authorized restart after interruption.\n';
      const digest = sha256Text(
        JSON.stringify({
          base: mock.prBase,
          content,
          max_rounds: 4,
          start_head: headSha,
          supersedes: 50,
          tier: 'deep',
        }),
      );
      mock.issueComments.push({
        id: 52,
        user: { login: mock.actor },
        body: `<!-- local-review-run:v1 id=${digest} tier=deep max-rounds=4 base=${mock.prBase} start-head=${headSha} supersedes=50 content-sha256=${digest} -->\n${content}`,
      });
      expect(() => preflightFix(params)).toThrow(
        /different active run or pass/,
      );
      expect(() => revalidateOccurrence(params)).not.toThrow();
      expect(preflightFix(params)).toMatchObject({ verified: true });
      expect(revalidateOccurrence(params)).toMatchObject({ replayed: true });
      expect(() => revalidateOccurrence({ ...params, round: 2 })).toThrow(
        /forward head transition/,
      );
      const after = '4444444444444444444444444444444444444444';
      mock.prHead = after;
      dispose({
        ...params,
        head: after,
        outcome: 'fixed',
        content: 'Fixed after the authorized restart.\n',
      });
      syncThread();
      expect(verifyLedger({ ...params, head: after })).toMatchObject({
        verified: true,
      });
      const resultFile = join(tmpdir(), `restart-result-${process.pid}.json`);
      try {
        expect(
          writeResult({
            ...params,
            head: after,
            before: headSha,
            base: mock.prBase,
            classification: 'material',
            resultFile,
          }),
        ).toMatchObject({
          status: 'changed',
          findingFingerprints: ['inherited'],
        });
      } finally {
        if (existsSync(resultFile)) unlinkSync(resultFile);
      }
    });

    it('carries an uncarried finding into a run restarted at its own head', () => {
      // Run A posts the finding at H and aborts; run B restarts at the same H.
      mock.reviewComments[0]!['body'] = buildFindingBody({
        ...params,
        severity: 'blocking',
        lens: 'correctness',
        content: 'Original reproduction.\n',
      }).body;
      mock.reviewComments[0]!['createdAt'] = '2025-01-02T00:00:00Z';
      mock.reviewComments[0]!['created_at'] = '2025-01-02T00:00:00Z';
      mock.issueComments[0]!['created_at'] = '2025-01-01T00:00:00Z';
      syncThread();
      // Inside run A an unchanged head is still not a carry.
      expect(() => revalidateOccurrence(params)).toThrow(
        /forward head transition/,
      );
      const oldRunId = /id=([a-f0-9]+)/.exec(
        String(mock.issueComments[0]!['body']),
      )![1];
      mock.issueComments.push({
        id: 51,
        user: { login: mock.actor },
        created_at: '2025-01-03T00:00:00Z',
        body: `<!-- local-review-run-end:v1 id=${oldRunId} outcome=aborted head=${headSha} -->`,
      });
      const content = 'Authorized restart after interruption.\n';
      const digest = sha256Text(
        JSON.stringify({
          base: mock.prBase,
          content,
          max_rounds: 4,
          start_head: headSha,
          supersedes: 50,
          tier: 'deep',
        }),
      );
      mock.issueComments.push({
        id: 52,
        user: { login: mock.actor },
        created_at: '2025-01-04T00:00:00Z',
        body: `<!-- local-review-run:v1 id=${digest} tier=deep max-rounds=4 base=${mock.prBase} start-head=${headSha} supersedes=50 content-sha256=${digest} -->\n${content}`,
      });
      const history = structuredClone(mock.reviewComments);
      expect(revalidateOccurrence(params)).toMatchObject({
        verified: true,
        replayed: false,
      });
      mock.reviewComments[1]!['created_at'] = '2025-01-05T00:00:00Z';
      mock.reviewComments[1]!['createdAt'] = '2025-01-05T00:00:00Z';
      expect(mock.reviewComments.slice(0, 1)).toEqual(history);
      expect(revalidateOccurrence(params)).toMatchObject({ replayed: true });
      expect(() => revalidateOccurrence({ ...params, round: 2 })).toThrow(
        /forward head transition/,
      );
      expect(preflightFix(params)).toMatchObject({ verified: true });
      const after = '4444444444444444444444444444444444444444';
      mock.prHead = after;
      dispose({
        ...params,
        head: after,
        outcome: 'fixed',
        content: 'Fixed after the authorized restart.\n',
      });
      syncThread();
      expect(verifyLedger({ ...params, head: after })).toMatchObject({
        verified: true,
      });
      const resultFile = join(tmpdir(), `uncarried-result-${process.pid}.json`);
      try {
        expect(
          writeResult({
            ...params,
            head: after,
            before: headSha,
            base: mock.prBase,
            classification: 'material',
            resultFile,
          }),
        ).toMatchObject({
          status: 'changed',
          findingFingerprints: ['inherited'],
        });
      } finally {
        if (existsSync(resultFile)) unlinkSync(resultFile);
      }
    });

    it('refuses an unchanged-head carry when the finding or run lacks a timestamp', () => {
      mock.reviewComments[0]!['body'] = buildFindingBody({
        ...params,
        severity: 'blocking',
        lens: 'correctness',
        content: 'Original reproduction.\n',
      }).body;
      delete mock.reviewComments[0]!['createdAt'];
      syncThread();
      expect(() => revalidateOccurrence(params)).toThrow(
        /forward head transition/,
      );
      expect(mock.reviewComments).toHaveLength(1);
    });

    it('can revalidate again at a later head without inventing a new occurrence', () => {
      const original = structuredClone(mock.reviewComments[0]);
      revalidateOccurrence(params);
      const nextHead = '4444444444444444444444444444444444444444';
      mock.prHead = nextHead;
      revalidateOccurrence({
        ...params,
        head: nextHead,
        round: 3,
        content: `The same defect still reproduces at ${nextHead}.\n`,
      });
      expect(mock.reviewComments).toHaveLength(3);
      expect(mock.reviewComments[0]).toEqual(original);
      // codex read round 1 of this run, so round 3 is not a late joiner's read.
      mock.issueComments.push({
        id: 51,
        user: { login: mock.actor },
        body: `<!-- local-review-pass:v3 engine=codex round=1 base=${mock.prBase} head=${oldHead} result-sha256=${'d'.repeat(64)} -->\nReviewed.\n`,
      });
      expect(
        preflightFix({ ...params, head: nextHead, round: 3 }),
      ).toMatchObject({ stance: 'convergence', severity: 'blocking' });
      expect(() => preflightFix({ ...params, head: nextHead })).toThrow(
        /different active run or pass/,
      );
    });

    it.each([
      'fromHead',
      'head',
      'runId',
      'severity',
      'reply-root',
      'content-hash',
    ])('rejects mismatched or fabricated %s evidence', (field) => {
      revalidateOccurrence(params);
      const row = mock.reviewComments[1]!;
      const [marker, ...content] = String(row['body']).split('\n');
      const record = matchRevalidation(String(row['body']))!;
      const changed = { ...record } as Record<string, unknown>;
      if (field === 'fromHead') changed['fromHead'] = headSha;
      if (field === 'head') changed['head'] = oldHead;
      if (field === 'runId') changed['runId'] = '0'.repeat(64);
      if (field === 'severity') changed['severity'] = 'minor';
      if (field === 'reply-root') row['in_reply_to_id'] = 999;
      if (field === 'content-hash')
        row['body'] = `${marker}\nDifferent reproduction.\n`;
      else
        row['body'] =
          `<!-- local-review-revalidation:v1 ${JSON.stringify(changed, Object.keys(changed).sort())} -->\n${content.join('\n')}`;
      syncThread();
      expect(() => preflightFix(params)).toThrow();
      expect(() => reconcile(params)).toThrow();
    });

    it('refuses a fabricated disposition after revalidation and leaves the run cap alone', () => {
      revalidateOccurrence(params);
      mock.reviewComments.push({
        id: 301,
        user: { login: mock.actor },
        body: buildDispositionBody({
          ...params,
          round: 2,
          outcome: 'fixed',
          content: 'Invented outcome.\n',
        }).body,
      });
      expect(() => preflightFix(params)).toThrow(/fabricated disposition/);
      expect(() => reconcile(params)).toThrow(/fabricated disposition/);
      syncThread();
      expect(() => verifyLedger(params)).toThrow(/fabricated disposition/);
    });
  });

  it('reproduces the undisposed earlier-head blocker deadlock in both existing commands', () => {
    const oldHead = '3333333333333333333333333333333333333333';
    const finding = buildFindingBody({
      engine: 'codex',
      round: 1,
      head: oldHead,
      fingerprint: 'inherited',
      occurrence: 1,
      severity: 'blocking',
      lens: 'correctness',
      content: 'The defect still reproduces at the next head.\n',
    });
    mock.reviewComments.push({
      id: 101,
      user: { login: mock.actor },
      body: finding.body,
    });
    expect(() =>
      reopenOccurrence({
        repo: 'example/project',
        pr: 10,
        head: headSha,
        engine: 'codex',
        round: 1,
        fingerprint: 'inherited',
        occurrence: 2,
        severity: 'blocking',
        lens: 'correctness',
        commentId: 101,
        threadId: 'thread',
        content: 'Still reachable.\n',
      }),
    ).toThrow(
      'every prior finding occurrence must have exactly one disposition',
    );
    expect(() =>
      preflightFix({
        repo: 'example/project',
        pr: 10,
        head: headSha,
        engine: 'codex',
        round: 1,
        fingerprint: 'inherited',
        occurrence: 1,
      }),
    ).toThrow('requires exactly one authenticated finding at this head');
    expect(mock.reviewComments).toHaveLength(1);
    expect(mock.reviewComments[0]!.body).toBe(finding.body);
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
      // The engine has not read this run yet: a late joiner's cold read.
      expect(preflightFix({ ...params, round: 2 })).toMatchObject({
        stance: 'adversarial',
        severity: 'major',
      });
      mock.issueComments.push({
        id: 21,
        user: { login: mock.actor },
        body: `<!-- local-review-pass:v3 engine=${params.engine} round=1 base=${mock.prBase} head=${headSha} result-sha256=${'d'.repeat(64)} -->\nReviewed.\n`,
      });
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

  it.each(['unchanged', 'head-typo', 'post-write-move'])(
    'handles disposition head checks: %s',
    (scenario) => {
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

      const params: Parameters<typeof dispose>[0] = {
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
      };
      if (scenario === 'head-typo') {
        const before = JSON.stringify([
          mock.reviewComments,
          mock.issueComments,
          mock.threads,
        ]);
        expect(() => dispose({ ...params, head: 'a'.repeat(40) })).toThrow(
          /PR head mismatch/,
        );
        expect(
          JSON.stringify([
            mock.reviewComments,
            mock.issueComments,
            mock.threads,
          ]),
        ).toBe(before);

        mock.prHead = 'b'.repeat(40);
        expect(() => dispose(params)).toThrow(/PR head mismatch/);
        expect(
          JSON.stringify([
            mock.reviewComments,
            mock.issueComments,
            mock.threads,
          ]),
        ).toBe(before);
        mock.prHead = headSha;
      }
      if (scenario === 'post-write-move') {
        mock.moveAfterResolution = true;
        expect(() => dispose(params)).toThrow(/PR head mismatch/);
        expect(mock.reviewComments).toHaveLength(2);
        expect(mock.threads[0]!.isResolved).toBe(true);
        return;
      }
      const res = dispose(params);

      expect(res.verified).toBe(true);
      expect(res.resolved).toBe(true);
      expect(mock.threads[0]!.isResolved).toBe(true);
    },
  );

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

  it('appends verified upward severity corrections for unresolved findings and unlocks convergence fix gates only for the corrected finding', () => {
    const postRes1 = postFinding({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      path: 'src/index.ts',
      line: 2,
      engine: 'codex',
      round: 3,
      fingerprint: 'fp-escalated',
      occurrence: 1,
      severity: 'major',
      lens: 'security',
      content: 'Initial major finding prose.',
    });
    const postRes2 = postFinding({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      path: 'src/index.ts',
      line: 2,
      engine: 'codex',
      round: 3,
      fingerprint: 'fp-still-major',
      occurrence: 1,
      severity: 'major',
      lens: 'correctness',
      content: 'Unrelated major finding prose.',
    });

    mock.threads.push(
      {
        id: 'PRRT_escalated',
        isResolved: false,
        repository: { nameWithOwner: 'loomantix/platform-oss' },
        pullRequest: { number: 10 },
        comments: {
          nodes: [{ databaseId: postRes1.comment_id }],
          pageInfo: { hasNextPage: false },
        },
      },
      {
        id: 'PRRT_still_major',
        isResolved: false,
        repository: { nameWithOwner: 'loomantix/platform-oss' },
        pullRequest: { number: 10 },
        comments: {
          nodes: [{ databaseId: postRes2.comment_id }],
          pageInfo: { hasNextPage: false },
        },
      },
    );

    // Before correction, convergence preflight refuses the major finding.
    expect(() =>
      preflightFix({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        engine: 'codex',
        round: 3,
        fingerprint: 'fp-escalated',
        occurrence: 1,
      }),
    ).toThrowError(
      /convergence fixes require a posted blocking finding; fp-escalated occurrence 1 is major/,
    );

    const originalRootBody = String(mock.reviewComments[0]!['body']);
    const corrRes = correctSeverity({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      engine: 'codex',
      round: 3,
      fingerprint: 'fp-escalated',
      occurrence: 1,
      from: 'major',
      severity: 'blocking',
      commentId: postRes1.comment_id,
      threadId: 'PRRT_escalated',
      content:
        'Verified unauthenticated callers reach the write path and corrupt persistent records.',
    });

    expect(corrRes).toEqual({
      comment_id: expect.any(Number),
      from_severity: 'major',
      severity: 'blocking',
      replayed: false,
      thread_replayed: true,
      resolved: false,
      verified: true,
    });
    // Original finding marker and comment body are preserved verbatim.
    expect(mock.reviewComments[0]!['body']).toBe(originalRootBody);
    expect(mock.threads[0]!.isResolved).toBe(false);

    // Idempotent replay does not duplicate the correction comment.
    const countAfterFirst = mock.reviewComments.length;
    const replayRes = correctSeverity({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      engine: 'codex',
      round: 3,
      fingerprint: 'fp-escalated',
      occurrence: 1,
      severity: 'blocking',
      commentId: postRes1.comment_id,
      threadId: 'PRRT_escalated',
      content:
        'Verified unauthenticated callers reach the write path and corrupt persistent records.',
    });
    expect(replayRes.replayed).toBe(true);
    expect(replayRes.comment_id).toBe(corrRes.comment_id);
    expect(mock.reviewComments.length).toBe(countAfterFirst);

    // Now preflightFix and preflightPublish authorize ONLY fp-escalated.
    expect(
      preflightFix({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        engine: 'codex',
        round: 3,
        fingerprint: 'fp-escalated',
        occurrence: 1,
      }),
    ).toEqual({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      engine: 'codex',
      round: 3,
      fingerprint: 'fp-escalated',
      occurrence: 1,
      severity: 'blocking',
      stance: 'convergence',
      verified: true,
    });

    expect(
      preflightPublish({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        engine: 'codex',
        round: 3,
        findings: [{ fingerprint: 'fp-escalated', occurrence: 1 }],
      }).findings,
    ).toEqual([
      { fingerprint: 'fp-escalated', occurrence: 1, severity: 'blocking' },
    ]);

    // Uncorrected major finding on the same PR is still refused.
    expect(() =>
      preflightFix({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        engine: 'codex',
        round: 3,
        fingerprint: 'fp-still-major',
        occurrence: 1,
      }),
    ).toThrowError(
      /convergence fixes require a posted blocking finding; fp-still-major occurrence 1 is major/,
    );
    expect(() =>
      preflightPublish({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        engine: 'codex',
        round: 3,
        findings: [
          { fingerprint: 'fp-escalated', occurrence: 1 },
          { fingerprint: 'fp-still-major', occurrence: 1 },
        ],
      }),
    ).toThrowError(
      /convergence fixes require a posted blocking finding; fp-still-major occurrence 1 is major/,
    );

    // Reconcile reflects effective blocking severity and the correction record.
    const reconciled = reconcile({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      fingerprint: 'fp-escalated',
    });
    expect(reconciled.ledgerValid).toBe(true);
    expect(reconciled.nextAction).toBe('dispose');
    expect(reconciled.findings[0]!['severity']).toBe('blocking');
    expect(reconciled.severityCorrections).toHaveLength(1);

    // Deferring the corrected blocker is rejected.
    expect(() =>
      dispose({
        repo: 'loomantix/platform-oss',
        pr: 10,
        head: headSha,
        engine: 'codex',
        round: 3,
        fingerprint: 'fp-escalated',
        occurrence: 1,
        outcome: 'deferred',
        commentId: postRes1.comment_id,
        threadId: 'PRRT_escalated',
        content: 'Attempted deferral.',
      }),
    ).toThrowError('blocking local-review findings cannot be deferred');

    // Fixing the corrected blocker succeeds.
    const dispRes = dispose({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      engine: 'codex',
      round: 3,
      fingerprint: 'fp-escalated',
      occurrence: 1,
      outcome: 'fixed',
      commentId: postRes1.comment_id,
      threadId: 'PRRT_escalated',
      content: 'Fixed by enforcing authentication before handler execution.',
    });
    expect(dispRes.resolved).toBe(true);
  });

  it('fails closed on invalid severity correction attempts and reconciles corrupted corrections', () => {
    const postRes = postFinding({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      path: 'src/index.ts',
      line: 2,
      engine: 'codex',
      round: 3,
      fingerprint: 'fp-guard',
      occurrence: 1,
      severity: 'major',
      lens: 'security',
      content: 'Initial major finding.',
    });
    mock.threads.push({
      id: 'PRRT_guard',
      isResolved: false,
      repository: { nameWithOwner: 'loomantix/platform-oss' },
      pullRequest: { number: 10 },
      comments: {
        nodes: [{ databaseId: postRes.comment_id }],
        pageInfo: { hasNextPage: false },
      },
    });

    const baseParams = {
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      engine: 'codex' as const,
      round: 3,
      fingerprint: 'fp-guard',
      occurrence: 1,
      commentId: postRes.comment_id,
      threadId: 'PRRT_guard',
      content: 'Impact evidence.',
    };

    // Lowering or same-severity attempts fail closed.
    expect(() =>
      correctSeverity({ ...baseParams, severity: 'minor' }),
    ).toThrowError('severity correction must increase severity');
    expect(() =>
      correctSeverity({ ...baseParams, severity: 'major' }),
    ).toThrowError('severity correction must increase severity');

    // Wrong from-severity fails closed.
    expect(() =>
      correctSeverity({ ...baseParams, from: 'minor', severity: 'blocking' }),
    ).toThrowError(
      'severity correction from=minor does not match current finding severity major',
    );

    // Wrong engine, round, occurrence, or root commentId fail closed.
    expect(() =>
      correctSeverity({
        ...baseParams,
        engine: 'claude',
        severity: 'blocking',
      }),
    ).toThrowError(
      'severity correction does not identify an existing finding occurrence at this head, engine, and round',
    );
    expect(() =>
      correctSeverity({ ...baseParams, round: 2, severity: 'blocking' }),
    ).toThrowError(
      'severity correction does not identify an existing finding occurrence at this head, engine, and round',
    );
    expect(() =>
      correctSeverity({ ...baseParams, occurrence: 2, severity: 'blocking' }),
    ).toThrowError(
      'severity correction does not identify an existing finding occurrence at this head, engine, and round',
    );
    expect(() =>
      correctSeverity({ ...baseParams, commentId: 999, severity: 'blocking' }),
    ).toThrowError('--comment-id is not the root comment of --thread-id');

    // Resolved thread fails closed.
    mock.threads[0]!.isResolved = true;
    expect(() =>
      correctSeverity({ ...baseParams, severity: 'blocking' }),
    ).toThrowError('cannot correct severity on a resolved review thread');
    mock.threads[0]!.isResolved = false;

    // Disposed finding fails closed.
    dispose({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      engine: 'codex',
      round: 3,
      fingerprint: 'fp-guard',
      occurrence: 1,
      outcome: 'deferred',
      commentId: postRes.comment_id,
      threadId: 'PRRT_guard',
      content: 'Deferred to follow-up.',
    });
    mock.threads[0]!.isResolved = false;
    expect(() =>
      correctSeverity({ ...baseParams, severity: 'blocking' }),
    ).toThrowError('cannot correct severity of a disposed finding');

    // A correction injected after disposition causes reconcile to report repair-sequence.
    const postDispCorrection = buildSeverityCorrectionBody({
      engine: 'codex',
      round: 3,
      head: headSha,
      fingerprint: 'fp-guard',
      occurrence: 1,
      from: 'major',
      to: 'blocking',
      content: 'Late correction after disposition.',
    });
    mock.reviewComments.push({
      id: 888,
      databaseId: 888,
      user: { login: mock.actor },
      author: { login: mock.actor },
      body: postDispCorrection.body,
    });
    const badReconcile = reconcile({
      repo: 'loomantix/platform-oss',
      pr: 10,
      head: headSha,
      fingerprint: 'fp-guard',
    });
    expect(badReconcile.ledgerValid).toBe(false);
    expect(badReconcile.nextAction).toBe('repair-sequence');
  });
});
