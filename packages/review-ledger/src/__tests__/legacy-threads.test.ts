import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, writeFileSync } from 'node:fs';
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
  setGitHubRunner({
    runGh: () => JSON.stringify({ login: 'reviewer' }),
    runGit: () => '',
  });
});
afterEach(() => resetGitHubRunner());

describe('structured legacy disposition repair', () => {
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
      currentActor: () => 'reviewer',
      runGit: () => '',
      runGh: (args, payload) => {
        if (args.includes('view')) return HEAD;
        if (args.includes('graphql'))
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
        if (args.includes('POST')) {
          posted = payload as { body: string };
          return JSON.stringify({ id: 99 });
        }
        return JSON.stringify({
          id: 99,
          body: posted?.body,
          user: { login: 'reviewer' },
        });
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
