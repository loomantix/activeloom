import { readFileSync } from 'node:fs';
import {
  DISPOSITION_V3_RE,
  FINDING_V3_RE,
  PSEUDO_V3_RE,
  SUPPORTED_ENGINES,
  SUPPORTED_OUTCOMES,
  SUPPORTED_SEVERITIES,
} from './constants.js';
import { fail, LedgerError } from './errors.js';
import { assertRegularFile } from './io.js';
import { requireSha, requireToken, sha256Text } from './hash.js';
import type {
  DispositionV3Match,
  FindingV3Match,
  PseudoV3Match,
  SupportedEngine,
  SupportedOutcome,
  SupportedSeverity,
} from './types.js';

/**
 * Read legacy v1 comment body from file or stdin.
 */
export function readLegacyBody(
  path: string,
  marker: string | readonly string[],
  bodyText?: string,
): string {
  let body: string;
  if (bodyText !== undefined) {
    body = bodyText;
  } else if (path === '-') {
    body = readFileSync(0, 'utf8');
  } else {
    body = readFileSync(path, 'utf8');
  }
  if (!body.trim()) {
    fail('comment body is empty');
  }
  const markers = typeof marker === 'string' ? [marker] : marker;
  const matchesMarker = markers.some((candidate) => body.includes(candidate));
  if (!matchesMarker) {
    fail('comment body lacks the required local-review marker');
  }
  return body.trimEnd();
}

/**
 * Validate that a content string complies with protocol rules (no NUL, not empty, no markers).
 */
export function validateContentString(content: string): string {
  if (!content.trim()) {
    fail('comment content is empty');
  }
  if (content.includes('\x00')) {
    fail('comment content contains NUL');
  }
  if (content.includes('<!-- local-review')) {
    fail('comment content must not contain local-review markers');
  }
  return content;
}

/**
 * Read v3 comment content from a regular file.
 */
export function readContent(path: string): string {
  if (path === '-') {
    fail(
      'v3 content must be a regular file; stdin and heredocs are not accepted',
    );
  }
  assertRegularFile(path, 'content file must be a regular non-symlink file');

  // Decoded strictly: readFileSync(path, 'utf8') never throws on invalid bytes,
  // it substitutes U+FFFD. The marker would then seal text nobody wrote.
  let content: string;
  try {
    content = new TextDecoder('utf-8', { fatal: true }).decode(
      readFileSync(path),
    );
  } catch (error) {
    throw new LedgerError('content file must be valid UTF-8', { cause: error });
  }

  return validateContentString(content);
}

/**
 * Resolve v3 content from an inline string or a content file.
 *
 * Returns `undefined` when neither is supplied; callers that require content
 * own the failure message for their own command.
 */
export function resolveContent(params: {
  content?: string | undefined;
  contentFile?: string | undefined;
}): string | undefined {
  if (params.content === undefined && params.contentFile) {
    return readContent(params.contentFile);
  }
  if (params.content !== undefined) {
    return validateContentString(params.content);
  }
  return undefined;
}

/**
 * Build finding marker and complete comment body.
 */
export function buildFindingBody(params: {
  engine: SupportedEngine;
  round: number;
  head: string;
  fingerprint: string;
  occurrence: number;
  severity: SupportedSeverity;
  lens: string;
  content: string;
}): { marker: string; body: string } {
  if (!SUPPORTED_ENGINES.includes(params.engine)) {
    fail('engine must be one of: codex, claude, gemini, antigravity');
  }
  if (!Number.isSafeInteger(params.round) || params.round < 1) {
    fail('round must be a positive integer');
  }
  if (!Number.isSafeInteger(params.occurrence) || params.occurrence < 1) {
    fail('occurrence must be a positive integer');
  }
  if (!SUPPORTED_SEVERITIES.includes(params.severity)) {
    fail('severity must be one of: blocking, major, minor, nit');
  }
  const marker =
    `<!-- local-review:v3 engine=${params.engine} round=${params.round} ` +
    `head=${requireSha(params.head, 'head')} fingerprint=${requireToken(params.fingerprint, 'fingerprint')} ` +
    `occurrence=${params.occurrence} severity=${params.severity} ` +
    `lens=${requireToken(params.lens, 'lens')} ` +
    `content-sha256=${sha256Text(params.content)} -->`;
  return { marker, body: `${marker}\n${params.content}` };
}

/**
 * Build disposition marker and complete comment body.
 */
export function buildDispositionBody(params: {
  engine: SupportedEngine;
  round: number;
  head: string;
  fingerprint: string;
  occurrence: number;
  outcome: SupportedOutcome;
  content: string;
}): { marker: string; body: string } {
  if (!SUPPORTED_ENGINES.includes(params.engine)) {
    fail('engine must be one of: codex, claude, gemini, antigravity');
  }
  if (!Number.isSafeInteger(params.round) || params.round < 1) {
    fail('round must be a positive integer');
  }
  if (!Number.isSafeInteger(params.occurrence) || params.occurrence < 1) {
    fail('occurrence must be a positive integer');
  }
  if (!SUPPORTED_OUTCOMES.includes(params.outcome)) {
    fail('outcome must be one of: fixed, dismissed, deferred');
  }
  const marker =
    `<!-- local-review-disposition:v3 engine=${params.engine} round=${params.round} ` +
    `head=${requireSha(params.head, 'head')} fingerprint=${requireToken(params.fingerprint, 'fingerprint')} ` +
    `occurrence=${params.occurrence} outcome=${params.outcome} ` +
    `content-sha256=${sha256Text(params.content)} -->`;
  return { marker, body: `${marker}\n${params.content}` };
}

/**
 * Match a protocol marker at the start of a body string and verify its content hash.
 *
 * The marker must be the first complete line of the body. Anything ahead of it
 * would sit outside `content-sha256`, so accepting a marker at a later offset
 * would leave part of an authenticated comment unhashed.
 */
export function matchProtocol(
  body: string,
  pattern: RegExp,
  marker: string,
): RegExpExecArray | null {
  const matched = matchMarkerLine(body, pattern, marker);
  if (matched === null) {
    return null;
  }
  if (sha256Text(matched.content) !== matched.match.groups!['content_sha']) {
    fail(`authenticated ${marker} record has an invalid content hash`);
  }
  return matched.match;
}

/**
 * Enforce the shared marker layout and return the marker with its content.
 *
 * Every authenticated record in this protocol has the same shape: the marker is
 * the first complete line, and everything after it is non-empty content. Only
 * the digest differs — a finding hashes its prose, a roster hashes its prose
 * together with the declaration the prose exists to justify. Splitting the
 * layout rules out here keeps the two digests from drifting apart on which
 * bytes they are defined over.
 */
export function matchMarkerLine(
  body: string,
  pattern: RegExp,
  marker: string,
): { match: RegExpExecArray; content: string } | null {
  if (!body.includes(marker)) {
    return null;
  }
  const match = pattern.exec(body);
  if (!match || !match.groups) {
    fail(`authenticated ${marker} record is malformed`);
  }
  const matchEnd = match.index + match[0].length;
  if (match.index !== 0 || !body.slice(matchEnd).startsWith('\n')) {
    fail(`authenticated ${marker} record is not the first complete line`);
  }
  const content = body.slice(matchEnd + 1);
  if (!content.trim()) {
    fail(`authenticated ${marker} content is empty`);
  }
  return { match, content };
}

/**
 * Legacy tools placed their single marker before or after the prose. Require
 * one standalone marker and non-empty prose without imposing v3's hashed layout.
 */
export function verifyV1Marker(
  body: string,
  match: RegExpExecArray,
  label: string,
): void {
  const matchEnd = match.index + match[0].length;
  if (
    (body.match(/<!-- local-review(?:-disposition)?:v1\b/g) ?? []).length !==
      1 ||
    (match.index > 0 && body[match.index - 1] !== '\n') ||
    (matchEnd < body.length && !body.slice(matchEnd).startsWith('\n'))
  ) {
    fail(`actor-owned v1 ${label} marker is malformed`);
  }
  if (!(body.slice(0, match.index) + body.slice(matchEnd)).trim()) {
    fail(`actor-owned v1 ${label} content is empty`);
  }
}

/**
 * Match historical pseudo v3 marker.
 */
export function matchPseudoV3(body: string): PseudoV3Match | null {
  const regex = new RegExp(PSEUDO_V3_RE.source, 'gm');
  const matches: RegExpExecArray[] = [];
  let m: RegExpExecArray | null;
  while ((m = regex.exec(body)) !== null) {
    matches.push(m);
  }
  if (matches.length === 0) {
    return null;
  }
  const markerCount = (body.match(/<!-- local-review/g) || []).length;
  const first = matches[0]!;
  if (
    matches.length !== 1 ||
    markerCount !== 1 ||
    first.index === 0 ||
    body[first.index - 1] !== '\n' ||
    first.index + first[0].length !== body.length ||
    !body.slice(0, first.index).trim()
  ) {
    fail('authenticated historical local-review:v3 record is malformed');
  }
  return {
    fingerprint: first.groups!['fingerprint']!,
    outcome: first[0].includes('outcome=deferred') ? 'deferred' : undefined,
  };
}

/**
 * Match finding v3 marker in comment body.
 */
export function matchFinding(body: string): FindingV3Match | null {
  if (matchPseudoV3(body) !== null) {
    return null;
  }
  const match = matchProtocol(body, FINDING_V3_RE, '<!-- local-review:v3');
  if (!match || !match.groups) {
    return null;
  }
  return {
    engine: match.groups['engine'] as SupportedEngine,
    round: parseInt(match.groups['round']!, 10),
    head: match.groups['head']!,
    fingerprint: match.groups['fingerprint']!,
    occurrence: parseInt(match.groups['occurrence']!, 10),
    severity: match.groups['severity'] as SupportedSeverity,
    lens: match.groups['lens']!,
    contentSha: match.groups['content_sha']!,
  };
}

/**
 * Match disposition v3 marker in comment body.
 */
export function matchDisposition(body: string): DispositionV3Match | null {
  const match = matchProtocol(
    body,
    DISPOSITION_V3_RE,
    '<!-- local-review-disposition:v3',
  );
  if (!match || !match.groups) {
    return null;
  }
  return {
    engine: match.groups['engine'] as SupportedEngine,
    round: parseInt(match.groups['round']!, 10),
    head: match.groups['head']!,
    fingerprint: match.groups['fingerprint']!,
    occurrence: parseInt(match.groups['occurrence']!, 10),
    outcome: match.groups['outcome'] as SupportedOutcome,
    contentSha: match.groups['content_sha']!,
  };
}
