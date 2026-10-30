import { fail } from './errors.js';
import { sha256Text } from './hash.js';
import { reviewRuns, type ReviewRun } from './runs.js';

const TRANSITION = '<!-- local-review-transition:v1 ';
const OWNER = /^<!-- local-review-owner-transition:v1 id=([0-9a-f]{64}) -->\n/;
const END =
  /^<!-- local-review-run-end:v1 id=([0-9a-f]{64}) outcome=(converged|exhausted|aborted) head=([0-9a-f]{40}) -->/;
const LOGIN = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?$/;

/** The user's transition approval never authorizes a new review budget. */
export interface OwnerTransition {
  approval_id: string;
  created: string;
  expires: string;
  from_actor: string;
  head: string;
  pr: number;
  repo: string;
  run_comment_id: number;
  run_id: string;
  to_actor: string;
  version: 1;
}

/** Actor-qualified run identity; v1 digests are not globally scoped identities. */
export interface LifecycleRun extends ReviewRun {
  actor: string;
  startHead: string;
  outcome: string | null;
  ownerTransition: string | null;
}

/** One decision shared by controllers, runners and shipping evidence consumers. */
export interface LifecycleState {
  state:
    | 'none'
    | 'active'
    | 'ended'
    | 'foreign-active'
    | 'foreign-ended'
    | 'transitioned'
    | 'foreign-transition';
  actor: string;
  run: LifecycleRun | null;
  transition: {
    id: string;
    commentId: number;
    binding: OwnerTransition;
  } | null;
  transitions: Array<{
    id: string;
    commentId: number;
    binding: OwnerTransition;
  }>;
}

/** Canonical transition serialization is identical in Python and JavaScript. */
export function transitionJson(value: OwnerTransition): string {
  return JSON.stringify(value, Object.keys(value).sort());
}

/** Restrict records to the complete explicitly scoped authorization schema. */
export function parseTransition(value: unknown): OwnerTransition {
  if (value === null || typeof value !== 'object' || Array.isArray(value))
    fail('invalid transition binding');
  const row = value as Record<string, unknown>;
  const keys = [
    'approval_id',
    'created',
    'expires',
    'from_actor',
    'head',
    'pr',
    'repo',
    'run_comment_id',
    'run_id',
    'to_actor',
    'version',
  ];
  if (JSON.stringify(Object.keys(row).sort()) !== JSON.stringify(keys))
    fail('transition fields differ');
  if (
    row['version'] !== 1 ||
    typeof row['approval_id'] !== 'string' ||
    !/^[0-9a-f]{16}$/.test(row['approval_id']) ||
    typeof row['repo'] !== 'string' ||
    !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(row['repo']) ||
    !Number.isSafeInteger(row['pr']) ||
    Number(row['pr']) < 1 ||
    !Number.isSafeInteger(row['run_comment_id']) ||
    Number(row['run_comment_id']) < 1 ||
    typeof row['head'] !== 'string' ||
    !/^[0-9a-f]{40}$/.test(row['head']) ||
    typeof row['run_id'] !== 'string' ||
    !/^[0-9a-f]{64}$/.test(row['run_id']) ||
    typeof row['from_actor'] !== 'string' ||
    !LOGIN.test(row['from_actor']) ||
    typeof row['to_actor'] !== 'string' ||
    !LOGIN.test(row['to_actor']) ||
    row['from_actor'] === row['to_actor']
  )
    fail('invalid transition identity');
  for (const field of ['created', 'expires']) {
    if (
      typeof row[field] !== 'string' ||
      !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(row[field] as string)
    )
      fail('invalid transition time');
  }
  const created = Date.parse(String(row['created']));
  const expires = Date.parse(String(row['expires']));
  if (
    !Number.isFinite(created) ||
    !Number.isFinite(expires) ||
    expires <= created ||
    expires - created > 72 * 3600000
  )
    fail('invalid transition expiry');
  return {
    version: 1,
    approval_id: String(row['approval_id']),
    created: String(row['created']),
    expires: String(row['expires']),
    from_actor: String(row['from_actor']),
    to_actor: String(row['to_actor']),
    head: String(row['head']),
    repo: String(row['repo']),
    pr: Number(row['pr']),
    run_id: String(row['run_id']),
    run_comment_id: Number(row['run_comment_id']),
  };
}

/** A lifecycle marker cannot be edited or have missing author/edit metadata. */
function identity(row: Record<string, unknown>): {
  actor: string;
  id: number;
  body: string;
} {
  const author = row['user'];
  const login =
    author && typeof author === 'object'
      ? (author as Record<string, unknown>)['login']
      : null;
  if (
    typeof login !== 'string' ||
    !LOGIN.test(login) ||
    !Number.isSafeInteger(row['id']) ||
    Number(row['id']) < 1 ||
    typeof row['body'] !== 'string' ||
    !Object.hasOwn(row, 'lastEditedAt') ||
    row['lastEditedAt'] !== null ||
    typeof row['created_at'] !== 'string'
  )
    fail('edited or unauthenticated lifecycle marker');
  return { actor: login, id: Number(row['id']), body: row['body'] as string };
}

/** Validate actor-local chains first, then replay PR-wide ownership in comment order. */
export function lifecycleState(
  rows: Array<Record<string, unknown>>,
  repo: string,
  pr: number,
  actor: string,
): LifecycleState {
  const relevant = rows.filter(
    (row) =>
      typeof row['body'] === 'string' &&
      /^<!-- local-review-(?:run:|run-end:|transition:)/.test(row['body']),
  );
  const groups = new Map<string, Array<Record<string, unknown>>>();
  for (const row of relevant) {
    const entry = identity(row);
    groups.set(entry.actor, [...(groups.get(entry.actor) ?? []), row]);
  }
  const runs = new Map<number, LifecycleRun>();
  for (const [owner, comments] of groups) {
    for (const run of reviewRuns(comments)) {
      const body = String(
        comments.find((row) => row['id'] === run.commentId)!['body'],
      );
      const startHead = / start-head=([0-9a-f]{40}) /.exec(body)![1]!;
      const content = body.slice(body.indexOf('\n') + 1);
      runs.set(run.commentId, {
        ...run,
        actor: owner,
        startHead,
        outcome: null,
        ownerTransition: OWNER.exec(content)?.[1] ?? null,
      });
    }
  }
  let current: LifecycleRun | null = null;
  let pending: LifecycleState['transition'] = null;
  const transitions: LifecycleState['transitions'] = [];
  const seen = new Map<string, string>();
  const ends = new Set<string>();
  for (const row of relevant.sort(
    (a, b) => Number(a['id']) - Number(b['id']),
  )) {
    const entry = identity(row);
    const run = runs.get(entry.id);
    if (run) {
      if (pending) {
        const content = entry.body.slice(entry.body.indexOf('\n') + 1);
        const owner = OWNER.exec(content);
        if (
          entry.actor !== pending.binding.to_actor ||
          owner?.[1] !== pending.id ||
          run.startHead !== pending.binding.head
        )
          fail('new run does not bind the authorized owner transition');
        pending = null;
      } else if (
        current &&
        (current.actor !== run.actor || current.outcome === null)
      ) {
        fail('unrelated active run or actor change without a transition');
      } else if (run.ownerTransition !== null) {
        fail('run reuses an ownership transition without a pending handoff');
      }
      current = run;
      continue;
    }
    if (entry.body.startsWith('<!-- local-review-run:v1')) continue; // actor-local exact replay alias
    if (entry.body.startsWith('<!-- local-review-run-end:')) {
      const match = END.exec(entry.body);
      if (!match) fail('malformed terminal lifecycle marker');
      const marker = match[0];
      const suffix =
        match[2] === 'converged'
          ? ''
          : ' This run does not establish convergence.';
      if (
        entry.body !== marker &&
        entry.body !==
          `${marker}\n\nReview ${match[2]} at \`${match[3]!.slice(0, 12)}\`.${suffix}`
      )
        fail('terminal marker has unexpected content');
      const owned = [...runs.values()].filter(
        (item) =>
          item.actor === entry.actor &&
          item.id === match[1] &&
          item.commentId < entry.id,
      );
      if (owned.length !== 1)
        fail('terminal marker does not identify one actor-owned run');
      const key = `${entry.actor}:${owned[0]!.commentId}`;
      if (ends.has(key)) fail('duplicate terminal lifecycle marker');
      ends.add(key);
      owned[0]!.outcome = match[2]!;
      continue;
    }
    if (!entry.body.startsWith(TRANSITION))
      fail('unsupported lifecycle marker');
    const match =
      /^<!-- local-review-transition:v1 id=([0-9a-f]{64}) -->\n(.+)$/.exec(
        entry.body,
      );
    if (!match) fail('malformed owner transition');
    let decoded: unknown;
    try {
      decoded = JSON.parse(match[2]!);
    } catch {
      fail('invalid owner transition JSON');
    }
    const binding = parseTransition(decoded);
    if (
      transitionJson(binding) !== match[2] ||
      sha256Text(match[2]!) !== match[1] ||
      binding.repo !== repo ||
      binding.pr !== pr ||
      binding.to_actor !== entry.actor
    )
      fail('transition scope or authorship mismatch');
    const posted = Date.parse(String(row['created_at']));
    if (
      !Number.isFinite(posted) ||
      posted < Date.parse(binding.created) ||
      posted >= Date.parse(binding.expires)
    )
      fail('transition was published outside its approval window');
    const duplicate = seen.get(match[1]!);
    if (duplicate !== undefined) {
      if (duplicate !== entry.body) fail('conflicting transition replay');
      continue;
    }
    if (
      pending ||
      !current ||
      current.actor !== binding.from_actor ||
      current.commentId !== binding.run_comment_id ||
      current.id !== binding.run_id
    )
      fail('competing or stale owner transition');
    pending = { id: match[1]!, commentId: entry.id, binding };
    transitions.push(pending);
    seen.set(pending.id, entry.body);
  }
  const state = pending
    ? pending.binding.to_actor === actor
      ? 'transitioned'
      : 'foreign-transition'
    : !current
      ? 'none'
      : current.actor === actor
        ? current.outcome === null
          ? 'active'
          : 'ended'
        : current.outcome === null
          ? 'foreign-active'
          : 'foreign-ended';
  return { state, actor, run: current, transition: pending, transitions };
}

/** Foreign history and abandoned runs are never current-actor coverage. */
export function requireLifecycleOwner(state: LifecycleState): void {
  if (!['none', 'active', 'ended'].includes(state.state))
    fail(
      `review lifecycle is ${state.state}; explicit owner transition and a separate fresh restart are required`,
    );
}
