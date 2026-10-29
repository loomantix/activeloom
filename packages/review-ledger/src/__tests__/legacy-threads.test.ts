import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { resetGitHubRunner, setGitHubRunner } from '../github.js';
import { reply, threadProtocolRecords } from '../ledger.js';
import type { GitHubReviewThreadNode } from '../types.js';

const HEAD = 'a'.repeat(40);
const finding = (round = 1, extra = '') =>
  `<!-- local-review:v1 engine=codex round=${round} head=${HEAD} fingerprint=example${extra} -->\nA verified finding.`;
const disposition = (engine = 'codex', round = 1, fingerprint = 'example') =>
  `<!-- local-review-disposition:v1 engine=${engine} round=${round} head=${HEAD} fingerprint=${fingerprint} outcome=fixed -->\nVerified correction.`;

function thread(bodies: string[], resolved = true): GitHubReviewThreadNode {
  return {
    id: 'thread',
    isResolved: resolved,
    repository: { nameWithOwner: 'example/repo' },
    pullRequest: { number: 1 },
    comments: {
      nodes: bodies.map((body, index) => ({
        databaseId: index + 1,
        body,
        author: { login: 'reviewer' },
      })),
      pageInfo: { hasNextPage: false },
    },
  } as GitHubReviewThreadNode;
}

beforeEach(() => {
  resetGitHubRunner();
  setGitHubRunner({
    runGh: (args) => {
      if (args[0] === 'api' && args[1] === 'user') {
        return JSON.stringify({ login: 'reviewer' });
      }
      throw new Error(`Unexpected legacy parser operation: ${args.join(' ')}`);
    },
    runGit: () => '',
  });
});
afterEach(() => resetGitHubRunner());

describe('structured legacy disposition repair', () => {
  it.each([
    'preflight',
    'inventory',
    'threads',
    'post',
    'readback',
    'final-head',
    'readback-author',
  ])(
    'rejects operation actor drift at %s without fabricating success',
    (stage) => {
      const directory = mkdtempSync(join(tmpdir(), 'legacy-reply-drift-'));
      const contentFile = join(directory, 'reply.md');
      writeFileSync(contentFile, 'Verified correction.\n');
      let actor = 'reviewer';
      let heads = 0;
      const mutations: Array<{ body: string; author: string }> = [];
      const record = thread([finding()]);
      const originalThread = JSON.stringify(record);
      setGitHubRunner({
        runGh: (args, payload) => {
          if (args[0] === 'api' && args[1] === 'user') {
            return JSON.stringify({ login: actor });
          }
          if (args[0] === 'pr' && args[1] === 'view') {
            heads += 1;
            if (
              stage === 'preflight' ||
              (stage === 'final-head' && heads === 2)
            )
              actor = 'different-actor';
            return HEAD;
          }
          if (
            args.includes('repos/example/repo/issues/1/comments?per_page=100')
          ) {
            if (stage === 'inventory') actor = 'different-actor';
            return JSON.stringify([[]]);
          }
          if (
            args.includes('graphql') &&
            args.some((arg) => arg.includes('reviewThreads(first:100'))
          ) {
            if (stage === 'threads') actor = 'different-actor';
            return JSON.stringify([
              {
                data: {
                  repository: {
                    pullRequest: {
                      reviewThreads: {
                        nodes: [record],
                        pageInfo: { hasNextPage: false },
                      },
                    },
                  },
                },
              },
            ]);
          }
          if (
            args.includes('POST') &&
            args.includes('repos/example/repo/pulls/1/comments/1/replies')
          ) {
            const body = (payload as { body: string }).body;
            mutations.push({ body, author: actor });
            if (stage === 'post') actor = 'different-actor';
            return JSON.stringify({ id: 99 });
          }
          if (args.includes('repos/example/repo/pulls/comments/99')) {
            if (stage === 'readback') actor = 'different-actor';
            return JSON.stringify({
              id: 99,
              body: mutations[0]?.body,
              user: {
                login:
                  stage === 'readback-author'
                    ? 'different-actor'
                    : mutations[0]?.author,
              },
            });
          }
          throw new Error(
            `Unexpected legacy drift operation: ${args.join(' ')}`,
          );
        },
      });
      try {
        expect(() =>
          reply({
            repo: 'example/repo',
            pr: 1,
            head: HEAD,
            commentId: 1,
            engine: 'codex',
            round: 4,
            fingerprint: 'example',
            outcome: 'fixed',
            contentFile,
          }),
        ).toThrow(
          stage === 'readback-author'
            ? /could not verify review comment/
            : /authenticated GitHub actor changed/,
        );
        if (['preflight', 'inventory', 'threads'].includes(stage)) {
          expect(mutations).toEqual([]);
        } else {
          expect(mutations).toEqual([
            {
              body: `<!-- local-review-disposition:v1 engine=codex round=4 head=${HEAD} fingerprint=example outcome=fixed -->\nVerified correction.\n`,
              author: 'reviewer',
            },
          ]);
        }
        expect(JSON.stringify(record)).toBe(originalThread);
      } finally {
        rmSync(directory, { recursive: true, force: true });
      }
    },
  );

  it.each([true, false])('requires a matching owned finding: %s', (matches) => {
    const contentFile = join(
      mkdtempSync(join(tmpdir(), 'legacy-reply-')),
      'reply.md',
    );
    writeFileSync(
      contentFile,
      'Rechecked the correction at the current head.\n',
    );
    // Legacy reviews sometimes added a second root cause as a thread reply.
    const record = thread(['Original review discussion.', finding()]);
    let posted: { body: string } | undefined;
    setGitHubRunner({
      runGit: () => '',
      runGh: (args, payload) => {
        if (args[0] === 'api' && args[1] === 'user') {
          return JSON.stringify({ login: 'reviewer' });
        }
        if (args[0] === 'pr' && args[1] === 'view') return HEAD;
        if (
          args.includes('repos/example/repo/issues/1/comments?per_page=100')
        ) {
          return JSON.stringify([[]]);
        }
        if (
          args.includes('graphql') &&
          args.some((arg) => arg.includes('reviewThreads(first:100'))
        )
          return JSON.stringify([
            {
              data: {
                repository: {
                  pullRequest: {
                    reviewThreads: {
                      nodes: [record],
                      pageInfo: { hasNextPage: false },
                    },
                  },
                },
              },
            },
          ]);
        if (
          args.includes('POST') &&
          args.includes('repos/example/repo/pulls/1/comments/1/replies')
        ) {
          posted = payload as { body: string };
          return JSON.stringify({ id: 99 });
        }
        if (args.includes('repos/example/repo/pulls/comments/99')) {
          return JSON.stringify({
            id: 99,
            body: posted?.body,
            user: { login: 'reviewer' },
          });
        }
        throw new Error(`Unexpected legacy reply operation: ${args.join(' ')}`);
      },
    });
    const run = () =>
      reply({
        repo: 'example/repo',
        pr: 1,
        head: HEAD,
        commentId: 1,
        engine: 'codex',
        round: 4,
        fingerprint: matches ? 'example' : 'different',
        outcome: 'fixed',
        contentFile,
      });
    if (matches) {
      expect(run()).toEqual({ comment_id: 99, verified: true });
      expect(posted?.body).toBe(
        `<!-- local-review-disposition:v1 engine=codex round=4 head=${HEAD} fingerprint=example outcome=fixed -->\nRechecked the correction at the current head.\n`,
      );
    } else {
      expect(run).toThrow('actor-owned finding');
      expect(posted).toBeUndefined();
    }
  });
});

describe('settled legacy review threads', () => {
  it('accepts a standalone footer marker from older tools', () => {
    const [marker, prose] = finding(1, ' severity=P1 category=privacy').split(
      '\n',
    );
    expect(() =>
      threadProtocolRecords(thread([`${prose}\n\n${marker}`, disposition()])),
    ).not.toThrow();
  });

  it('rejects duplicate markers and marker-only comments', () => {
    expect(() =>
      threadProtocolRecords(
        thread([`${finding()}\n${finding()}`, disposition()]),
      ),
    ).toThrow();
    expect(() =>
      threadProtocolRecords(thread([finding().split('\n')[0]!, disposition()])),
    ).toThrow();
  });

  it('accepts the historical severity and category annotation', () => {
    expect(() =>
      threadProtocolRecords(
        thread([finding(1, ' severity=P1 category=privacy'), disposition()]),
      ),
    ).not.toThrow();
  });

  it('accepts an authenticated later-engine disposition of the same root cause', () => {
    expect(() =>
      threadProtocolRecords(
        thread([finding(), finding(2), disposition('claude', 3)]),
      ),
    ).not.toThrow();
  });

  it('accepts repeated confirmations after the original disposition', () => {
    expect(() =>
      threadProtocolRecords(
        thread([finding(), disposition(), disposition('claude', 3)]),
      ),
    ).not.toThrow();
  });

  it.each([
    ' severity=P9 category=privacy',
    ' severity=P1 category=privacy ignored=true',
    ' head=bogus',
  ])('rejects unsupported metadata: %s', (extra) => {
    expect(() =>
      threadProtocolRecords(thread([finding(1, extra), disposition()])),
    ).toThrow();
  });

  it('rejects an unresolved legacy thread', () => {
    expect(() =>
      threadProtocolRecords(thread([finding(), disposition()], false)),
    ).toThrow('unresolved');
  });

  it('rejects a recurrence after the last disposition', () => {
    expect(() =>
      threadProtocolRecords(thread([finding(), disposition(), finding(2)])),
    ).toThrow();
  });

  it('rejects a disposition for an unknown fingerprint', () => {
    expect(() =>
      threadProtocolRecords(
        thread([finding(), disposition('claude', 3, 'other')]),
      ),
    ).toThrow();
  });

  it('rejects a disposition before its finding', () => {
    expect(() =>
      threadProtocolRecords(thread([disposition(), finding()])),
    ).toThrow();
  });

  it('does not treat a prose reply or resolved flag as disposition evidence', () => {
    expect(() =>
      threadProtocolRecords(thread([finding(), 'Looks fixed.'])),
    ).toThrow();
  });

  it('does not accept another actor settling the finding', () => {
    const value = thread([finding(), disposition('claude', 3)]);
    value.comments.nodes[1]!.author = { login: 'someone-else' };
    expect(() => threadProtocolRecords(value)).toThrow();
  });
});
