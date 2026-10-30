import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  currentActor,
  getIssueComments,
  readLifecycle,
  resetGitHubRunner,
  setGitHubRunner,
} from '../github.js';
import { verifyCoverage } from '../roster.js';
import { sha256Text } from '../hash.js';
import type { GitHubRunner } from '../types.js';

const HEAD = 'a'.repeat(40);
const BASE = 'b'.repeat(40);
const content = 'Synthetic authorization.';
const digest = sha256Text(
  JSON.stringify({
    base: BASE,
    content,
    max_rounds: 2,
    start_head: HEAD,
    supersedes: null,
    tier: 'lean',
  }),
);
const body = `<!-- local-review-run:v1 id=${digest} tier=lean max-rounds=2 base=${BASE} start-head=${HEAD} supersedes=none content-sha256=${digest} -->\n${content}`;

class LifecycleRunner implements GitHubRunner {
  actor = 'new-owner';
  edited: string | null = null;
  total = 1;
  more = false;
  missingEdit = false;
  driftAfter: 'list' | 'graphql' | null = null;
  calls: string[] = [];

  runGh(args: string[]): string {
    if (args[0] === 'pr') return HEAD;
    if (args[0] === 'api' && args[1] === 'user') {
      this.calls.push('user');
      return JSON.stringify({ login: this.actor });
    }
    if (args.includes('graphql')) {
      this.calls.push('graphql');
      if (this.driftAfter === 'graphql') this.actor = 'different-owner';
      const node: Record<string, unknown> = {
        databaseId: 10,
        body,
        createdAt: '2026-01-01T00:00:00Z',
        author: { login: 'old-owner' },
      };
      if (!this.missingEdit) node['lastEditedAt'] = this.edited;
      return JSON.stringify([
        {
          data: {
            repository: {
              issueOrPullRequest: {
                comments: {
                  totalCount: this.total,
                  nodes: [node],
                  pageInfo: { hasNextPage: this.more, endCursor: 'cursor' },
                },
              },
            },
          },
        },
      ]);
    }
    if (args.some((arg) => arg.includes('/comments?per_page=100'))) {
      this.calls.push('list');
      if (this.driftAfter === 'list') this.actor = 'different-owner';
      return JSON.stringify([[{ id: 10, body, user: { login: 'old-owner' } }]]);
    }
    throw new Error('unexpected test operation');
  }
}

let runner: LifecycleRunner;
beforeEach(() => {
  runner = new LifecycleRunner();
  setGitHubRunner(runner);
});
afterEach(() => resetGitHubRunner());

describe('edit-aware complete lifecycle inventory', () => {
  it.each([undefined, 'old-owner'])(
    'rejects lifecycle identity drift with expected actor %s',
    (expected) => {
      runner.actor = 'old-owner';
      expect(currentActor()).toBe('old-owner');
      runner.driftAfter = 'list';
      expect(() => getIssueComments('example/project', 7, expected)).toThrow(
        /authenticated GitHub actor changed/,
      );
      expect(runner.calls).not.toContain('graphql');

      runner.actor = 'old-owner';
      runner.driftAfter = 'graphql';
      expect(() => getIssueComments('example/project', 7, expected)).toThrow(
        /authenticated GitHub actor changed/,
      );
      expect(runner.calls).toContain('graphql');
    },
  );
  it('returns foreign-active and blocks shipping evidence', () => {
    expect(readLifecycle('example/project', 7).state).toBe('foreign-active');
    expect(() =>
      verifyCoverage({ repo: 'example/project', pr: 7, head: HEAD }),
    ).toThrow(/foreign-active/);
  });
  it('rejects edited records even when REST timestamps could have rounded them', () => {
    runner.edited = '2026-01-01T00:00:00Z';
    expect(() => readLifecycle('example/project', 7)).toThrow(/edited/);
  });
  it('rejects missing edit metadata', () => {
    runner.missingEdit = true;
    expect(() => readLifecycle('example/project', 7)).toThrow();
  });
  it('rejects missing pages and inconsistent totals', () => {
    runner.more = true;
    expect(() => readLifecycle('example/project', 7)).toThrow(/pagination/);
    runner.more = false;
    runner.total = 2;
    expect(() => readLifecycle('example/project', 7)).toThrow(/pagination/);
  });
});
