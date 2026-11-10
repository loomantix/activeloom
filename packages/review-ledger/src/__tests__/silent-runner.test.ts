import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { dispose, postFinding, reconcile } from '../index.js';
import {
  fetchReviewThreads,
  getAllIssueComments,
  readLifecycle,
  resetGitHubRunner,
  setGitHubRunner,
} from '../github.js';
import {
  LOCAL_ID_BASE,
  LocalStore,
  SILENT_FORCE_ENV,
  SILENT_ROOT_ENV,
  silentPatternMatches,
} from '../local-store.js';
import { SilentRunner, type DelegateRunner } from '../silent-runner.js';
import { postSummary, renderSummary } from '../silent-summary.js';

const REPO = 'client/app';
const PR = 7;
const HEAD = '1111111111111111111111111111111111111111';

/** GitHub as silent mode may see it: reads answer, every write throws. */
class GitHub implements DelegateRunner {
  actor = 'operator';
  calls: string[] = [];
  issueComments: Array<Record<string, unknown>> = [];
  reviewComments: Array<Record<string, unknown>> = [];
  posted: Array<{ endpoint: string; body: unknown }> = [];
  allowPost = false;

  runGh(args: string[], payload?: unknown): string {
    const cmd = args.join(' ');
    this.calls.push(cmd);
    if (cmd === 'api user') return JSON.stringify({ login: this.actor });
    if (cmd.startsWith('pr view')) return HEAD;
    if (cmd.includes(`pulls/${PR}/files?per_page=100`))
      return JSON.stringify([
        [{ filename: 'src/app.ts', patch: '@@ -1,3 +1,4 @@\n a\n b\n+c\n d' }],
      ]);
    if (cmd.includes(`issues/${PR}/comments?per_page=100`))
      return JSON.stringify([this.issueComments]);
    if (cmd.includes(`pulls/${PR}/comments?per_page=100`))
      return JSON.stringify([this.reviewComments]);
    if (this.allowPost && cmd.startsWith('api -X POST')) {
      this.posted.push({ endpoint: args[3]!, body: payload });
      return JSON.stringify({ id: 99 });
    }
    throw new Error(`unexpected GitHub call: ${cmd}`);
  }
  currentActor(): string {
    return this.actor;
  }
  liveActor(): string {
    return this.actor;
  }
  runGit(): string {
    throw new Error('git is not used when the root is overridden');
  }
}

let root: string;
let github: GitHub;
let runner: SilentRunner;

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'silent-ledger-'));
  process.env[SILENT_ROOT_ENV] = root;
  process.env[SILENT_FORCE_ENV] = '1';
  process.env['ACTIVELOOM_REVIEW_PROFILE'] = join(root, 'no-profile.json');
  github = new GitHub();
  runner = new SilentRunner(github, { repo: REPO, pr: PR });
  setGitHubRunner(runner);
});

afterEach(() => {
  resetGitHubRunner();
  delete process.env[SILENT_ROOT_ENV];
  delete process.env[SILENT_FORCE_ENV];
  delete process.env['ACTIVELOOM_REVIEW_PROFILE'];
  rmSync(root, { recursive: true, force: true });
});

function finding(fingerprint: string, content: string) {
  return postFinding({
    repo: REPO,
    pr: PR,
    head: HEAD,
    path: 'src/app.ts',
    line: 3,
    side: 'RIGHT',
    engine: 'claude',
    round: 1,
    fingerprint,
    occurrence: 1,
    severity: 'major',
    lens: 'code-reviewer',
    content,
  });
}

describe('silent runner', () => {
  it('keeps the finding, disposition and resolve lifecycle off GitHub', () => {
    const posted = finding(
      'null-guard',
      '**The handler dereferences a null user.**\n\nEvidence: line 3.',
    );
    expect(posted.comment_id).toBeGreaterThan(LOCAL_ID_BASE);

    const before = reconcile({
      repo: REPO,
      pr: PR,
      head: HEAD,
      fingerprint: 'null-guard',
    });
    expect(before.nextAction).toBe('dispose');
    const result = dispose({
      repo: REPO,
      pr: PR,
      head: HEAD,
      engine: 'claude',
      round: 1,
      fingerprint: 'null-guard',
      occurrence: 1,
      outcome: 'fixed',
      commentId: posted.comment_id,
      threadId: before.threadId!,
      content: `Fixed in ${HEAD}. Added a guard before the dereference. Validation: tests pass.`,
    });
    expect(result.resolved).toBe(true);
    expect(fetchReviewThreads(REPO, PR)[0]!.isResolved).toBe(true);

    const writes = github.calls.filter((c) =>
      /-X (POST|PATCH|DELETE)|mutation/.test(c),
    );
    expect(writes).toEqual([]);
    const scratchpad = readFileSync(
      new LocalStore(root, REPO, PR).scratchpadPath,
      'utf8',
    );
    expect(scratchpad).toContain('The handler dereferences a null user.');
  });

  it('answers issue comments and the lifecycle query locally', () => {
    const body = 'plain note';
    const created = JSON.parse(
      runner.runGh(
        ['api', '-X', 'POST', `repos/${REPO}/issues/${PR}/comments`],
        { body },
      ),
    ) as { id: number };
    expect(getAllIssueComments(REPO, PR).map((r) => r['id'])).toEqual([
      created.id,
    ]);
    runner.runGh(
      ['api', '-X', 'PATCH', `repos/${REPO}/issues/comments/${created.id}`],
      { body: 'edited' },
    );
    expect(getAllIssueComments(REPO, PR)[0]!['body']).toBe('edited');
    expect(readLifecycle(REPO, PR).state).toBe('none');
    runner.runGh([
      'api',
      '-X',
      'DELETE',
      `repos/${REPO}/issues/comments/${created.id}`,
    ]);
    expect(getAllIssueComments(REPO, PR)).toEqual([]);
  });

  it('refuses writes it does not emulate', () => {
    expect(() =>
      runner.runGh(
        ['api', '-X', 'POST', `repos/${REPO}/pulls/${PR}/comments/123/replies`],
        { body: 'x' },
      ),
    ).toThrowError(/silent mode: refusing a reply to GitHub comment 123/);
    expect(() =>
      runner.runGh(['api', '-X', 'POST', `repos/${REPO}/labels`], {
        name: 'x',
      }),
    ).toThrowError(/silent mode: refusing POST/);
    expect(() =>
      runner.runGh(['pr', 'comment', String(PR), '--body', 'x']),
    ).toThrowError(/silent mode: refusing gh pr comment/);
    expect(() =>
      runner.runGh(['api', 'graphql'], {
        query: 'mutation { addComment(input:{}) { clientMutationId } }',
      }),
    ).toThrowError(/silent mode: refusing a GraphQL mutation/);
  });

  it('refuses to start silent on a PR that already has chain comments', () => {
    github.issueComments = [
      {
        id: 5,
        user: { login: 'operator' },
        body: '<!-- local-review-run:v1 id=x -->',
      },
    ];
    expect(() => runner.storeFor(REPO, PR)).toThrowError(
      /already carries 1 review-chain comment/,
    );
  });

  it('passes a repository that is not silent straight through', () => {
    delete process.env[SILENT_FORCE_ENV];
    const loud = new SilentRunner(github, { repo: 'loomantix/x', pr: PR });
    github.allowPost = true;
    loud.runGh(
      ['api', '-X', 'POST', `repos/loomantix/x/issues/${PR}/comments`],
      { body: 'b' },
    );
    expect(github.posted).toHaveLength(1);
  });

  it('reads silent.repos from the review profile', () => {
    delete process.env[SILENT_FORCE_ENV];
    const profile = join(root, 'profile.json');
    writeFileSync(profile, JSON.stringify({ silent: { repos: ['Client/*'] } }));
    process.env['ACTIVELOOM_REVIEW_PROFILE'] = profile;
    expect(new SilentRunner(github).storeFor(REPO, PR)).not.toBeNull();
    expect(new SilentRunner(github).storeFor('other/app', PR)).toBeNull();
  });
});

describe('silent summary', () => {
  function closeOne(): void {
    const posted = finding(
      'null-guard',
      '**The handler dereferences a null user.**\n\nEvidence.',
    );
    const { threadId } = reconcile({
      repo: REPO,
      pr: PR,
      head: HEAD,
      fingerprint: 'null-guard',
    });
    dispose({
      repo: REPO,
      pr: PR,
      head: HEAD,
      engine: 'claude',
      round: 1,
      fingerprint: 'null-guard',
      occurrence: 1,
      outcome: 'fixed',
      commentId: posted.comment_id,
      threadId: threadId!,
      content: `Fixed in ${HEAD}. Added a guard before the dereference. Validation: tests pass.`,
    });
    finding('naming', '**Rename for clarity.**');
  }

  it('renders a brief summary with no markers or engine names', () => {
    closeOne();
    const store = new LocalStore(root, REPO, PR);
    const summary = renderSummary(store, store.read());
    expect(summary.summary).toBe(
      [
        '**Review summary**',
        '',
        'Fixed:',
        '- **major** `src/app.ts`: The handler dereferences a null user. Added a guard before the dereference. (1111111)',
        '',
        'Other findings: 1 still open.',
        '',
        'Result: the review ended without a recorded outcome.',
        '',
      ].join('\n'),
    );
    expect(summary.summary).not.toMatch(/<!--|claude|round/i);
  });

  it('posts only the approved bytes, once', () => {
    closeOne();
    const store = new LocalStore(root, REPO, PR);
    const { sha256 } = renderSummary(store, store.read());
    github.allowPost = true;
    expect(() => postSummary(github, store, '0'.repeat(64))).toThrowError(
      /summary changed/,
    );
    expect(postSummary(github, store, sha256)).toEqual({
      comment_id: 99,
      replayed: false,
    });
    expect(postSummary(github, store, sha256)).toEqual({
      comment_id: 99,
      replayed: true,
    });
    expect(github.posted).toHaveLength(1);
    expect(github.posted[0]!.endpoint).toBe(
      `repos/${REPO}/issues/${PR}/comments`,
    );
  });
});

describe('silent pattern matching', () => {
  const vectors = JSON.parse(
    readFileSync(
      join(__dirname, '../../../../tests/fixtures/silent-globs.json'),
      'utf8',
    ),
  ) as Array<{ pattern: string; repo: string; match: boolean }>;
  it.each(vectors)('$pattern vs $repo', ({ pattern, repo, match }) => {
    expect(silentPatternMatches(pattern, repo)).toBe(match);
  });
});
