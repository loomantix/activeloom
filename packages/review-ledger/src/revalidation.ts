import { fail } from './errors.js';
import { requireSha, requireToken, sha256Text } from './hash.js';
import {
  assertActor,
  assertLiveActor,
  currentActor,
  getIssueComments,
  getReviewComments,
  getThreadState,
  postReviewComment,
  readLifecycle,
  verifyHead,
  compareIsForward,
} from './github.js';
import {
  buildFindingBody,
  buildSeverityCorrectionBody,
  matchDisposition,
  matchFinding,
  matchSeverityCorrection,
  validateContentString,
  resolveContent,
} from './protocol.js';
import { reviewRuns } from './runs.js';
import type { GitHubReviewCommentNode, SupportedEngine } from './types.js';

const PREFIX = '<!-- local-review-revalidation:v1 ';

export interface Revalidation {
  engine: SupportedEngine;
  round: number;
  head: string;
  fingerprint: string;
  occurrence: number;
  fromEngine: SupportedEngine;
  fromRound: number;
  fromHead: string;
  runId: string;
  runCommentId: number;
  maxRounds: number;
  contentSha: string;
}

export interface RevalidateOccurrenceParams {
  repo: string;
  pr: number;
  head: string;
  engine: SupportedEngine;
  round: number;
  fingerprint: string;
  occurrence: number;
  commentId: number;
  threadId: string;
  content?: string | undefined;
  contentFile?: string | undefined;
}

function canonical(record: Revalidation): string {
  return JSON.stringify(record, Object.keys(record).sort());
}

export function matchRevalidation(body: string): Revalidation | null {
  if (!body.includes('<!-- local-review-revalidation:')) return null;
  const line = body.split('\n')[0]!;
  if (!line.startsWith(PREFIX) || !line.endsWith(' -->'))
    fail('malformed revalidation marker');
  let record: Revalidation;
  try {
    record = JSON.parse(line.slice(PREFIX.length, -4)) as Revalidation;
  } catch {
    fail('malformed revalidation JSON');
  }
  const keys = [
    'contentSha',
    'engine',
    'fingerprint',
    'fromEngine',
    'fromHead',
    'fromRound',
    'head',
    'maxRounds',
    'occurrence',
    'round',
    'runCommentId',
    'runId',
  ];
  if (
    !record ||
    JSON.stringify(Object.keys(record).sort()) !== JSON.stringify(keys)
  )
    fail('revalidation fields differ');
  if (line !== `${PREFIX}${canonical(record)} -->`)
    fail('noncanonical revalidation marker');
  const engines = ['codex', 'claude', 'gemini', 'antigravity'];
  if (!engines.includes(record.engine) || !engines.includes(record.fromEngine))
    fail('invalid revalidation engine');
  for (const number of [
    record.round,
    record.fromRound,
    record.occurrence,
    record.runCommentId,
    record.maxRounds,
  ])
    if (!Number.isSafeInteger(number) || number < 1)
      fail('invalid revalidation integer');
  requireSha(record.head, 'head');
  requireSha(record.fromHead, 'fromHead');
  requireToken(record.fingerprint, 'fingerprint');
  if (!/^[0-9a-f]{64}$/.test(record.runId)) fail('invalid revalidation run');
  const content = body.slice(line.length + 1);
  validateContentString(content);
  if (sha256Text(content) !== record.contentSha)
    fail('invalid revalidation content hash');
  return record;
}

/** A derived view only: persisted finding markers and replies are never rewritten. */
export function revalidatedComments(
  input: GitHubReviewCommentNode[],
  repo: string,
  pr: number,
  sameThread = false,
): GitHubReviewCommentNode[] {
  const actor = currentActor();
  const owned = (row: GitHubReviewCommentNode): boolean =>
    (row.author ?? row.user)?.login === actor;
  if (
    !input.some(
      (row) =>
        owned(row) &&
        String(row.body ?? '').includes('<!-- local-review-revalidation:'),
    )
  )
    return input;
  const rows = input.map((row) => ({ ...row }));
  const originalRows = input;
  const runs = reviewRuns(getIssueComments(repo, pr));
  const carriedRuns = new Map<string, number>();
  for (let index = 0; index < rows.length; index++) {
    const row = rows[index]!;
    if (!owned(row)) continue;
    const record = matchRevalidation(String(row.body ?? ''));
    if (!record) continue;
    const runIndex = runs.findIndex(
      (run) => run.id === record.runId && run.commentId === record.runCommentId,
    );
    const run = runs[runIndex];
    if (
      !run ||
      run.maxRounds !== record.maxRounds ||
      record.round > run.maxRounds
    )
      fail('revalidation run identity or round cap differs');
    const occurrenceKey = `${record.fingerprint}#${record.occurrence}`;
    const previousRunIndex = carriedRuns.get(occurrenceKey);
    const restartedAtSameHead =
      record.fromHead === record.head &&
      previousRunIndex !== undefined &&
      previousRunIndex < runIndex;
    if (
      !restartedAtSameHead &&
      (record.fromHead === record.head ||
        !compareIsForward(repo, record.fromHead, record.head))
    )
      fail('revalidation requires a forward head transition');
    const findings = rows
      .map((r, i) => ({
        row: r,
        index: i,
        finding: owned(r) ? matchFinding(String(r.body ?? '')) : null,
      }))
      .filter((entry) => entry.finding?.fingerprint === record.fingerprint);
    const roots = findings.filter((entry) => entry.finding!.occurrence === 1);
    if (roots.length !== 1)
      fail('revalidation requires exactly one fingerprint root thread');
    if (findings.some((entry, i) => entry.finding!.occurrence !== i + 1))
      fail('revalidation finding occurrence sequence is invalid');
    const rootId = roots[0]!.row.databaseId ?? roots[0]!.row.id;
    if (
      (!sameThread || row['in_reply_to_id'] !== undefined) &&
      row['in_reply_to_id'] !== rootId
    )
      fail('revalidation reply belongs to a different root thread');
    const targets = findings.filter(
      (entry) =>
        entry.index < index &&
        entry.finding!.engine === record.fromEngine &&
        entry.finding!.round === record.fromRound &&
        entry.finding!.head === record.fromHead &&
        entry.finding!.occurrence === record.occurrence,
    );
    if (
      targets.length !== 1 ||
      findings.some(
        (entry) =>
          entry.index < index && entry.finding!.occurrence > record.occurrence,
      )
    )
      fail('revalidation does not identify the latest earlier finding');
    if (
      rows.slice(0, index).some((r) => {
        if (!owned(r)) return false;
        const disposition = matchDisposition(String(r.body ?? ''));
        return (
          disposition?.fingerprint === record.fingerprint &&
          disposition.occurrence === record.occurrence
        );
      })
    )
      fail('cannot revalidate a disposed occurrence or fabricated disposition');
    const target = targets[0]!;
    const finding = target.finding!;
    const content = String(target.row.body).slice(
      String(target.row.body).indexOf('\n') + 1,
    );
    target.row.body = buildFindingBody({
      ...finding,
      engine: record.engine,
      round: record.round,
      head: record.head,
      content,
    }).body;
    // Evidence now belongs to this run, even though its root predates the run.
    target.row['createdAt'] = row['createdAt'] ?? row['created_at'];
    target.row['created_at'] = row['created_at'] ?? row['createdAt'];
    // Preserve earlier upward corrections in the derived identity namespace.
    let severity = finding.severity;
    for (const prior of rows.slice(target.index + 1, index)) {
      if (!owned(prior)) continue;
      const correction = matchSeverityCorrection(String(prior.body ?? ''));
      if (
        correction?.fingerprint !== record.fingerprint ||
        correction.occurrence !== record.occurrence
      )
        continue;
      if (
        correction.engine !== finding.engine ||
        correction.round !== finding.round ||
        correction.head !== finding.head ||
        correction.from !== severity ||
        (!sameThread && prior['in_reply_to_id'] !== rootId)
      )
        fail('revalidation has a mismatched severity correction');
      severity = correction.to;
      prior.body = buildSeverityCorrectionBody({
        ...correction,
        engine: record.engine,
        round: record.round,
        head: record.head,
        content: String(prior.body).slice(String(prior.body).indexOf('\n') + 1),
      }).body;
    }
    // The marker remains available in the raw ledger, rather than posing as a finding.
    row.body = 'Verified revalidation evidence.\n';
    carriedRuns.set(occurrenceKey, runIndex);
  }
  // Reject dispositions that were invented in a different identity namespace.
  for (let index = 0; index < rows.length; index++) {
    const row = rows[index]!;
    if (!owned(row)) continue;
    const disposition = matchDisposition(String(row.body ?? ''));
    if (!disposition) continue;
    const carried = originalRows.some((r) => {
      if (!owned(r)) return false;
      const record = matchRevalidation(String(r.body ?? ''));
      return (
        record?.fingerprint === disposition.fingerprint &&
        record.occurrence === disposition.occurrence
      );
    });
    if (!carried) continue;
    const finding = rows
      .slice(0, index)
      .map((r) => (owned(r) ? matchFinding(String(r.body ?? '')) : null))
      .filter(
        (f) =>
          f?.fingerprint === disposition.fingerprint &&
          f.occurrence === disposition.occurrence,
      );
    if (
      finding.length !== 1 ||
      finding[0]!.engine !== disposition.engine ||
      finding[0]!.round !== disposition.round
    )
      fail('revalidated occurrence has a fabricated disposition identity');
  }
  return rows;
}

export function requireRevalidationRun(
  params: Pick<RevalidateOccurrenceParams, 'repo' | 'pr' | 'round'>,
) {
  const state = readLifecycle(params.repo, params.pr);
  if (
    state.state !== 'active' ||
    !state.run ||
    state.run.actor !== currentActor()
  )
    fail('revalidation requires an active authenticated actor-owned run');
  if (
    !Number.isSafeInteger(params.round) ||
    params.round < 1 ||
    params.round > state.run.maxRounds
  )
    fail('revalidation round exceeds the unchanged run cap');
  return state.run;
}

export function revalidateOccurrence(params: RevalidateOccurrenceParams) {
  const actor = assertLiveActor(assertActor());
  verifyHead(params.repo, params.pr, params.head);
  const run = requireRevalidationRun(params);
  const content = resolveContent(params);
  if (content === undefined)
    fail('revalidate-occurrence requires exact-head reproduction evidence');
  validateContentString(content);
  const comments = getReviewComments(params.repo, params.pr);
  const existing = comments.filter((row) => {
    const record = matchRevalidation(String(row.body ?? ''));
    return (
      record?.fingerprint === params.fingerprint &&
      record.occurrence === params.occurrence &&
      record.head === params.head &&
      record.engine === params.engine &&
      record.round === params.round &&
      record.runId === run.id
    );
  });
  if (existing.length > 1) fail('duplicate revalidation identity');
  const prior = existing[0];
  const view = revalidatedComments(
    prior ? comments.filter((row) => row !== prior) : comments,
    params.repo,
    params.pr,
  );
  const findings = view
    .map((row) => ({ row, finding: matchFinding(String(row.body ?? '')) }))
    .filter((entry) => entry.finding?.fingerprint === params.fingerprint);
  const roots = findings.filter((entry) => entry.finding!.occurrence === 1);
  if (
    roots.length !== 1 ||
    (roots[0]!.row.databaseId ?? roots[0]!.row.id) !== params.commentId
  )
    fail('revalidation requires the original fingerprint root comment');
  const target = findings.filter(
    (entry) => entry.finding!.occurrence === params.occurrence,
  );
  if (target.length !== 1)
    fail('revalidation requires an authenticated original occurrence');
  if (
    getThreadState(params.threadId, params.commentId, {
      repo: params.repo,
      pr: params.pr,
    })
  )
    fail('cannot revalidate a resolved thread');
  const record: Revalidation = {
    engine: params.engine,
    round: params.round,
    head: params.head,
    fingerprint: params.fingerprint,
    occurrence: params.occurrence,
    fromEngine: target[0]!.finding!.engine,
    fromRound: target[0]!.finding!.round,
    fromHead: target[0]!.finding!.head,
    runId: run.id,
    runCommentId: run.commentId,
    maxRounds: run.maxRounds,
    contentSha: sha256Text(content),
  };
  const marker = `${PREFIX}${canonical(record)} -->`;
  const body = `${marker}\n${content}`;
  if (prior && prior.body !== body)
    fail('revalidation identity has conflicting evidence');
  const candidate = {
    id: -1,
    user: { login: actor },
    body,
    in_reply_to_id: params.commentId,
  };
  revalidatedComments(
    [...comments.filter((row) => row !== prior), candidate],
    params.repo,
    params.pr,
  );
  assertLiveActor(actor);
  const posted = postReviewComment(
    params.repo,
    params.pr,
    params.head,
    marker,
    body,
    { replyTo: params.commentId },
  );
  revalidatedComments(
    getReviewComments(params.repo, params.pr),
    params.repo,
    params.pr,
  );
  if (
    getThreadState(params.threadId, params.commentId, {
      repo: params.repo,
      pr: params.pr,
    })
  )
    fail('revalidation thread was resolved during publication');
  verifyHead(params.repo, params.pr, params.head);
  if (requireRevalidationRun(params).id !== run.id)
    fail('active run changed during revalidation');
  assertLiveActor(actor);
  return {
    comment_id: posted.commentId,
    replayed: posted.replayed,
    verified: true,
  };
}
