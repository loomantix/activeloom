import { fail } from './errors.js';
import { sha256Text } from './hash.js';
import { parseJsonOrFail } from './io.js';
import type { LocalState, LocalStore } from './local-store.js';
import { matchDisposition, matchFinding } from './protocol.js';
import type { DelegateRunner } from './silent-runner.js';

/**
 * The end-of-loop summary for a silent PR: the only text silent mode ever
 * posts, and only after the user approves these exact bytes.
 */

export interface SilentSummary {
  summary: string;
  sha256: string;
  fixed: number;
  deferred: number;
  dismissed: number;
  open: number;
  outcome: string | null;
  scratchpad: string;
}

const RUN_END_RE =
  /^<!-- local-review-run-end:v1 id=[0-9a-f]{64} outcome=(converged|exhausted|aborted) /;
const MAX_FIX_TEXT = 140;

function prose(body: string): string {
  return body.split('\n').slice(1).join(' ').replace(/\s+/g, ' ').trim();
}

function title(body: string): string {
  const line =
    body
      .split('\n')
      .slice(1)
      .map((l) => l.trim())
      .find((l) => l.length > 0) ?? '';
  return line.replace(/^\*\*(.*)\*\*$/, '$1').replace(/[.:]\s*$/, '');
}

function fixText(body: string): string {
  const text = prose(body).replace(/^Fixed in [0-9a-f]{7,40}\.?\s*/i, '');
  const sentence = /^(.*?[.!?])(?:\s|$)/.exec(text)?.[1] ?? text;
  return sentence.length > MAX_FIX_TEXT
    ? `${sentence.slice(0, MAX_FIX_TEXT - 3)}...`
    : sentence;
}

/** Render the summary from the local ledger. Deterministic for a given state. */
export function renderSummary(
  store: LocalStore,
  state: LocalState,
): SilentSummary {
  const lines: string[] = [];
  let deferred = 0;
  let dismissed = 0;
  let open = 0;
  for (const root of state.review_comments) {
    if (root.in_reply_to_id !== undefined) continue;
    const finding = matchFinding(root.body);
    if (finding === null) continue;
    const replies = state.review_comments.filter(
      (c) => c.in_reply_to_id === root.id,
    );
    let last: { outcome: string; head: string; body: string } | null = null;
    for (const reply of replies) {
      const disposition = matchDisposition(reply.body);
      if (disposition) last = { ...disposition, body: reply.body };
    }
    if (last === null) {
      open += 1;
    } else if (last.outcome === 'fixed') {
      const where = root.path ? ` \`${root.path}\`` : '';
      lines.push(
        `- **${finding.severity}**${where}: ${title(root.body)}. ${fixText(last.body)} (${last.head.slice(0, 7)})`,
      );
    } else if (last.outcome === 'deferred') {
      deferred += 1;
    } else {
      dismissed += 1;
    }
  }
  let outcome: string | null = null;
  for (const row of state.issue_comments) {
    const match = RUN_END_RE.exec(row.body);
    if (match) outcome = match[1]!;
  }

  const parts = ['**Review summary**', ''];
  if (lines.length > 0) parts.push('Fixed:', ...lines, '');
  else parts.push('No changes were needed.', '');
  const counts: string[] = [];
  if (deferred > 0) counts.push(`${deferred} deferred to follow-up`);
  if (dismissed > 0) counts.push(`${dismissed} dismissed after review`);
  if (open > 0) counts.push(`${open} still open`);
  if (counts.length > 0)
    parts.push(`Other findings: ${counts.join(', ')}.`, '');
  parts.push(
    outcome === 'converged' && open === 0
      ? 'Result: the review converged with no open findings.'
      : `Result: the review ended ${outcome ?? 'without a recorded outcome'}.`,
  );
  const summary = `${parts.join('\n')}\n`;
  return {
    summary,
    sha256: sha256Text(summary),
    fixed: lines.length,
    deferred,
    dismissed,
    open,
    outcome,
    scratchpad: store.scratchpadPath,
  };
}

/**
 * Post the approved summary to the PR through the real GitHub runner. The
 * digest the user approved must still match, and a repeat call replays the
 * first post instead of posting twice.
 */
export function postSummary(
  real: DelegateRunner,
  store: LocalStore,
  expectedSha256: string,
): { comment_id: number; replayed: boolean } {
  const state = store.read();
  const rendered = renderSummary(store, state);
  if (rendered.sha256 !== expectedSha256)
    fail(
      'the summary changed since it was shown; render it again and ask the user before posting',
    );
  if (state.summary_post?.sha256 === expectedSha256)
    return { comment_id: state.summary_post.comment_id, replayed: true };
  if (state.summary_post !== null)
    fail(
      `a different summary was already posted as comment ${state.summary_post.comment_id}`,
    );

  const actor = real.liveActor();
  const endpoint = `repos/${store.repo}/issues/${store.pr}/comments`;
  const existing = parseJsonOrFail<Array<Array<Record<string, unknown>>>>(
    real.runGh(['api', '--paginate', '--slurp', `${endpoint}?per_page=100`]),
    'GitHub returned invalid JSON',
  )
    .flat()
    .find(
      (row) =>
        row['body'] === rendered.summary &&
        (row['user'] as { login?: string } | undefined)?.login === actor,
    );
  let commentId: number;
  let replayed = false;
  if (existing && typeof existing['id'] === 'number') {
    commentId = existing['id'];
    replayed = true;
  } else {
    const response = parseJsonOrFail<{ id?: unknown }>(
      real.runGh(['api', '-X', 'POST', endpoint], { body: rendered.summary }),
      'GitHub returned invalid JSON',
    );
    if (typeof response.id !== 'number')
      fail('GitHub accepted the summary but returned no comment ID');
    commentId = response.id;
  }
  store.update((s) => {
    s.summary_post = { comment_id: commentId, sha256: expectedSha256 };
  });
  store.note(`- summary posted as comment ${commentId}`);
  return { comment_id: commentId, replayed };
}
