import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fail, LedgerError } from './errors.js';
import { assertRegularFile, parseJsonOrFail } from './io.js';
import { matchProtocol, matchPseudoV3 } from './protocol.js';
import {
  SUBPROCESS_MAX_BUFFER,
  EXPECTED_ACTOR_ENV,
  EXPECTED_THREADS_SHA256_ENV,
  HISTORICAL_COMMENT_IDS_ENV,
  DISPOSITION_V1,
  FINDING_V1,
  FINDING_V3_OPENER,
  FINDING_V3_RE,
  PSEUDO_V3_RE,
  SHA_64_RE,
  SHA_RE,
} from './constants.js';
import { requireToken, sha256Bytes } from './hash.js';
import type { ReviewRun } from './runs.js';
import type {
  GitHubReviewCommentNode,
  GitHubReviewThreadNode,
  GitHubRunner,
} from './types.js';

let defaultActor: string | null = null;

/** Describe a failed subprocess, handling buffer overflow errors. */
export function execFailureDetail(error: unknown): string {
  const execError = error as { stderr?: string; code?: string };
  if (execError.code === 'ENOBUFS') {
    return `output exceeded the ${SUBPROCESS_MAX_BUFFER}-byte subprocess buffer`;
  }
  return execError.stderr?.trim() || 'no diagnostic returned';
}

/** Default runner: shells out to `gh` and `git`. */
export class DefaultGitHubRunner implements GitHubRunner {
  private actorOverride: string | null = null;
  private cachedActor: string | null = null;

  constructor(customActor?: string) {
    this.setActor(customActor ?? null);
  }

  setActor(actor: string | null): void {
    this.actorOverride = actor ? requireToken(actor, 'actor') : null;
    this.cachedActor = null;
  }

  runGh(args: string[], payload?: unknown): string {
    const command = 'gh';
    const finalArgs = [...args];
    let input: string | undefined;
    if (payload !== undefined) {
      finalArgs.push('--input', '-');
      input = JSON.stringify(payload);
    }
    try {
      const stdout = execFileSync(command, finalArgs, {
        input,
        encoding: 'utf8',
        stdio: ['pipe', 'pipe', 'pipe'],
        maxBuffer: SUBPROCESS_MAX_BUFFER,
      });
      return stdout;
    } catch (error: unknown) {
      fail(`GitHub operation failed: ${execFailureDetail(error)}`);
    }
  }

  currentActor(): string {
    this.cachedActor ??= this.liveActor();
    return this.cachedActor;
  }

  /** Resolve the actor bypassing the cache. */
  liveActor(): string {
    if (this.actorOverride !== null) {
      return this.actorOverride;
    }
    return resolveLoginOrFail(this.runGh(['api', 'user']));
  }

  gitCompare(repo: string, before: string, after: string): unknown {
    const raw = this.runGh([
      'api',
      `repos/${repo}/compare/${before}...${after}`,
    ]);
    return parseJsonOrFail(raw, 'GitHub returned invalid JSON');
  }

  gitRevList(before: string, head: string): string[] {
    try {
      const stdout = execFileSync(
        'git',
        ['rev-list', '--reverse', '--ancestry-path', `${before}..${head}`],
        {
          encoding: 'utf8',
          stdio: ['pipe', 'pipe', 'pipe'],
          maxBuffer: SUBPROCESS_MAX_BUFFER,
        },
      );
      return stdout.trim().split(/\r?\n/).filter(Boolean);
    } catch {
      fail('could not derive the forward review transition');
    }
  }

  runGit(args: string[]): string {
    try {
      return execFileSync('git', args, {
        encoding: 'utf8',
        stdio: ['pipe', 'pipe', 'pipe'],
        maxBuffer: SUBPROCESS_MAX_BUFFER,
      });
    } catch (error: unknown) {
      fail(`Git operation failed: ${execFailureDetail(error)}`);
    }
  }

  isAncestor(ancestor: string, descendant: string): boolean {
    try {
      execFileSync(
        'git',
        ['merge-base', '--is-ancestor', ancestor, descendant],
        {
          encoding: 'utf8',
          stdio: ['pipe', 'pipe', 'pipe'],
          maxBuffer: SUBPROCESS_MAX_BUFFER,
        },
      );
      return true;
    } catch (error: unknown) {
      const execError = error as { status?: number };
      if (execError.status === 1) {
        return false;
      }
      fail(`Git ancestry check failed: ${execFailureDetail(error)}`);
    }
  }
}

let activeRunner: GitHubRunner = new DefaultGitHubRunner();

/** Return the runner all GitHub and git access flows through. */
export function getGitHubRunner(): GitHubRunner {
  return activeRunner;
}

/** Replace the active runner. Test seam. */
export function setGitHubRunner(runner: GitHubRunner): void {
  activeRunner = runner;
}

/** Restore the default runner and clear any cached actor. */
export function resetGitHubRunner(actor?: string): void {
  defaultActor = actor ?? null;
  activeRunner = new DefaultGitHubRunner(actor);
}

/** Invoke `gh` through the active runner, optionally with a JSON body. */
export function runGh(args: string[], payload?: unknown): string {
  return activeRunner.runGh(args, payload);
}

/** Invoke `gh` and parse its stdout as JSON. */
export function jsonOutput<T = unknown>(args: string[], payload?: unknown): T {
  const raw = runGh(args, payload);
  return parseJsonOrFail<T>(raw, 'GitHub returned invalid JSON');
}

/** Report whether `after` is strictly ahead of `before` with `before` as merge base. */
export function compareIsForward(
  repo: string,
  before: string,
  after: string,
): boolean {
  const comparison = activeRunner.gitCompare
    ? (activeRunner.gitCompare(repo, before, after) as Record<string, unknown>)
    : jsonOutput<Record<string, unknown>>([
        'api',
        `repos/${repo}/compare/${before}...${after}`,
      ]);
  const mergeBase = comparison?.['merge_base_commit'] as
    | { sha?: string }
    | undefined;
  return (
    typeof comparison === 'object' &&
    comparison !== null &&
    comparison['status'] === 'ahead' &&
    typeof mergeBase === 'object' &&
    mergeBase !== null &&
    mergeBase.sha === before
  );
}

/** Parse a `gh api user` response and require a non-empty string `login`. */
function resolveLoginOrFail(raw: string): string {
  const response = parseJsonOrFail<Record<string, unknown>>(
    raw,
    'GitHub returned an invalid authenticated-user response',
  );
  const login = response?.['login'];
  if (
    typeof response !== 'object' ||
    response === null ||
    typeof login !== 'string'
  ) {
    fail('GitHub returned an invalid authenticated-user response');
  }
  if (!login) {
    fail('GitHub returned an empty authenticated user');
  }
  return login;
}

/** Resolve the authenticated GitHub login, honouring the actor pin. */
export function currentActor(): string {
  let login: string;
  if (activeRunner.currentActor) {
    login = activeRunner.currentActor();
  } else {
    if (defaultActor === null) {
      defaultActor = resolveLoginOrFail(runGh(['api', 'user']));
    }
    login = defaultActor;
  }
  return assertActorPins(login);
}

function assertActorPins(login: string, expected?: string): string {
  if (!login) {
    fail('GitHub returned an empty authenticated user');
  }
  const rawEnvironmentActor = process.env[EXPECTED_ACTOR_ENV];
  if (rawEnvironmentActor !== undefined) {
    const environmentActor = requireToken(rawEnvironmentActor, 'actor');
    if (login !== environmentActor) {
      fail(
        `authenticated GitHub actor changed: expected ${environmentActor}, found ${login}`,
      );
    }
  }
  if (expected !== undefined && requireToken(expected, 'actor') !== login) {
    fail(
      `authenticated GitHub actor changed: expected ${expected}, found ${login}`,
    );
  }
  return login;
}

/** Resolve the live authenticated actor and assert it matches any expected pin. */
export function assertActor(expected?: string | undefined): string {
  return assertActorPins(currentActor(), expected);
}

/** Re-resolve the authenticated actor without consulting the runner cache. */
export function assertLiveActor(expected?: string | undefined): string {
  if (!activeRunner.liveActor && activeRunner.currentActor) {
    fail('GitHub runner cannot re-resolve the live authenticated actor');
  }
  const actor = activeRunner.liveActor
    ? activeRunner.liveActor()
    : resolveLoginOrFail(runGh(['api', 'user']));
  return assertActorPins(actor, expected);
}

/** Seed the actor cache. Test seam only. */
export function setCurrentActor(actor: string | null): void {
  defaultActor = actor ? requireToken(actor, 'actor') : null;
  if (activeRunner instanceof DefaultGitHubRunner) {
    activeRunner.setActor(defaultActor);
  }
}

/** Filter rows to only those authored by the authenticated actor. */
export function authenticatedRows<T extends Record<string, unknown>>(
  rows: T[],
  options?: { graphql?: boolean; actor?: string },
): T[] {
  const actor =
    options?.actor === undefined
      ? currentActor()
      : assertLiveActor(options.actor);
  return rows.filter((row) => {
    const user = row['user'] as { login?: string } | undefined;
    const author = row['author'] as { login?: string } | undefined;
    const identity = options?.graphql ? (author ?? user) : (user ?? author);
    return (
      typeof identity === 'object' &&
      identity !== null &&
      identity.login === actor
    );
  });
}

/** Assert the PR's live head is the commit the caller is acting on. */
export function verifyHead(
  repo: string,
  pr: number,
  expectedHead: string,
): void {
  const actual = runGh([
    'pr',
    'view',
    String(pr),
    '--repo',
    repo,
    '--json',
    'headRefOid',
    '--jq',
    '.headRefOid',
  ]).trim();
  if (actual !== expectedHead) {
    fail(
      `PR head mismatch: expected ${expectedHead}, found ${actual || '<empty>'}`,
    );
  }
}

/** Run a local git command through the active runner. */
export function runGit(args: string[]): string {
  const runner = getGitHubRunner();
  if (!runner.runGit) {
    fail('Git operations are unavailable in the active runner');
  }
  return runner.runGit(args);
}

/** Report whether `ancestor` is an ancestor of `descendant` in the local clone. */
export function isAncestor(ancestor: string, descendant: string): boolean {
  const runner = getGitHubRunner();
  if (!runner.isAncestor) {
    fail('Git operations are unavailable in the active runner');
  }
  return runner.isAncestor(ancestor, descendant);
}

/** Bind the pinned review base to the target branch lineage and local history. */
export function verifyReviewBase(
  repo: string,
  pr: number,
  base: string,
  before: string,
): void {
  const resolved = runGit(['rev-parse', '--verify', `${base}^{commit}`]).trim();
  if (resolved !== base) {
    fail('review base did not resolve to the supplied commit');
  }
  if (!isAncestor(base, before)) {
    fail('review base is not an ancestor of beforeSha');
  }
  const prBase = runGh([
    'pr',
    'view',
    String(pr),
    '--repo',
    repo,
    '--json',
    'baseRefOid',
    '--jq',
    '.baseRefOid',
  ]).trim();
  if (prBase === base) {
    return;
  }
  if (!SHA_RE.test(prBase)) {
    fail(`PR base is not a commit SHA: found ${prBase || '<empty>'}`);
  }
  let descendant: boolean;
  try {
    descendant = isAncestor(base, prBase);
  } catch (error) {
    if (!(error instanceof LedgerError)) throw error;
    fail(
      `could not verify PR base ${prBase} against the pinned base ${base}: ${error.message}; fetch the target branch before retrying`,
    );
  }
  if (!descendant) {
    fail(
      `PR base is outside the pinned review lineage: expected a descendant of ${base}, found ${prBase}; the target branch history diverged from the pinned base`,
    );
  }
}

/**
 * Bind the attested transition to the local checkout and the live PR head.
 */
export function verifyGitTransition(
  before: string,
  resultHead: string,
  liveHead: string,
): void {
  const localHead = runGit(['rev-parse', 'HEAD']).trim();
  if (localHead !== liveHead) {
    fail(
      `local HEAD mismatch: expected ${liveHead}, found ${localHead || '<empty>'}`,
    );
  }
  if (!isAncestor(before, resultHead)) {
    fail('review result rewrites or does not descend from beforeSha');
  }
  if (!isAncestor(resultHead, liveHead)) {
    fail('review result head is not an ancestor of the live head');
  }
}

function flattenPages<T>(value: unknown, label: string): T[] {
  if (!Array.isArray(value)) {
    fail(`GitHub ${label} response has an unexpected shape`);
  }
  const rows: T[] = [];
  for (const page of value) {
    if (!Array.isArray(page)) {
      fail(`GitHub ${label} page has an unexpected shape`);
    }
    for (const item of page) {
      if (typeof item !== 'object' || item === null) {
        fail(`GitHub ${label} item has an unexpected shape`);
      }
      rows.push(item as T);
    }
  }
  return rows;
}

/**
 * Fetch every changed file on the PR with its unified diff patch.
 */
export function getPrFiles(
  repo: string,
  pr: number,
): Record<string, string | null> {
  const pages = jsonOutput<unknown[]>([
    'api',
    '--paginate',
    '--slurp',
    `repos/${repo}/pulls/${pr}/files?per_page=100`,
  ]);
  const rows = flattenPages<{ filename?: unknown; patch?: unknown }>(
    pages,
    'PR-files',
  );
  const files: Record<string, string | null> = Object.create(null) as Record<
    string,
    string | null
  >;
  for (const item of rows) {
    if (typeof item.filename !== 'string') {
      fail('GitHub PR-files item has an unexpected shape');
    }
    files[item.filename] = typeof item.patch === 'string' ? item.patch : null;
  }
  return files;
}

/**
 * Fetch every review (inline) comment on the PR.
 */
export function getReviewComments(
  repo: string,
  pr: number,
): GitHubReviewCommentNode[] {
  const pages = jsonOutput<unknown[]>([
    'api',
    '--paginate',
    '--slurp',
    `repos/${repo}/pulls/${pr}/comments?per_page=100`,
  ]);
  const rows = flattenPages<GitHubReviewCommentNode>(pages, 'review-comments');
  return authenticatedRows(rows);
}

/**
 * Fetch every issue (conversation) comment on the PR.
 */
export function getIssueComments(
  repo: string,
  pr: number,
  expectedActor?: string,
): Array<Record<string, unknown>> {
  if (expectedActor === undefined) {
    return authenticatedRows(getAllIssueComments(repo, pr));
  }
  const actor = assertLiveActor(expectedActor);
  const rows = getAllIssueComments(repo, pr);
  return authenticatedRows(rows, { actor });
}

function getAllIssueComments(
  repo: string,
  pr: number,
): Array<Record<string, unknown>> {
  const pages = jsonOutput<unknown[]>([
    'api',
    '--paginate',
    '--slurp',
    `repos/${repo}/issues/${pr}/comments?per_page=100`,
  ]);
  return flattenPages<Record<string, unknown>>(pages, 'PR-comments');
}

/**
 * Assert a review comment still has the expected author and body.
 */
export function verifyComment(
  repo: string,
  commentId: number,
  expectedBody: string,
): void {
  verifyOwnedComment(
    `repos/${repo}/pulls/comments/${commentId}`,
    commentId,
    expectedBody,
    'review comment',
  );
}

/**
 * Assert an issue comment still has the expected author and body.
 */
export function verifyIssueComment(
  repo: string,
  commentId: number,
  expectedBody: string,
  expectedActor?: string,
): void {
  verifyOwnedComment(
    `repos/${repo}/issues/comments/${commentId}`,
    commentId,
    expectedBody,
    'PR comment',
    expectedActor,
  );
}

function verifyOwnedComment(
  endpoint: string,
  commentId: number,
  expectedBody: string,
  label: string,
  expectedActor?: string,
): void {
  const actor =
    expectedActor === undefined
      ? assertActor()
      : assertLiveActor(expectedActor);
  const response = jsonOutput<Record<string, unknown>>(['api', endpoint]);
  if (expectedActor !== undefined) {
    assertLiveActor(actor);
  }
  const user = (response['user'] ?? response['author']) as
    | { login?: string }
    | undefined;
  if (
    typeof response !== 'object' ||
    response === null ||
    response['body'] !== expectedBody ||
    typeof user !== 'object' ||
    user === null ||
    user.login !== actor
  ) {
    fail(`could not verify ${label} ${commentId} after posting`);
  }
}

/** Find a prior attestation for this engine and round in the current run. */
export function findMatchingAttestation(
  rows: Array<Record<string, unknown>>,
  runs: ReviewRun[],
  engine: string,
  round: number,
  body: string,
): { id: number; body: string } | null {
  const prefixes = [
    `<!-- local-review-pass:v3 engine=${engine} round=${round} `,
    `<!-- local-review-complete:v3 engine=${engine} round=${round} `,
  ];
  const start = runs.at(-1)?.commentId;
  const matches = rows.filter(
    (row) =>
      (start === undefined ||
        (typeof row['id'] === 'number' && row['id'] > start)) &&
      prefixes.some((prefix) => String(row['body'] ?? '').startsWith(prefix)),
  );
  if (matches.length === 0) {
    return null;
  }
  const marker = body.split('\n')[0];
  let earliest: { id: number; body: string } | null = null;
  for (const row of matches) {
    const existingBody = String(row['body']);
    if (
      existingBody.split('\n')[0] !== marker ||
      typeof row['id'] !== 'number'
    ) {
      fail(
        'local-review attestation identity conflicts with existing evidence in this run; recover the original sealed result instead of rewriting it',
      );
    }
    if (earliest === null || row['id'] < earliest.id) {
      earliest = { id: row['id'], body: existingBody };
    }
  }
  return earliest;
}

/**
 * Report whether an issue comment is still present on the pull request.
 */
export function issueCommentExists(
  repo: string,
  pr: number,
  commentId: number,
): boolean {
  return getAllIssueComments(repo, pr).some((row) => row['id'] === commentId);
}

/**
 * Delete an issue comment and confirm it is gone.
 */
export function deleteIssueComment(
  repo: string,
  pr: number,
  commentId: number,
): void {
  try {
    runGh([
      'api',
      '-X',
      'DELETE',
      `repos/${repo}/issues/comments/${commentId}`,
    ]);
  } catch (error) {
    if (issueCommentExists(repo, pr, commentId)) {
      throw error;
    }
    return;
  }
  if (issueCommentExists(repo, pr, commentId)) {
    fail(`could not verify rollback of PR comment ${commentId}`);
  }
}

/**
 * Extract the comment id from a create-comment response.
 */
export function getPostedCommentId(response: unknown): number {
  if (
    typeof response !== 'object' ||
    response === null ||
    typeof (response as { id?: unknown }).id !== 'number'
  ) {
    fail('GitHub accepted the mutation but returned no comment ID');
  }
  return (response as { id: number }).id;
}

/**
 * Find an existing comment with an identical marker and body.
 */
export function findMatchingBody(
  rows: Array<Record<string, unknown>>,
  marker: string,
  body: string,
): number | null {
  const matches = rows.filter((row) =>
    String(row['body'] ?? '').includes(marker),
  );
  if (matches.length === 0) {
    return null;
  }
  if (matches.length !== 1) {
    fail('ledger idempotency key is duplicated');
  }
  const row = matches[0]!;
  if (row['body'] !== body || typeof row['id'] !== 'number') {
    fail('ledger idempotency key already exists with conflicting content');
  }
  return row['id'] as number;
}

/**
 * Post an inline review comment, recovering an identical prior post.
 */
export function postReviewComment(
  repo: string,
  pr: number,
  head: string,
  marker: string,
  body: string,
  options: {
    path?: string | undefined;
    line?: number | undefined;
    side?: string | undefined;
    fileLevel?: boolean | undefined;
    replyTo?: number | undefined;
  },
): { commentId: number; replayed: boolean } {
  const existing = findMatchingBody(
    getReviewComments(repo, pr) as unknown as Array<Record<string, unknown>>,
    marker,
    body,
  );
  if (existing !== null) {
    verifyComment(repo, existing, body);
    verifyHead(repo, pr, head);
    return { commentId: existing, replayed: true };
  }

  let endpoint: string;
  let payload: Record<string, unknown>;
  if (options.replyTo === undefined) {
    payload = { body, commit_id: head, path: options.path };
    if (options.fileLevel) {
      payload['subject_type'] = 'file';
    } else {
      payload['line'] = options.line;
      payload['side'] = options.side;
    }
    endpoint = `repos/${repo}/pulls/${pr}/comments`;
  } else {
    payload = { body };
    endpoint = `repos/${repo}/pulls/${pr}/comments/${options.replyTo}/replies`;
  }

  let replayed = false;
  let commentId: number;
  try {
    const response = jsonOutput(['api', '-X', 'POST', endpoint], payload);
    commentId = getPostedCommentId(response);
  } catch (error) {
    if (error instanceof LedgerError) {
      const recovered = findMatchingBody(
        getReviewComments(repo, pr) as unknown as Array<
          Record<string, unknown>
        >,
        marker,
        body,
      );
      if (recovered === null) {
        throw error;
      }
      commentId = recovered;
      replayed = true;
    } else {
      throw error;
    }
  }

  verifyComment(repo, commentId, body);
  verifyHead(repo, pr, head);
  return { commentId, replayed };
}

/**
 * Report whether a review thread is currently resolved.
 */
export function getThreadState(
  threadId: string,
  commentId?: number,
  scope?: { repo: string; pr: number },
): boolean {
  const query = `
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      repository { nameWithOwner }
      pullRequest { number }
      comments(first: 100) {
        nodes { databaseId }
        pageInfo { hasNextPage }
      }
    }
  }
}
`.trim();

  const response = jsonOutput<{ data?: { node?: unknown } }>(
    ['api', 'graphql'],
    { query, variables: { threadId } },
  );

  const thread = response?.data?.node as
    | {
        id?: unknown;
        isResolved?: unknown;
        repository?: { nameWithOwner?: unknown };
        pullRequest?: { number?: unknown };
        comments?: {
          nodes?: Array<{ databaseId?: unknown }>;
          pageInfo?: { hasNextPage?: unknown };
        };
      }
    | undefined;

  if (typeof thread !== 'object' || thread === null || thread.id !== threadId) {
    fail(`could not verify review thread ${threadId}`);
  }
  if (scope) {
    const repository = thread.repository;
    const pullRequest = thread.pullRequest;
    if (
      typeof repository !== 'object' ||
      repository === null ||
      repository.nameWithOwner !== scope.repo ||
      typeof pullRequest !== 'object' ||
      pullRequest === null ||
      pullRequest.number !== scope.pr
    ) {
      fail(
        `review thread ${threadId} does not belong to ${scope.repo}#${scope.pr}`,
      );
    }
  }
  if (typeof thread.isResolved !== 'boolean') {
    fail(`review thread ${threadId} has invalid resolution state`);
  }

  if (commentId !== undefined) {
    const comments = thread.comments;
    if (typeof comments !== 'object' || comments === null) {
      fail('review thread read-back omitted comments');
    }
    const pageInfo = comments.pageInfo;
    const nodes = comments.nodes;
    if (
      typeof pageInfo !== 'object' ||
      pageInfo === null ||
      pageInfo.hasNextPage !== false ||
      !Array.isArray(nodes)
    ) {
      fail('review thread comments are incomplete');
    }
    const ids = nodes.map((node) => node.databaseId);
    if (ids.length === 0 || ids[0] !== commentId) {
      fail('--comment-id is not the root comment of --thread-id');
    }
  }

  return thread.isResolved;
}

/**
 * Resolve or unresolve a review thread and confirm the new state.
 */
export function setThreadState(
  threadId: string,
  resolved: boolean,
  commentId?: number,
  scope?: { repo: string; pr: number },
): boolean {
  if (getThreadState(threadId, commentId, scope) === resolved) {
    return true;
  }
  const field = resolved ? 'resolveReviewThread' : 'unresolveReviewThread';
  const mutation = `
mutation($threadId: ID!) {
  ${field}(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
`.trim();

  try {
    const response = jsonOutput<
      Record<
        string,
        Record<string, { thread?: { id?: unknown; isResolved?: unknown } }>
      >
    >(['api', 'graphql'], { query: mutation, variables: { threadId } });
    const thread = response?.['data']?.[field]?.['thread'];
    if (typeof thread !== 'object' || thread === null) {
      fail('GitHub returned an invalid thread mutation response');
    }
    if (thread.id !== threadId || thread.isResolved !== resolved) {
      fail(`GitHub did not set review thread ${threadId} resolved=${resolved}`);
    }
  } catch (error: unknown) {
    let verifiedAfterError = false;
    try {
      if (getThreadState(threadId, commentId, scope) === resolved) {
        verifiedAfterError = true;
      }
    } catch {
      // ignore
    }
    if (verifiedAfterError) {
      return false;
    }
    if (error instanceof LedgerError) {
      throw error;
    }
    throw new LedgerError(
      'GitHub returned an invalid thread mutation response',
      { cause: error },
    );
  }

  let verified: boolean;
  try {
    verified = getThreadState(threadId, commentId, scope);
  } catch {
    verified = getThreadState(threadId, commentId, scope);
  }
  if (verified !== resolved) {
    fail(`could not verify review thread ${threadId} resolved=${resolved}`);
  }
  return false;
}

/** Validate a paginated review-thread response and flatten it to threads. */
export function parseReviewThreadPages(
  pages: unknown,
  options?: { requireFullComments?: boolean },
): GitHubReviewThreadNode[] {
  const requireFullComments = options?.requireFullComments ?? true;
  if (!Array.isArray(pages)) {
    fail('GitHub review-thread response has an unexpected shape');
  }
  if (pages.length === 0) {
    fail('GitHub review-thread response is empty');
  }

  const threads: GitHubReviewThreadNode[] = [];
  for (let pageIndex = 0; pageIndex < pages.length; pageIndex++) {
    const page = pages[pageIndex] as {
      errors?: unknown;
      data?: {
        repository?: {
          pullRequest?: {
            reviewThreads?: {
              nodes?: unknown;
              pageInfo?: { hasNextPage?: unknown; endCursor?: unknown };
            };
          };
        };
      };
    };
    if (typeof page !== 'object' || page === null || page.errors) {
      fail('GitHub review-thread response is incomplete');
    }
    const connection = page.data?.repository?.pullRequest?.reviewThreads;
    if (
      !connection ||
      !Array.isArray(connection.nodes) ||
      typeof connection.pageInfo !== 'object' ||
      connection.pageInfo === null
    ) {
      fail('GitHub review-thread nodes have an unexpected shape');
    }
    const expectedMore = pageIndex < pages.length - 1;
    if (connection.pageInfo.hasNextPage !== expectedMore) {
      fail('GitHub review-thread pagination is incomplete');
    }
    if (expectedMore && typeof connection.pageInfo.endCursor !== 'string') {
      fail('GitHub review-thread pagination omitted its cursor');
    }
    for (const thread of connection.nodes as Array<{
      comments?: { nodes?: unknown; pageInfo?: { hasNextPage?: unknown } };
    }>) {
      if (typeof thread !== 'object' || thread === null) {
        fail('GitHub review thread has an unexpected shape');
      }
      const comments = thread.comments;
      if (
        !comments ||
        !Array.isArray(comments.nodes) ||
        typeof comments.pageInfo !== 'object' ||
        comments.pageInfo === null ||
        typeof comments.pageInfo.hasNextPage !== 'boolean' ||
        (requireFullComments && comments.pageInfo.hasNextPage !== false)
      ) {
        fail('GitHub review-thread comments are incomplete');
      }
      threads.push(thread as unknown as GitHubReviewThreadNode);
    }
  }

  return threads;
}

/** Load a review-thread snapshot from disk, requiring a sealed SHA-256 digest. */
export function loadReviewThreads(
  pathValue: string,
  expectedDigest?: string | undefined,
): GitHubReviewThreadNode[] {
  assertRegularFile(
    pathValue,
    'review-thread snapshot must be a regular non-symlink file',
  );

  const digest = expectedDigest ?? process.env[EXPECTED_THREADS_SHA256_ENV];
  if (digest === undefined || !SHA_64_RE.test(digest)) {
    fail('review-thread snapshot requires a sealed SHA-256 digest');
  }

  const raw = readFileSync(pathValue);
  if (sha256Bytes(raw) !== digest) {
    fail('review-thread snapshot changed after it was sealed');
  }

  const pages = parseJsonOrFail(
    raw.toString('utf8'),
    'review-thread snapshot must contain valid UTF-8 JSON',
  );

  return parseReviewThreadPages(pages);
}

/** Assert every thread belongs to the requested repository and pull request. */
export function assertThreadScope(
  threads: GitHubReviewThreadNode[],
  repo: string,
  pr: number,
): void {
  for (const thread of threads) {
    const repository = thread.repository;
    const pullRequest = thread.pullRequest;
    if (
      typeof repository !== 'object' ||
      repository === null ||
      repository.nameWithOwner !== repo ||
      typeof pullRequest !== 'object' ||
      pullRequest === null ||
      pullRequest.number !== pr
    ) {
      fail('GitHub returned a review thread outside the requested PR');
    }
  }
}

/** Resolve review threads live or from snapshot, asserting PR scope. */
export function reviewThreads(
  repo: string,
  pr: number,
  threadsFile?: string | undefined,
  expectedDigest?: string | undefined,
): GitHubReviewThreadNode[] {
  const threads =
    threadsFile === undefined
      ? fetchReviewThreads(repo, pr)
      : loadReviewThreads(threadsFile, expectedDigest);
  assertThreadScope(threads, repo, pr);
  return threads;
}

/** Fetch every review thread on a PR, with its PR scope attached. */
export function fetchReviewThreads(
  repo: string,
  pr: number,
): GitHubReviewThreadNode[] {
  const parts = repo.split('/');
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    fail('--repo must be OWNER/REPO');
  }
  const [owner, name] = parts;
  const query = `
query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$endCursor) {
        nodes {
          id
          isResolved
          repository { nameWithOwner }
          pullRequest { number }
          comments(first:100) {
            nodes { databaseId body author { login } }
            pageInfo { hasNextPage }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
`.trim();

  const pages = jsonOutput<unknown[]>([
    'api',
    'graphql',
    '--paginate',
    '--slurp',
    '-f',
    `query=${query}`,
    '-f',
    `owner=${owner}`,
    '-f',
    `name=${name}`,
    '-F',
    `number=${pr}`,
  ]);

  return parseReviewThreadPages(pages);
}

/** Find the review thread whose first comment is `rootCommentId`. */
export function findRootThread(
  repo: string,
  pr: number,
  rootCommentId: number,
): GitHubReviewThreadNode | null {
  const parts = repo.split('/');
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    fail('--repo must be OWNER/REPO');
  }
  const [owner, name] = parts;
  const query = `
query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$endCursor) {
        nodes {
          id
          isResolved
          repository { nameWithOwner }
          pullRequest { number }
          comments(first:1) {
            nodes { databaseId body author { login } }
            pageInfo { hasNextPage }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
`.trim();

  const pages = jsonOutput<unknown[]>([
    'api',
    'graphql',
    '--paginate',
    '--slurp',
    '-f',
    `query=${query}`,
    '-f',
    `owner=${owner}`,
    '-f',
    `name=${name}`,
    '-F',
    `number=${pr}`,
  ]);

  const threads = parseReviewThreadPages(pages, { requireFullComments: false });
  assertThreadScope(threads, repo, pr);

  const matching = threads.filter(
    (thread) => thread.comments.nodes[0]?.databaseId === rootCommentId,
  );
  if (matching.length > 1) {
    fail('could not identify exactly one root review thread');
  }
  return matching[0] ?? null;
}

/**
 * Load the set of comment ids a pass may treat as pre-existing history.
 */
export function loadHistoricalCommentIds(pathValue?: string): Set<number> {
  const finalPath = pathValue || process.env[HISTORICAL_COMMENT_IDS_ENV];
  if (!finalPath) {
    return new Set();
  }
  assertRegularFile(
    finalPath,
    'historical comment IDs must be a regular non-symlink file',
  );

  const values = parseJsonOrFail(
    readFileSync(finalPath, 'utf8'),
    'historical comment IDs must contain valid UTF-8 JSON',
  );

  if (
    !Array.isArray(values) ||
    values.some(
      (v) => typeof v !== 'number' || !Number.isInteger(v) || v < 1,
    ) ||
    new Set(values).size !== values.length
  ) {
    fail('historical comment IDs must be unique positive integers');
  }

  return new Set(values as number[]);
}

/**
 * Assert every historical v3 record is settled, owned, and in-bounds.
 */
export function verifyPseudoV3History(
  threads: GitHubReviewThreadNode[],
  historicalCommentIds?: Set<number>,
): void {
  const actor = currentActor();
  for (const thread of threads) {
    const comments = thread.comments;
    if (
      !comments ||
      !Array.isArray(comments.nodes) ||
      !comments.pageInfo ||
      comments.pageInfo.hasNextPage !== false
    ) {
      fail('GitHub review thread comments are incomplete');
    }
    const nodes = comments.nodes;
    for (let index = 0; index < nodes.length; index++) {
      const row = nodes[index]!;
      const body = String(row.body ?? '');
      const author = row.author;
      if (!author || typeof author.login !== 'string') {
        if (body.includes('<!-- local-review')) {
          fail('could not establish local-review comment ownership');
        }
        continue;
      }
      if (author.login !== actor) {
        continue;
      }
      if (!body.includes('<!-- local-review:v3')) {
        continue;
      }
      const pseudo = matchPseudoV3(body);
      if (pseudo === null) {
        matchProtocol(body, FINDING_V3_RE, '<!-- local-review:v3');
        continue;
      }
      if (
        historicalCommentIds !== undefined &&
        (typeof row.databaseId !== 'number' ||
          !historicalCommentIds.has(row.databaseId))
      ) {
        fail(
          'historical local-review:v3 finding was not captured before the current pass',
        );
      }
      const laterSameActor = nodes.slice(index + 1).some((reply) => {
        const replyAuthor = reply.author;
        const replyBody = String(reply.body ?? '').trim();
        return (
          replyAuthor &&
          replyAuthor.login === actor &&
          Boolean(replyBody) &&
          !replyBody.includes('<!-- local-review')
        );
      });
      if (
        index !== 0 ||
        typeof thread.id !== 'string' ||
        thread.isResolved !== true ||
        !laterSameActor
      ) {
        fail(
          'historical local-review:v3 finding is not settled actor-owned history',
        );
      }
    }
  }
}

/**
 * Load and forward-verify the head sequence a transition may span.
 */
export function loadAllowedHeads(
  path: string,
  before: string,
  afterSha: string,
  repo: string,
): Record<string, number> {
  assertRegularFile(
    path,
    'allowed transition heads must be a regular non-symlink file',
  );

  const values = parseJsonOrFail(
    readFileSync(path, 'utf8'),
    'allowed transition heads must contain valid UTF-8 JSON',
  );

  if (
    !Array.isArray(values) ||
    values.length === 0 ||
    values.some((v) => typeof v !== 'string' || !SHA_RE.test(v)) ||
    new Set(values).size !== values.length ||
    values[0] !== before ||
    values[values.length - 1] !== afterSha
  ) {
    fail(
      'allowed transition heads do not match the observed review transition',
    );
  }

  const headList = values as string[];
  for (let i = 0; i < headList.length - 1; i++) {
    const cur = headList[i]!;
    const nxt = headList[i + 1]!;
    if (!compareIsForward(repo, cur, nxt)) {
      fail('allowed transition heads are not forward-only');
    }
  }

  const allowed: Record<string, number> = {};
  headList.forEach((val, idx) => {
    allowed[val] = idx;
  });
  return allowed;
}

/**
 * Report whether any row carries a historical pseudo-v3 marker.
 */
export function rowsHaveHistoricalMarkers(
  rows: Array<{ body?: unknown }>,
): boolean {
  return rows.some((row) => {
    const body = String(row.body ?? '');
    if (body.includes(FINDING_V1) || body.includes(DISPOSITION_V1)) {
      return true;
    }
    if (body.includes(FINDING_V3_OPENER) && !FINDING_V3_RE.test(body)) {
      return true;
    }
    return PSEUDO_V3_RE.test(body);
  });
}
