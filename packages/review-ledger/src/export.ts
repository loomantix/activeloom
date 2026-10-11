/**
 * Pull-based export of telemetry records as JSONL.
 *
 * Read-only: every GitHub call here is a GET. Records are read from wherever a
 * source finds them, filtered by comment author, deduplicated on
 * `idempotencyKey` across all sources, and written one per line followed by a
 * trailer that says whether the export is complete.
 */
import { isCanonicalUtcTimestamp, REPO_RE } from './constants.js';
import { errorMessage, fail } from './errors.js';
import { flattenPages, getAllIssueComments, jsonOutput } from './github.js';
import {
  isTelemetryComment,
  isTelemetryReplay,
  matchTelemetry,
} from './telemetry.js';
import type { TelemetryRecord } from './types.js';

export const EXPORT_LINE_SCHEMA = 'activeloom.review-metrics.export/v1';
export const EXPORT_TRAILER_SCHEMA =
  'activeloom.review-metrics.export-trailer/v1';

/** Author associations whose comments may carry a record. */
export const EXPORT_AUTHOR_ASSOCIATIONS: readonly string[] = [
  'OWNER',
  'MEMBER',
  'COLLABORATOR',
];

/** Upper bound on pull requests named by number in one export. */
export const EXPORT_MAX_PULL_REQUESTS = 1000;

// An underscore appears in managed-user logins (`handle_shortcode`).
const LOGIN_RE = /^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?(?:\[bot\])?$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Where a record was read from. `pr-comment` is a legacy record. */
export type ExportSourceKind = 'pr-comment' | 'store-issue';

/** One issue's comments as a source read them. */
export interface ExportSourceRead {
  kind: ExportSourceKind;
  issue: number;
  /** The comment count GitHub reports for the issue; null when unreadable. */
  commentsReported: number | null;
  /** Every comment on the issue; null when the listing could not be read. */
  comments: Array<Record<string, unknown>> | null;
}

/** A place records live. Each source reads zero or more issues. */
export type ExportSource = (
  warn: (message: string) => void,
) => ExportSourceRead[];

/** Pull requests to read legacy records from. */
export type PullRequestSelection =
  | { numbers: readonly number[] }
  | { since?: string | undefined; until?: string | undefined };

export interface ExportLine {
  schema: typeof EXPORT_LINE_SCHEMA;
  source: {
    kind: ExportSourceKind;
    repo: string;
    issue: number;
    commentId: number;
    authorAssociation: string;
    createdAt: string;
    updatedAt: string;
  };
  record: TelemetryRecord;
}

export interface ExportTrailer {
  schema: typeof EXPORT_TRAILER_SCHEMA;
  sources: Array<{
    kind: ExportSourceKind;
    issue: number;
    commentsReported: number | null;
    commentsRead: number | null;
  }>;
  records: number;
  duplicatesCollapsed: number;
  conflicts: number;
  rejectedAuthor: number;
  malformed: number;
  complete: boolean;
}

/** Parse `372-395,401` into ascending, distinct pull request numbers. */
export function parsePullRequestNumbers(spec: string): number[] {
  const numbers = new Set<number>();
  for (const part of spec.split(',')) {
    const match = part.match(/^([1-9]\d*)(?:-([1-9]\d*))?$/);
    if (!match) {
      fail('--prs must be a comma-separated list of numbers and A-B ranges');
    }
    const first = Number(match[1]);
    const last = match[2] === undefined ? first : Number(match[2]);
    if (!Number.isSafeInteger(last) || last < first) {
      fail('--prs ranges must run from a lower number to a higher one');
    }
    if (numbers.size + (last - first + 1) > EXPORT_MAX_PULL_REQUESTS) {
      fail(
        `--prs names more than ${EXPORT_MAX_PULL_REQUESTS} pull requests; export in smaller ranges`,
      );
    }
    for (let number = first; number <= last; number++) {
      numbers.add(number);
    }
  }
  return [...numbers].sort((left, right) => left - right);
}

/**
 * Parse a date or UTC timestamp bound. A bare date covers its whole UTC day:
 * the start for `--since`, the end for `--until`.
 */
export function parseExportBound(
  value: string,
  name: '--since' | '--until',
): string {
  const timestamp = DATE_RE.test(value)
    ? `${value}T${name === '--since' ? '00:00:00' : '23:59:59'}Z`
    : value;
  if (!isCanonicalUtcTimestamp(timestamp)) {
    fail(`${name} must be YYYY-MM-DD or an RFC 3339 UTC timestamp`);
  }
  return timestamp;
}

/** Parse a comma-separated login list for the author filter. */
export function parseAllowedLogins(spec: string): string[] {
  const logins = spec.split(',');
  if (logins.some((login) => !LOGIN_RE.test(login))) {
    fail('--allowed-logins must be a comma-separated list of GitHub logins');
  }
  return logins;
}

function reportedComments(issue: Record<string, unknown>): number {
  const count = issue['comments'];
  if (typeof count !== 'number' || !Number.isSafeInteger(count) || count < 0) {
    fail('GitHub issue response has an unexpected shape');
  }
  return count;
}

/** Legacy source: records posted as comments on the pull request reviewed. */
export function prCommentSource(target: {
  repo: string;
  selection: PullRequestSelection;
}): ExportSource {
  const { repo, selection } = target;
  return (warn) => {
    const reads: ExportSourceRead[] = [];
    const read = (issue: number, commentsReported: number): void => {
      let comments: ExportSourceRead['comments'] = null;
      try {
        comments = getAllIssueComments(repo, issue);
      } catch (error) {
        warn(`could not read comments on #${issue}: ${errorMessage(error)}`);
      }
      reads.push({ kind: 'pr-comment', issue, commentsReported, comments });
    };

    if ('numbers' in selection) {
      for (const number of selection.numbers) {
        let issue: Record<string, unknown>;
        try {
          issue = jsonOutput<Record<string, unknown>>([
            'api',
            `repos/${repo}/issues/${number}`,
          ]);
        } catch (error) {
          warn(`could not read #${number}: ${errorMessage(error)}`);
          reads.push({
            kind: 'pr-comment',
            issue: number,
            commentsReported: null,
            comments: null,
          });
          continue;
        }
        if (typeof issue !== 'object' || issue === null) {
          fail('GitHub issue response has an unexpected shape');
        }
        if (issue['pull_request'] == null) {
          warn(`skipped #${number}: not a pull request`);
          continue;
        }
        read(number, reportedComments(issue));
      }
      return reads;
    }

    // `since` filters on last update, so it reaches every pull request that
    // could have received a record on or after that instant.
    const query =
      `repos/${repo}/issues?state=all&sort=created&direction=asc&per_page=100` +
      (selection.since === undefined ? '' : `&since=${selection.since}`);
    const issues = flattenPages<Record<string, unknown>>(
      jsonOutput<unknown[]>(['api', '--paginate', '--slurp', query]),
      'issues',
    );
    for (const issue of issues) {
      const number = issue['number'];
      const createdAt = issue['created_at'];
      if (
        typeof number !== 'number' ||
        !Number.isSafeInteger(number) ||
        typeof createdAt !== 'string'
      ) {
        fail('GitHub issues item has an unexpected shape');
      }
      if (
        issue['pull_request'] == null ||
        (selection.until !== undefined && createdAt > selection.until)
      ) {
        continue;
      }
      read(number, reportedComments(issue));
    }
    return reads;
  };
}

function exportLineFrom(
  repo: string,
  read: ExportSourceRead,
  row: Record<string, unknown>,
  record: TelemetryRecord,
): ExportLine {
  const commentId = row['id'];
  const createdAt = row['created_at'];
  const updatedAt = row['updated_at'];
  if (
    typeof commentId !== 'number' ||
    !Number.isSafeInteger(commentId) ||
    typeof createdAt !== 'string' ||
    typeof updatedAt !== 'string'
  ) {
    fail('GitHub comment has an unexpected shape');
  }
  return {
    schema: EXPORT_LINE_SCHEMA,
    source: {
      kind: read.kind,
      repo,
      issue: read.issue,
      commentId,
      authorAssociation: String(row['author_association']),
      createdAt,
      updatedAt,
    },
    record,
  };
}

/**
 * Read every source, write one line per accepted record and a trailer, and
 * return the trailer.
 *
 * Every comment carrying a telemetry marker lands in exactly one trailer
 * counter: `records`, `duplicatesCollapsed`, `conflicts`, `rejectedAuthor`, or
 * `malformed`.
 */
export function exportTelemetry(params: {
  repo: string;
  sources: readonly ExportSource[];
  /** When given, a comment's author must also be one of these logins. */
  allowedLogins?: readonly string[] | undefined;
  write: (line: string) => void;
  warn: (message: string) => void;
}): ExportTrailer {
  const { repo, write, warn } = params;
  if (!REPO_RE.test(repo)) {
    fail('export --repo must be owner/name');
  }
  const allowedLogins =
    params.allowedLogins === undefined
      ? null
      : new Set(params.allowedLogins.map((login) => login.toLowerCase()));

  const trailer: ExportTrailer = {
    schema: EXPORT_TRAILER_SCHEMA,
    sources: [],
    records: 0,
    duplicatesCollapsed: 0,
    conflicts: 0,
    rejectedAuthor: 0,
    malformed: 0,
    complete: true,
  };
  const candidates: ExportLine[] = [];

  for (const source of params.sources) {
    for (const read of source(warn)) {
      const commentsRead = read.comments?.length ?? null;
      trailer.sources.push({
        kind: read.kind,
        issue: read.issue,
        commentsReported: read.commentsReported,
        commentsRead,
      });
      if (commentsRead === null || commentsRead !== read.commentsReported) {
        trailer.complete = false;
      }
      for (const row of read.comments ?? []) {
        const body = String(row['body'] ?? '');
        if (!isTelemetryComment(body)) {
          continue;
        }
        // Author first: a comment from outside the allowlist is never parsed.
        const login = (row['user'] as { login?: unknown } | null | undefined)
          ?.login;
        if (
          !EXPORT_AUTHOR_ASSOCIATIONS.includes(
            String(row['author_association']),
          ) ||
          (allowedLogins !== null &&
            (typeof login !== 'string' ||
              !allowedLogins.has(login.toLowerCase())))
        ) {
          trailer.rejectedAuthor += 1;
          continue;
        }
        let record: TelemetryRecord | null = null;
        let reason = 'the marker carries no record';
        try {
          record = matchTelemetry(body);
        } catch (error) {
          reason = errorMessage(error);
        }
        // A legacy record describes the pull request it was posted on. A
        // store issue holds records for many, so its number is not compared.
        if (
          record !== null &&
          read.kind === 'pr-comment' &&
          record.pr !== read.issue
        ) {
          reason = `the record names pull request #${record.pr}`;
          record = null;
        }
        if (record === null) {
          trailer.malformed += 1;
          warn(
            `malformed record in comment ${String(row['id'])} on ` +
              `#${read.issue}: ${reason}`,
          );
          continue;
        }
        candidates.push(exportLineFrom(repo, read, row, record));
      }
    }
  }

  // Earliest first, so the record kept for a key is the first one posted.
  candidates.sort(
    (left, right) =>
      left.source.createdAt.localeCompare(right.source.createdAt) ||
      left.source.commentId - right.source.commentId,
  );
  const kept = new Map<string, ExportLine>();
  for (const line of candidates) {
    const key = line.record.idempotencyKey;
    const earliest = kept.get(key);
    if (earliest === undefined) {
      kept.set(key, line);
    } else if (isTelemetryReplay(earliest.record, line.record)) {
      trailer.duplicatesCollapsed += 1;
    } else {
      trailer.conflicts += 1;
      warn(
        `conflicting records for idempotency key ${key}: kept comment ` +
          `${earliest.source.commentId} on #${earliest.source.issue}, ` +
          `dropped comment ${line.source.commentId} on #${line.source.issue}`,
      );
    }
  }

  for (const line of kept.values()) {
    write(JSON.stringify(line));
  }
  trailer.records = kept.size;
  write(JSON.stringify(trailer));
  return trailer;
}
