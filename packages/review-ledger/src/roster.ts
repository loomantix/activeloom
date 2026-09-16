import {
  COMPLETE_V3_RE,
  PASS_V3_RE,
  ROSTER_ANY_MARKER,
  ROSTER_V1_MARKER,
  ROSTER_V1_RE,
  ROSTER_V2_MARKER,
  ROSTER_V2_RE,
  SUPPORTED_ENGINES,
} from './constants.js';
import { fail, LedgerError } from './errors.js';
import {
  assertActor,
  deleteIssueComment,
  findMatchingBody,
  getIssueComments,
  getPostedCommentId,
  jsonOutput,
  verifyHead,
  verifyIssueComment,
} from './github.js';
import { requireSha, sha256Text } from './hash.js';
import { reviewRunForComment, reviewRuns } from './runs.js';
import {
  matchMarkerLine,
  matchProtocol,
  validateContentString,
} from './protocol.js';
import type {
  AttestationAtHead,
  CoverageParams,
  CoverageResult,
  CoverageTier,
  PostRosterParams,
  PostRosterResult,
  RosterMatch,
  RosterReport,
  SupportedEngine,
} from './types.js';

/**
 * Parse the `reviewers=` field into a validated engine list.
 *
 * `none` is the explicit declaration that this pull request is being reviewed
 * by its author engine alone. It is spelled out rather than represented by an
 * empty value so a truncated or malformed marker cannot read as a deliberate
 * solo review.
 */
export function parseReviewers(
  raw: string,
  author: SupportedEngine,
): SupportedEngine[] {
  if (raw === 'none') {
    return [];
  }
  return validateReviewers(raw.split(','), author);
}

/**
 * Apply the three roster rules to a reviewer list: every entry is a supported
 * engine, none of them is the author, and no engine appears twice.
 *
 * The builder and the parser share this so a roster this package writes is one
 * this package will read back. A rule enforced on only one side would let a
 * marker exist that its own reader rejects.
 */
function validateReviewers(
  reviewers: readonly string[],
  author: SupportedEngine,
): SupportedEngine[] {
  if (reviewers.length > 2) {
    fail('a roster may declare at most two reviewers');
  }
  const validated: SupportedEngine[] = [];
  for (const candidate of reviewers) {
    if (!SUPPORTED_ENGINES.includes(candidate as SupportedEngine)) {
      fail(`reviewer must be one of: ${SUPPORTED_ENGINES.join(', ')}`);
    }
    const engine = candidate as SupportedEngine;
    if (engine === author) {
      fail('the author engine cannot also be listed as a reviewer');
    }
    if (validated.includes(engine)) {
      fail('reviewers must be distinct');
    }
    validated.push(engine);
  }
  return validated;
}

/**
 * Canonical pre-image for a roster:v2 `declaration-sha256`.
 *
 * The declaration is hashed together with the reason prose that justifies it.
 * v1 hashed only the prose, so rewriting `reviewers=codex` to `reviewers=none`
 * in a posted marker left the digest valid and the ledger went on to report an
 * acknowledged solo relay that nobody had declared.
 *
 * The version string leads the pre-image so a roster digest can never collide
 * with the plain content digest every other record in this protocol uses.
 */
export function rosterDigestInput(fields: {
  author: SupportedEngine;
  reviewers: string;
  head: string;
  supersedes: number | null;
  content: string;
}): string {
  return [
    'local-review-roster:v2',
    `author=${fields.author}`,
    `reviewers=${fields.reviewers}`,
    `head=${fields.head}`,
    `supersedes=${fields.supersedes === null ? 'none' : fields.supersedes}`,
    '',
    fields.content,
  ].join('\n');
}

/** Render a validated reviewer list as the marker's `reviewers=` value. */
function formatReviewers(reviewers: readonly SupportedEngine[]): string {
  return reviewers.length === 0 ? 'none' : reviewers.join(',');
}

/**
 * Match the roster marker in a comment body and verify its digest.
 *
 * Reads both grammars. A v1 marker still parses so a pull request already
 * carrying one keeps reporting what it declared; it returns a null `head` and
 * null `supersedes`, which is what makes it advisory downstream.
 */
export function matchRoster(body: string): RosterMatch | null {
  const occurrences = body.split(ROSTER_ANY_MARKER).length - 1;
  if (occurrences === 0) {
    return null;
  }
  if (occurrences > 1) {
    fail('a comment carries more than one local-review roster marker');
  }
  if (body.includes(ROSTER_V2_MARKER)) {
    return matchRosterV2(body);
  }
  if (body.includes(ROSTER_V1_MARKER)) {
    return matchRosterV1(body);
  }
  fail('local-review roster record is of an unsupported protocol version');
}

function matchRosterV2(body: string): RosterMatch {
  const matched = matchMarkerLine(body, ROSTER_V2_RE, ROSTER_V2_MARKER);
  if (matched === null) {
    fail('local-review roster record is malformed');
  }
  const groups = matched.match.groups!;
  const author = groups['author'] as SupportedEngine;
  const rawSupersedes = groups['supersedes']!;
  const supersedes =
    rawSupersedes === 'none' ? null : parseInt(rawSupersedes, 10);
  if (supersedes !== null && !Number.isSafeInteger(supersedes)) {
    fail('local-review roster supersedes must be a comment id');
  }
  const expected = sha256Text(
    rosterDigestInput({
      author,
      reviewers: groups['reviewers']!,
      head: groups['head']!,
      supersedes,
      content: matched.content,
    }),
  );
  if (expected !== groups['declaration_sha']) {
    fail(
      'authenticated local-review-roster:v2 record has an invalid declaration hash',
    );
  }
  return {
    version: 2,
    author,
    reviewers: parseReviewers(groups['reviewers']!, author),
    head: groups['head']!,
    supersedes,
    digest: groups['declaration_sha']!,
  };
}

function matchRosterV1(body: string): RosterMatch {
  const match = matchProtocol(body, ROSTER_V1_RE, ROSTER_V1_MARKER);
  if (!match?.groups) {
    fail('local-review roster record is malformed');
  }
  const author = match.groups['author'] as SupportedEngine;
  return {
    version: 1,
    author,
    reviewers: parseReviewers(match.groups['reviewers']!, author),
    head: null,
    supersedes: null,
    digest: match.groups['content_sha']!,
  };
}

/**
 * Build the roster:v2 marker and its complete comment body.
 *
 * The declared reason travels in the hashed region for the same reason a
 * finding's prose does: a roster is a deliberate, attributable choice, and the
 * justification for it must not be silently editable after the fact. In v2 the
 * declaration travels there too, so the reason cannot be left standing over a
 * roster it no longer describes.
 */
export function buildRosterBody(params: {
  author: SupportedEngine;
  reviewers: readonly SupportedEngine[];
  head: string;
  supersedes: number | null;
  content: string;
}): { marker: string; body: string } {
  if (!SUPPORTED_ENGINES.includes(params.author)) {
    fail(`author must be one of: ${SUPPORTED_ENGINES.join(', ')}`);
  }
  validateContentString(params.content);
  validateReviewers(params.reviewers, params.author);
  requireSha(params.head, 'head');
  if (
    params.supersedes !== null &&
    (!Number.isSafeInteger(params.supersedes) || params.supersedes < 1)
  ) {
    fail('supersedes must be a positive comment id');
  }
  const reviewers = formatReviewers(params.reviewers);
  const declarationSha = sha256Text(
    rosterDigestInput({
      author: params.author,
      reviewers,
      head: params.head,
      supersedes: params.supersedes,
      content: params.content,
    }),
  );
  const marker =
    `${ROSTER_V2_MARKER} author=${params.author} reviewers=${reviewers} ` +
    `head=${params.head} ` +
    `supersedes=${params.supersedes === null ? 'none' : params.supersedes} ` +
    `declaration-sha256=${declarationSha} -->`;
  return { marker, body: `${marker}\n${params.content}` };
}

function absentRoster(): RosterReport {
  return {
    present: false,
    version: null,
    author: null,
    reviewers: [],
    head: null,
    commentId: null,
    supersedes: null,
    chain: [],
  };
}

interface RosterCandidate {
  id: number;
  match: RosterMatch;
}

/** Parse every roster-shaped comment in an actor-owned row set. */
function rosterCandidates(
  rows: Array<Record<string, unknown>>,
): RosterCandidate[] {
  const candidates: RosterCandidate[] = [];
  for (const row of rows) {
    const body = String(row['body'] ?? '');
    if (!body.includes(ROSTER_ANY_MARKER)) {
      continue;
    }
    const match = matchRoster(body);
    if (match === null || typeof row['id'] !== 'number') {
      fail('local-review roster record is malformed');
    }
    candidates.push({ id: row['id'] as number, match });
  }
  return candidates.sort((a, b) => a.id - b.id);
}

/**
 * Resolve the effective roster from the append-only supersession chain.
 *
 * Modelled on the tier marker: comments are read in chronological order and the
 * newest accepted link is the effective one, so narrowing a roster is a visible
 * replacement rather than a silent substitution. Unlike the tier marker this
 * imposes no ancestry requirement between links. A developer who decides late
 * that a change no longer needs a second reviewer must be able to record that
 * in one step, and a rebase must not be able to make that step unreachable.
 * Hard stops are reserved for genuine ambiguity — a forked or dangling chain,
 * two candidates in one comment, a forged digest — never for a human changing
 * their mind.
 */
export function resolveRoster(
  rows: Array<Record<string, unknown>>,
): RosterReport {
  const candidates = rosterCandidates(rows);
  if (candidates.length === 0) {
    return absentRoster();
  }
  const links = candidates.filter((row) => row.match.version === 2);
  if (links.length === 0) {
    return resolveLegacyRoster(candidates);
  }

  const byId = new Map(candidates.map((row) => [row.id, row]));
  const superseded = new Set<number>();
  let roots = 0;
  for (const link of links) {
    const predecessor = link.match.supersedes;
    if (predecessor === null) {
      // The chain opens here. A pull request that already carried a v1 roster
      // opens its chain by superseding that comment instead, so migrating to
      // v2 stays one step and leaves the original declaration standing.
      roots += 1;
      continue;
    }
    const target = byId.get(predecessor);
    if (target === undefined) {
      fail(
        `local-review roster supersedes comment ${predecessor}, which is not a roster on this pull request`,
      );
    }
    if (predecessor >= link.id) {
      fail('local-review roster supersedes a later declaration');
    }
    if (superseded.has(predecessor)) {
      fail('local-review roster supersession chain forks');
    }
    superseded.add(predecessor);
    if (target.match.version !== 2) {
      roots += 1;
    }
  }
  if (roots !== 1) {
    fail(
      roots === 0
        ? 'local-review roster supersession chain has no first declaration'
        : 'local-review roster declares more than one supersession chain',
    );
  }

  const tips = links.filter((link) => !superseded.has(link.id));
  if (tips.length !== 1) {
    fail('local-review roster supersession chain does not resolve to one tip');
  }
  const tip = tips[0]!;

  const chain: number[] = [];
  let cursor: RosterCandidate | undefined = tip;
  while (cursor !== undefined) {
    chain.unshift(cursor.id);
    const predecessor: number | null = cursor.match.supersedes;
    cursor = predecessor === null ? undefined : byId.get(predecessor);
  }
  if (chain.length !== candidates.length) {
    fail('local-review roster supersession chain does not cover every roster');
  }

  return {
    present: true,
    version: 2,
    author: tip.match.author,
    reviewers: tip.match.reviewers,
    head: tip.match.head,
    commentId: tip.id,
    supersedes: tip.match.supersedes,
    chain,
  };
}

/**
 * Resolve a pull request that carries only pre-v2 rosters.
 *
 * v1 has no supersession field, so two of them are a contradiction to reject
 * rather than a history to order — exactly the rule v1 shipped with. Posting a
 * v2 roster is what turns that contradiction into an ordered chain.
 */
function resolveLegacyRoster(candidates: RosterCandidate[]): RosterReport {
  if (candidates.length !== 1) {
    fail('local-review roster is declared more than once');
  }
  const only = candidates[0]!;
  return {
    present: true,
    version: 1,
    author: only.match.author,
    reviewers: only.match.reviewers,
    head: null,
    commentId: only.id,
    supersedes: null,
    chain: [only.id],
  };
}

/**
 * Read the effective actor-owned roster declared on a pull request.
 */
export function readRoster(params: {
  repo: string;
  pr: number;
  actor?: string | undefined;
}): RosterReport {
  assertActor(params.actor);
  return resolveRoster(getIssueComments(params.repo, params.pr));
}

function reconcileConcurrentRoster(
  repo: string,
  pr: number,
  body: string,
  postedCommentId: number,
): { commentId: number; usedPostedComment: boolean } {
  const rows = getIssueComments(repo, pr).filter(
    (row) => String(row['body'] ?? '') === body,
  );
  if (rows.some((row) => typeof row['id'] !== 'number')) {
    fail('local-review roster conflicts with concurrent evidence');
  }
  if (rows.length === 0) {
    fail('local-review roster conflicts with concurrent evidence');
  }

  const ids = rows.map((row) => row['id'] as number).sort((a, b) => a - b);
  const canonical = ids[0]!;
  for (const duplicate of ids.slice(1)) {
    deleteIssueComment(repo, pr, duplicate);
  }
  return {
    commentId: canonical,
    usedPostedComment: canonical === postedCommentId,
  };
}

/**
 * Declare, or re-declare, the engines participating in this review relay.
 *
 * Participation must be declared rather than inferred. An engine that has not
 * posted an attestation is indistinguishable from an engine that was never
 * going to, so without a declared roster no reader can decide whether a round
 * is complete or merely unfinished.
 *
 * Re-declaring is a first-class operation, not a workaround. Posting over an
 * existing roster appends a link naming the one it replaces, so widening the
 * roster, narrowing it to a declared solo relay, or simply re-stating it at a
 * new head are all one deliberate step that leaves the previous declaration
 * standing on the pull request.
 */
export function postRoster(params: PostRosterParams): PostRosterResult {
  assertActor(params.actor);
  requireSha(params.head, 'head');
  verifyHead(params.repo, params.pr, params.head);

  const rows = getIssueComments(params.repo, params.pr);
  const existing = resolveRoster(rows);

  // Replay is byte-identity with the effective roster, rebuilt against that
  // roster's own predecessor: re-running an unchanged declaration returns the
  // comment already posted, while any genuine re-declaration links to the tip
  // and becomes a new comment. Because `supersedes=` is inside the body, a
  // re-declaration can never collide with the link it replaces.
  const replayCandidate =
    existing.present && existing.version === 2
      ? buildRosterBody({
          author: params.author,
          reviewers: params.reviewers,
          head: params.head,
          supersedes: existing.supersedes,
          content: params.content,
        })
      : null;
  const effectiveBody =
    existing.commentId === null
      ? null
      : (rows.find((row) => row['id'] === existing.commentId)?.['body'] ??
        null);
  const isReplay =
    replayCandidate !== null && effectiveBody === replayCandidate.body;

  const supersedes = isReplay ? existing.supersedes : existing.commentId;
  const { marker, body } = isReplay
    ? replayCandidate
    : buildRosterBody({
        author: params.author,
        reviewers: params.reviewers,
        head: params.head,
        supersedes,
        content: params.content,
      });

  let commentId: number;
  let replayed = false;
  let created = false;

  if (isReplay) {
    commentId = existing.commentId!;
    replayed = true;
  } else {
    created = true;
    try {
      const response = jsonOutput(
        [
          'api',
          '-X',
          'POST',
          `repos/${params.repo}/issues/${params.pr}/comments`,
        ],
        { body },
      );
      commentId = getPostedCommentId(response);
    } catch (error) {
      if (error instanceof LedgerError) {
        const recovered = findMatchingBody(
          getIssueComments(params.repo, params.pr),
          marker,
          body,
        );
        if (recovered === null) throw error;
        commentId = recovered;
        replayed = true;
        created = false;
      } else {
        throw error;
      }
    }
  }

  let chain: number[];
  try {
    if (created) {
      const reconciled = reconcileConcurrentRoster(
        params.repo,
        params.pr,
        body,
        commentId,
      );
      commentId = reconciled.commentId;
      replayed = replayed || !reconciled.usedPostedComment;
      created = reconciled.usedPostedComment;
    }
    verifyIssueComment(params.repo, commentId, body);
    const roster = resolveRoster(getIssueComments(params.repo, params.pr));
    if (roster.commentId !== commentId) {
      fail('could not verify the effective local-review roster after posting');
    }
    chain = roster.chain;
    verifyHead(params.repo, params.pr, params.head);
  } catch (error) {
    if (created) {
      try {
        deleteIssueComment(params.repo, params.pr, commentId);
      } catch (rollbackError) {
        throw new LedgerError(
          `roster verification failed and rollback could not be verified: ${
            (rollbackError as { message?: string }).message ??
            String(rollbackError)
          }`,
          { cause: error },
        );
      }
    }
    throw error;
  }

  return {
    comment_id: commentId,
    author: params.author,
    reviewers: [...params.reviewers],
    head: params.head,
    supersedes,
    superseded: supersedes !== null,
    chain,
    replayed,
    verified: true,
  };
}

/** Match one attestation whose marker is the first complete line. */
function matchAttestationMarker(
  body: string,
  pattern: RegExp,
): RegExpExecArray | null {
  const match = pattern.exec(body);
  if (!match || !match.groups || match.index !== 0) {
    return null;
  }
  const matchEnd = match[0].length;
  if (!body.slice(matchEnd).startsWith('\n')) {
    return null;
  }
  if (!body.slice(matchEnd + 1).trim()) {
    return null;
  }
  return match;
}

/**
 * Collect the actor-owned pass and completion attestations naming an exact head.
 *
 * Attestation evidence is per-head by construction, so this is also the
 * invalidation rule: an engine whose newest attestation names an earlier commit
 * has not reviewed what the pull request currently contains, and an engine
 * whose attestation names this commit has, regardless of what moved the head or
 * how many rounds preceded it.
 */
export function attestationsAtHead(
  rows: Array<Record<string, unknown>>,
  head: string,
): AttestationAtHead[] {
  const found: AttestationAtHead[] = [];
  const identities = new Map<string, string>();
  const runs = reviewRuns(rows);
  for (const row of rows) {
    const body = String(row['body'] ?? '');
    const pass = matchAttestationMarker(body, PASS_V3_RE);
    const complete = matchAttestationMarker(body, COMPLETE_V3_RE);
    const match = pass ?? complete;
    if (!match?.groups) {
      continue;
    }
    const engine = match.groups['engine'] as SupportedEngine;
    const round = parseInt(match.groups['round']!, 10);
    const run = reviewRunForComment(runs, row['id']);
    const identity = `${run?.id ?? 'legacy'}|${engine}|${round}`;
    const prior = identities.get(identity);
    if (prior !== undefined && prior !== match[0]) {
      fail('local-review attestation identity is duplicated');
    }
    if (prior !== undefined) continue;
    identities.set(identity, match[0]);
    if (match.groups['head'] === head) {
      found.push({
        engine,
        round,
        status: pass === match ? 'clean' : 'changed',
      });
    }
  }
  return found;
}

/**
 * Classify how many distinct non-author engines reviewed the exact head.
 */
export function coverageTier(count: number): CoverageTier {
  if (count === 0) return 'solo';
  if (count === 1) return 'cross';
  return 'full';
}

/**
 * Report cross-model review coverage for the pull request's exact current head.
 *
 * The author engine's own pass is reported but never counted. It re-reads a
 * change while still holding the rationale that produced it, which is the
 * opposite of the cold read the relay exists to obtain.
 *
 * This reports; it never decides. Nothing here is a merge gate, and nothing
 * here should become one — a developer who has looked at a change and judged
 * its review sufficient is always free to ship it. What this owes them is an
 * accurate record of what actually happened, not a verdict on it.
 */
export function coverage(params: CoverageParams): CoverageResult {
  assertActor(params.actor);
  verifyHead(params.repo, params.pr, params.head);
  const rows = getIssueComments(params.repo, params.pr);
  const roster = resolveRoster(rows);
  const attested = attestationsAtHead(rows, params.head);

  const attestedEngines = [
    ...new Set(attested.map((row) => row.engine)),
  ].sort();
  // Only declared reviewers contribute to coverage. Other actor-owned
  // attestations remain visible in attestedAtHead for diagnostics.
  const nonAuthorAttested = roster.reviewers.filter((engine) =>
    attestedEngines.includes(engine),
  );
  const missingReviewers = roster.reviewers.filter(
    (engine) => !attestedEngines.includes(engine),
  );

  const authorAttested =
    roster.author !== null && attestedEngines.includes(roster.author);
  // A v1 roster names no commit, so it is stale in the only sense that matters:
  // it says nothing about which code it was declared over.
  const rosterStale = roster.present && roster.head !== params.head;
  const soloDeclared = roster.present && roster.reviewers.length === 0;
  // Solo review with a recorded reason is a legitimate outcome. What has to
  // hold before a reader repeats that claim is that the record is one the
  // reader can stand behind: the v2 grammar puts the declaration inside its own
  // digest, and `head=` binds it to the code it was declared over.
  const soloAcknowledged = soloDeclared && roster.version === 2 && !rosterStale;
  const report: CoverageResult = {
    head: params.head,
    rosterPresent: roster.present,
    rosterVersion: roster.version,
    rosterHead: roster.head,
    rosterStale,
    rosterChain: roster.chain,
    author: roster.author,
    reviewers: [...roster.reviewers],
    attestedAtHead: attestedEngines,
    nonAuthorAttested,
    missingReviewers,
    authorAttested,
    tier: coverageTier(nonAuthorAttested.length),
    soloDeclared,
    soloAcknowledged,
    roundComplete:
      roster.present &&
      missingReviewers.length === 0 &&
      (!soloDeclared || (soloAcknowledged && authorAttested)),
    verified: true,
  };
  verifyHead(params.repo, params.pr, params.head);
  return report;
}

/**
 * Compute coverage and refuse a ledger that would assert something untrue.
 *
 * Every refusal here is about the record, never about the amount of review. A
 * declared solo relay passes; what does not pass is a ledger reporting an
 * acknowledged solo relay on the strength of a declaration that was editable in
 * place, or one made over different code. Each refusal clears in one deliberate
 * step — re-post the roster at the current head — and that re-posting is an
 * ordinary operation, available at any point in a pull request's life.
 *
 * This is not a merge gate. It has no authority over whether a change ships,
 * and must never be wired into branch protection or a required check.
 */
export function verifyCoverage(params: CoverageParams): CoverageResult {
  const report = coverage(params);
  if (!report.rosterPresent) {
    fail(
      'no local-review roster is declared on this pull request; run post-roster before claiming coverage',
    );
  }
  if (report.missingReviewers.length > 0) {
    fail(
      `declared reviewers have not attested this head: ${report.missingReviewers.join(', ')}`,
    );
  }
  if (report.soloDeclared && report.rosterVersion !== 2) {
    fail(
      'this solo relay is declared in the roster:v1 grammar, whose declaration sits outside its own hash; ' +
        're-post it with post-roster to record the same choice as roster:v2 evidence',
    );
  }
  if (report.soloDeclared && report.rosterStale) {
    fail(
      `this solo relay was declared at ${report.rosterHead ?? '<no head>'}, not at ${report.head}; ` +
        're-post it with post-roster --head ' +
        `${report.head} to declare the same choice over the code that is here now`,
    );
  }
  // Solo review is review without a second engine, not the absence of review.
  // Without this the report passes on a pull request carrying no attestation at
  // all, because a solo roster has no reviewer that can be missing.
  if (report.soloAcknowledged && !report.authorAttested) {
    fail(
      'a declared solo relay still requires the author engine to attest this head',
    );
  }
  return report;
}
