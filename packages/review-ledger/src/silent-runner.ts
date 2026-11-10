import { fail } from './errors.js';
import { parseJsonOrFail } from './io.js';
import {
  allocateId,
  isLocalId,
  isLocalThreadId,
  LOCAL_THREAD_PREFIX,
  LocalStore,
  nowIso,
  profileSaysSilent,
  reviewRoot,
  type LocalIssueComment,
  type LocalReviewComment,
  type LocalState,
  type ModePin,
} from './local-store.js';
import type { GitHubRunner } from './types.js';

/**
 * A GitHubRunner that keeps a silent PR's ledger local. Comment, reply, thread
 * and resolve traffic for a PR pinned silent is answered from the local store
 * with GitHub's response shapes, so every ledger decision path runs unchanged.
 * Everything else (actor, PR head, files, compare) goes to GitHub. A write it
 * does not recognise is refused while any silent scope is in play.
 */

/** The parts of a delegate runner this wrapper forwards. */
export interface DelegateRunner extends GitHubRunner {
  currentActor(): string;
  liveActor(): string;
  runGit(args: string[]): string;
}

interface ApiRequest {
  method: string;
  endpoint: string;
  slurp: boolean;
  fields: Record<string, string>;
}

const OWNER = '[A-Za-z0-9_.-]+';
const PR_COMMENTS_RE = new RegExp(
  `^repos/(${OWNER}/${OWNER})/pulls/([1-9][0-9]*)/comments$`,
);
const PR_REPLIES_RE = new RegExp(
  `^repos/(${OWNER}/${OWNER})/pulls/([1-9][0-9]*)/comments/([1-9][0-9]*)/replies$`,
);
const ISSUE_COMMENTS_RE = new RegExp(
  `^repos/(${OWNER}/${OWNER})/issues/([1-9][0-9]*)/comments$`,
);
const REVIEW_COMMENT_RE = new RegExp(
  `^repos/(${OWNER}/${OWNER})/pulls/comments/([1-9][0-9]*)$`,
);
const ISSUE_COMMENT_RE = new RegExp(
  `^repos/(${OWNER}/${OWNER})/issues/comments/([1-9][0-9]*)$`,
);
const REPO_RE = new RegExp(`^repos/(${OWNER}/${OWNER})(?:/|$)`);
const READ_ONLY_GH = new Set(['pr view', 'pr checks', 'pr diff', 'repo view']);

function parseApi(args: string[]): ApiRequest | null {
  if (args[0] !== 'api') return null;
  let method = 'GET';
  let endpoint: string | undefined;
  let slurp = false;
  const fields: Record<string, string> = {};
  for (let i = 1; i < args.length; i++) {
    const arg = args[i]!;
    if (arg === '-X' || arg === '--method') {
      method = (args[++i] ?? '').toUpperCase();
    } else if (
      arg === '-f' ||
      arg === '-F' ||
      arg === '--raw-field' ||
      arg === '--field'
    ) {
      const value = args[++i] ?? '';
      const at = value.indexOf('=');
      if (at > 0) fields[value.slice(0, at)] = value.slice(at + 1);
    } else if (arg === '--slurp') {
      slurp = true;
    } else if (arg === '--paginate' || arg === '--include' || arg === '-i') {
      // pagination is moot for local rows
    } else if (
      arg === '--jq' ||
      arg === '-q' ||
      arg === '--input' ||
      arg === '-H' ||
      arg === '--header'
    ) {
      i++;
    } else if (!arg.startsWith('-') && endpoint === undefined) {
      endpoint = arg;
    }
  }
  if (endpoint === undefined) return null;
  return { method, endpoint: endpoint.split('?')[0]!, slurp, fields };
}

function json(value: unknown): string {
  return JSON.stringify(value);
}

function notFound(): never {
  fail('GitHub operation failed: gh: Not Found (HTTP 404)');
}

export class SilentRunner implements GitHubRunner {
  private readonly stores = new Map<string, LocalStore | null>();
  private root: string | null = null;

  constructor(
    private readonly inner: DelegateRunner,
    private readonly scope?: { repo: string; pr: number } | undefined,
  ) {}

  // --- delegation ------------------------------------------------------------

  currentActor(): string {
    return this.inner.currentActor();
  }

  liveActor(): string {
    return this.inner.liveActor();
  }

  gitCompare(repo: string, before: string, after: string): unknown {
    return this.inner.gitCompare
      ? this.inner.gitCompare(repo, before, after)
      : parseJsonOrFail(
          this.inner.runGh([
            'api',
            `repos/${repo}/compare/${before}...${after}`,
          ]),
          'GitHub returned invalid JSON',
        );
  }

  gitRevList(before: string, head: string): string[] {
    if (!this.inner.gitRevList) fail('git rev-list is unavailable');
    return this.inner.gitRevList(before, head);
  }

  runGit(args: string[]): string {
    return this.inner.runGit(args);
  }

  isAncestor(ancestor: string, descendant: string): boolean {
    if (!this.inner.isAncestor) fail('git ancestry check is unavailable');
    return this.inner.isAncestor(ancestor, descendant);
  }

  // --- silent scope ----------------------------------------------------------

  private reviewRoot(): string {
    this.root ??= reviewRoot(() =>
      this.inner.runGit(['rev-parse', '--git-common-dir']),
    );
    return this.root;
  }

  /** The PR's store when it is silent, pinning it on first use if the profile says so. */
  storeFor(repo: string, pr: number): LocalStore | null {
    const key = `${repo.toLowerCase()}#${pr}`;
    if (this.stores.has(key)) return this.stores.get(key)!;
    let root: string;
    try {
      root = this.reviewRoot();
    } catch (error) {
      // Outside a git checkout no PR can be pinned; only a profile that
      // marks the repository silent makes that fatal.
      if (profileSaysSilent(repo) !== null) throw error;
      this.stores.set(key, null);
      return null;
    }
    const store = new LocalStore(root, repo, pr);
    let result: LocalStore | null = null;
    if (store.pin() !== null) {
      result = store;
    } else {
      const source = profileSaysSilent(repo);
      if (source !== null) {
        this.pinStore(store, source);
        result = store;
      }
    }
    this.stores.set(key, result);
    return result;
  }

  /** Pin a PR silent. Refuses a PR that already carries this actor's chain comments. */
  pin(repo: string, pr: number, source: string): ModePin {
    const store = new LocalStore(this.reviewRoot(), repo, pr);
    const existing = store.pin();
    if (existing !== null) return existing;
    const pin = this.pinStore(store, source);
    this.stores.set(`${repo.toLowerCase()}#${pr}`, store);
    return pin;
  }

  private pinStore(store: LocalStore, source: string): ModePin {
    const actor = this.inner.liveActor();
    const prior = [
      `repos/${store.repo}/issues/${store.pr}/comments?per_page=100`,
      `repos/${store.repo}/pulls/${store.pr}/comments?per_page=100`,
    ].flatMap((endpoint) => {
      const pages = parseJsonOrFail<unknown[][]>(
        this.inner.runGh(['api', '--paginate', '--slurp', endpoint]),
        'GitHub returned invalid JSON',
      );
      return pages.flat() as Array<Record<string, unknown>>;
    });
    const chain = prior.filter(
      (row) =>
        (row['user'] as { login?: string } | undefined)?.login === actor &&
        String(row['body'] ?? '').startsWith('<!-- local-review'),
    );
    if (chain.length > 0)
      fail(
        `${store.repo}#${store.pr} already carries ${chain.length} review-chain comment(s) on GitHub; silent mode cannot start part-way through a PR. Finish this PR's review as it is, or open a new PR.`,
      );
    return store.writePin(actor, source);
  }

  private scopeStore(): LocalStore | null {
    return this.scope ? this.storeFor(this.scope.repo, this.scope.pr) : null;
  }

  private repoIsSilent(repo: string): boolean {
    if (this.scope && this.scope.repo.toLowerCase() === repo.toLowerCase())
      return this.scopeStore() !== null;
    return profileSaysSilent(repo) !== null;
  }

  private refuse(what: string): never {
    fail(
      `silent mode: refusing ${what}; a silent PR writes nothing to GitHub until the end-of-loop summary is approved`,
    );
  }

  // --- dispatch --------------------------------------------------------------

  runGh(args: string[], payload?: unknown): string {
    const request = parseApi(args);
    if (request === null) {
      const command = args.slice(0, 2).join(' ');
      if (!READ_ONLY_GH.has(command) && this.scopeStore() !== null)
        this.refuse(`gh ${command}`);
      return this.inner.runGh(args, payload);
    }
    if (request.endpoint === 'graphql')
      return this.graphql(args, request, payload);
    const handled = this.rest(request, payload);
    if (handled !== null) return handled;
    if (request.method !== 'GET') {
      const repo = REPO_RE.exec(request.endpoint)?.[1];
      if (
        this.scopeStore() !== null ||
        (repo !== undefined && this.repoIsSilent(repo))
      )
        this.refuse(`${request.method} ${request.endpoint}`);
    }
    return this.inner.runGh(args, payload);
  }

  private rest(request: ApiRequest, payload: unknown): string | null {
    const { method, endpoint } = request;
    const body = (payload as { body?: unknown } | undefined)?.body;
    let match = PR_COMMENTS_RE.exec(endpoint);
    if (match) {
      const store = this.storeFor(match[1]!, Number(match[2]));
      if (store === null) return null;
      if (method === 'GET')
        return this.rows(store.read().review_comments, request.slurp);
      if (method !== 'POST') this.refuse(`${method} ${endpoint}`);
      return json(this.postRoot(store, payload as Record<string, unknown>));
    }
    match = PR_REPLIES_RE.exec(endpoint);
    if (match) {
      const store = this.storeFor(match[1]!, Number(match[2]));
      if (store === null) return null;
      if (method !== 'POST') this.refuse(`${method} ${endpoint}`);
      return json(this.postReply(store, Number(match[3]), String(body)));
    }
    match = ISSUE_COMMENTS_RE.exec(endpoint);
    if (match) {
      const store = this.storeFor(match[1]!, Number(match[2]));
      if (store === null) return null;
      if (method === 'GET')
        return this.rows(store.read().issue_comments, request.slurp);
      if (method !== 'POST') this.refuse(`${method} ${endpoint}`);
      return json(this.postIssueComment(store, String(body)));
    }
    match = REVIEW_COMMENT_RE.exec(endpoint) ?? ISSUE_COMMENT_RE.exec(endpoint);
    if (match) {
      const id = Number(match[2]);
      const review = REVIEW_COMMENT_RE.test(endpoint);
      if (!isLocalId(id)) {
        if (method !== 'GET' && this.repoIsSilent(match[1]!))
          this.refuse(`${method} ${endpoint}`);
        return null;
      }
      const store = this.scopeStore();
      if (store === null)
        fail(`local comment ${id} needs --repo and --pr naming its silent PR`);
      return this.single(store, review, id, method, body);
    }
    return null;
  }

  private rows(rows: unknown[], slurp: boolean): string {
    return json(slurp ? [rows] : rows);
  }

  private postRoot(
    store: LocalStore,
    payload: Record<string, unknown>,
  ): LocalReviewComment {
    const actor = this.inner.currentActor();
    const row = store.update((state) => {
      const id = allocateId(state);
      const at = nowIso();
      const fileLevel = payload['subject_type'] === 'file';
      const comment: LocalReviewComment = {
        id,
        node_id: `PRRC_local_${id}`,
        body: String(payload['body']),
        user: { login: actor },
        path: typeof payload['path'] === 'string' ? payload['path'] : null,
        line:
          fileLevel || typeof payload['line'] !== 'number'
            ? null
            : payload['line'],
        side:
          fileLevel || typeof payload['side'] !== 'string'
            ? null
            : payload['side'],
        subject_type: fileLevel ? 'file' : 'line',
        commit_id:
          typeof payload['commit_id'] === 'string'
            ? payload['commit_id']
            : null,
        created_at: at,
        updated_at: at,
      };
      state.review_comments.push(comment);
      state.threads.push({
        id: `${LOCAL_THREAD_PREFIX}${id}`,
        root_id: id,
        resolved: false,
      });
      return comment;
    });
    store.note(
      `- finding ${row.id} on \`${row.path ?? '?'}\`: ${headline(row.body)}`,
    );
    return row;
  }

  private postReply(
    store: LocalStore,
    target: number,
    body: string,
  ): LocalReviewComment {
    if (!isLocalId(target)) this.refuse(`a reply to GitHub comment ${target}`);
    const actor = this.inner.currentActor();
    const row = store.update((state) => {
      const parent = state.review_comments.find((c) => c.id === target);
      if (!parent) notFound();
      const rootId = parent.in_reply_to_id ?? parent.id;
      const root = state.review_comments.find((c) => c.id === rootId)!;
      const id = allocateId(state);
      const at = nowIso();
      const comment: LocalReviewComment = {
        id,
        node_id: `PRRC_local_${id}`,
        body,
        user: { login: actor },
        path: root.path,
        line: root.line,
        side: root.side,
        subject_type: root.subject_type,
        commit_id: root.commit_id,
        in_reply_to_id: rootId,
        created_at: at,
        updated_at: at,
      };
      state.review_comments.push(comment);
      return comment;
    });
    store.note(`  - reply on ${row.in_reply_to_id}: ${headline(body)}`);
    return row;
  }

  private postIssueComment(store: LocalStore, body: string): LocalIssueComment {
    const actor = this.inner.currentActor();
    const row = store.update((state) => {
      const id = allocateId(state);
      const at = nowIso();
      const comment: LocalIssueComment = {
        id,
        node_id: `IC_local_${id}`,
        body,
        user: { login: actor },
        created_at: at,
        updated_at: at,
        last_edited_at: null,
      };
      state.issue_comments.push(comment);
      return comment;
    });
    const marker = /^<!-- (local-review[a-z-]*:v[0-9]+)/.exec(body)?.[1];
    if (marker) store.note(`- ${row.created_at} ${marker}`);
    return row;
  }

  private single(
    store: LocalStore,
    review: boolean,
    id: number,
    method: string,
    body: unknown,
  ): string {
    if (method === 'GET') {
      const state = store.read();
      const rows: Array<Record<string, unknown>> = review
        ? state.review_comments
        : state.issue_comments;
      const row = rows.find((r) => r['id'] === id);
      if (!row) notFound();
      return json(row);
    }
    if (review) this.refuse(`${method} on review comment ${id}`);
    if (method === 'PATCH') {
      return json(
        store.update((state) => {
          const row = state.issue_comments.find((r) => r.id === id);
          if (!row) notFound();
          row.body = String(body);
          row.updated_at = nowIso();
          row.last_edited_at = row.updated_at;
          return row;
        }),
      );
    }
    if (method === 'DELETE') {
      store.update((state) => {
        const index = state.issue_comments.findIndex((r) => r.id === id);
        if (index < 0) notFound();
        state.issue_comments.splice(index, 1);
      });
      return '';
    }
    this.refuse(`${method} on comment ${id}`);
  }

  // --- GraphQL ---------------------------------------------------------------

  private graphql(
    args: string[],
    request: ApiRequest,
    payload: unknown,
  ): string {
    const body = (payload ?? {}) as {
      query?: unknown;
      variables?: Record<string, unknown>;
    };
    const query = String(body.query ?? request.fields['query'] ?? '');
    const variables: Record<string, unknown> = {
      ...request.fields,
      ...(body.variables ?? {}),
    };
    const threadId =
      typeof variables['threadId'] === 'string' ? variables['threadId'] : null;

    if (/^\s*mutation\b/.test(query)) {
      const field = /\b(resolveReviewThread|unresolveReviewThread)\s*\(/.exec(
        query,
      )?.[1];
      if (field && threadId !== null) {
        if (!isLocalThreadId(threadId)) return this.inner.runGh(args, payload);
        const store = this.threadStore(threadId);
        const resolved = field === 'resolveReviewThread';
        store.update((state) => {
          const thread = state.threads.find((t) => t.id === threadId);
          if (!thread) fail(`unknown local review thread ${threadId}`);
          thread.resolved = resolved;
        });
        store.note(
          `  - thread ${threadId} ${resolved ? 'resolved' : 'reopened'}`,
        );
        return json({
          data: { [field]: { thread: { id: threadId, isResolved: resolved } } },
        });
      }
      if (this.scopeStore() !== null) this.refuse('a GraphQL mutation');
      return this.inner.runGh(args, payload);
    }

    if (threadId !== null && /\bnode\s*\(\s*id\s*:/.test(query)) {
      if (!isLocalThreadId(threadId)) return this.inner.runGh(args, payload);
      const store = this.threadStore(threadId);
      const node = threadNodes(store, store.read(), 100).find(
        (t) => t['id'] === threadId,
      );
      return json({ data: { node: node ?? null } });
    }

    const owner = variables['owner'];
    const name = variables['name'];
    const number = Number(variables['number']);
    if (
      typeof owner === 'string' &&
      typeof name === 'string' &&
      Number.isSafeInteger(number)
    ) {
      const repo = `${owner}/${name}`;
      if (/\breviewThreads\s*\(/.test(query)) {
        const store = this.storeFor(repo, number);
        if (store !== null) {
          const first = Number(
            /comments\s*\(\s*first\s*:\s*(\d+)/.exec(query)?.[1] ?? 100,
          );
          const nodes = threadNodes(store, store.read(), first);
          const page = {
            data: {
              repository: {
                pullRequest: {
                  reviewThreads: {
                    nodes,
                    pageInfo: { hasNextPage: false, endCursor: null },
                  },
                },
              },
            },
          };
          return json(request.slurp ? [page] : page);
        }
      } else if (/\bissueOrPullRequest\s*\(/.test(query)) {
        const store = this.storeFor(repo, number);
        if (store !== null) {
          const rows = store.read().issue_comments;
          const page = {
            data: {
              repository: {
                issueOrPullRequest: {
                  comments: {
                    totalCount: rows.length,
                    pageInfo: { hasNextPage: false, endCursor: null },
                    nodes: rows.map((row) => ({
                      databaseId: row.id,
                      body: row.body,
                      createdAt: row.created_at,
                      lastEditedAt: row.last_edited_at,
                      author: { login: row.user.login },
                    })),
                  },
                },
              },
            },
          };
          return json(request.slurp ? [page] : page);
        }
      }
    }
    return this.inner.runGh(args, payload);
  }

  private threadStore(threadId: string): LocalStore {
    const store = this.scopeStore();
    if (store === null)
      fail(
        `local review thread ${threadId} needs --repo and --pr naming its silent PR`,
      );
    return store;
  }
}

function threadNodes(
  store: LocalStore,
  state: LocalState,
  first: number,
): Array<Record<string, unknown>> {
  return state.threads.map((thread) => {
    const comments = state.review_comments.filter(
      (c) => c.id === thread.root_id || c.in_reply_to_id === thread.root_id,
    );
    return {
      id: thread.id,
      isResolved: thread.resolved,
      repository: { nameWithOwner: store.repo },
      pullRequest: { number: store.pr },
      comments: {
        nodes: comments
          .slice(0, first)
          .map((c) => ({
            databaseId: c.id,
            body: c.body,
            author: { login: c.user.login },
          })),
        pageInfo: { hasNextPage: comments.length > first },
      },
    };
  });
}

/** First readable line of a ledger body, for the scratchpad. */
export function headline(body: string): string {
  const line =
    body
      .split('\n')
      .slice(1)
      .map((l) => l.trim().replace(/^\*\*(.*)\*\*$/, '$1'))
      .find((l) => l.length > 0) ?? '';
  return line.length > 160 ? `${line.slice(0, 157)}...` : line;
}
