import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runCli } from '../cli.js';
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
import { buildFindingBody, buildDispositionBody } from '../protocol.js';

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
    expect(() => runner.pin(REPO, PR, 'run-arg')).toThrowError(
      /already carries 1 review-chain comment/,
    );
  });

  it('keeps a profile-matched PR loud when it was already reviewed loudly', () => {
    github.issueComments = [
      {
        id: 5,
        user: { login: 'operator' },
        body: '<!-- local-review-run:v1 id=x -->',
      },
    ];
    expect(runner.storeFor(REPO, PR)).toBeNull();
    expect(new LocalStore(root, REPO, PR).pin()).toBeNull();
  });

  it('treats gh api fields or input without -X as a write, like gh does', () => {
    for (const args of [
      ['api', `repos/${REPO}/pulls/${PR}/reviews`, '-f', 'event=COMMENT'],
      ['api', `repos/${REPO}/pulls/${PR}/reviews`, '--input', '-'],
      ['api', '-XPOST', `repos/${REPO}/labels`],
      ['api', '--method=PATCH', `repos/${REPO}/labels/x`],
      ['api', `repos/${REPO}/pulls/${PR}/reviews`, '--raw-field=body=x'],
      ['api', `repos/${REPO}/pulls/${PR}/reviews`, '--field=event=COMMENT'],
      ['api', `repos/${REPO}/pulls/${PR}/reviews`, '-fbody=x'],
      ['api', `repos/${REPO}/pulls/${PR}/reviews`, '-Fevent=COMMENT'],
      ['api', `repos/${REPO}/pulls/${PR}/reviews`, '--input=/tmp/body.json'],
      ['api', `repos/${REPO}/pulls/${PR}/reviews`, '-F', 'labels[]'],
    ]) {
      expect(() => runner.runGh(args)).toThrowError(/silent mode: refusing/);
    }
    expect(github.calls.some((call) => call.includes('/reviews'))).toBe(false);
  });

  it('infers POST from the payload that local-gh reads from stdin', () => {
    const body = { body: 'hello' };
    expect(() =>
      runner.runGh(['api', `repos/${REPO}/pulls/${PR}/reviews`], body),
    ).toThrowError(/silent mode: refusing POST/);
    runner.runGh(['api', `repos/${REPO}/issues/${PR}/comments`], body);
    expect(getAllIssueComments(REPO, PR).map((row) => row['body'])).toEqual([
      'hello',
    ]);
    expect(github.calls.some((call) => call.includes('/reviews'))).toBe(false);
  });

  it.each(['--raw-field=body=hello', '-fbody=hello', '-f=body=hello'])(
    'stores a comment body from %s',
    (field) => {
      runner.runGh(['api', `repos/${REPO}/issues/${PR}/comments`, field]);
      expect(getAllIssueComments(REPO, PR).map((row) => row['body'])).toEqual([
        'hello',
      ]);
    },
  );

  it('stores a comment body passed as a -f field', () => {
    runner.runGh([
      'api',
      `repos/${REPO}/issues/${PR}/comments`,
      '-f',
      'body=hello',
    ]);
    expect(getAllIssueComments(REPO, PR).map((row) => row['body'])).toEqual([
      'hello',
    ]);
  });

  it('maps differently cased repository names to one store', () => {
    expect(new LocalStore(root, 'Client/App', PR).dir).toBe(
      new LocalStore(root, REPO, PR).dir,
    );
  });

  it('clears an empty lock left behind by a crash, but not a fresh one', () => {
    const store = new LocalStore(root, REPO, PR);
    store.writePin('operator', 'run-arg');
    const lock = join(store.dir, '.lock');
    writeFileSync(lock, '');
    const old = new Date(Date.now() - 60_000);
    utimesSync(lock, old, old);
    store.update(() => undefined);
    expect(existsSync(lock)).toBe(false);
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
  function recurrence() {
    const posted = finding('recurring', '**Original defect.**');
    const store = new LocalStore(root, REPO, PR);
    const replies = `repos/${REPO}/pulls/${PR}/comments/${posted.comment_id}/replies`;
    const params = {
      engine: 'claude' as const,
      round: 1,
      head: HEAD,
      fingerprint: 'recurring',
      occurrence: 1,
    };
    runner.runGh(['api', '-X', 'POST', replies], {
      body: buildDispositionBody({
        ...params,
        outcome: 'fixed',
        content: `Fixed in ${HEAD}. Original fix.`,
      }).body,
    });
    runner.runGh(['api', '-X', 'POST', replies], {
      body: buildFindingBody({
        ...params,
        engine: 'codex',
        round: 2,
        occurrence: 2,
        severity: 'minor',
        lens: 'code-reviewer',
        content: '**Defect recurred with new evidence.**',
      }).body,
    });
    return { store, replies, params };
  }

  it('reports a reopened occurrence as open despite the earlier fix', () => {
    const { store } = recurrence();
    const summary = renderSummary(store, store.read());
    expect(summary.fixed).toBe(0);
    expect(summary.open).toBe(1);
    expect(summary.summary).not.toContain('Original fix');
  });

  it.each(['fixed', 'deferred', 'dismissed'] as const)(
    'uses only the latest occurrence when it is %s',
    (outcome) => {
      const { store, replies, params } = recurrence();
      runner.runGh(['api', '-X', 'POST', replies], {
        body: buildDispositionBody({
          ...params,
          engine: 'codex',
          round: 2,
          occurrence: 2,
          outcome,
          content: `Fixed in ${HEAD}. Current disposition.`,
        }).body,
      });
      const summary = renderSummary(store, store.read());
      expect(summary.open).toBe(0);
      expect(summary.fixed).toBe(outcome === 'fixed' ? 1 : 0);
      expect(summary.deferred).toBe(outcome === 'deferred' ? 1 : 0);
      expect(summary.dismissed).toBe(outcome === 'dismissed' ? 1 : 0);
      expect(summary.summary).not.toContain('Original defect');
      if (outcome === 'fixed') {
        expect(summary.summary).toContain(
          '**minor** `src/app.ts`: Defect recurred with new evidence',
        );
      }
    },
  );

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

  describe('post-summary CLI gates', () => {
    const scope = ['--repo', REPO, '--pr', String(PR)];

    beforeEach(() => {
      runner.pin(REPO, PR, 'run-arg');
      closeOne();
    });

    // A well-formed but stale digest: if a gate regressed, postSummary would
    // still refuse with "summary changed" before any GitHub call.
    it('refuses without --confirmed', () => {
      expect(() =>
        runCli([
          'post-summary',
          ...scope,
          '--expected-summary-sha256',
          '0'.repeat(64),
        ]),
      ).toThrowError(/requires --confirmed/);
    });

    it.each([
      ['missing', []],
      ['malformed', ['--expected-summary-sha256', 'abc']],
    ])('refuses a %s digest', (_label, digest) => {
      expect(() =>
        runCli(['post-summary', ...scope, '--confirmed', ...digest]),
      ).toThrowError(/requires --expected-summary-sha256/);
    });
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
