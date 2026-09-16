import { HISTORICAL_COMMENT_IDS_ENV, SHA_64_RE } from './constants.js';
import { lstatSync, renameSync } from 'node:fs';
import { fail } from './errors.js';
import { sha256Bytes } from './hash.js';
import { parseJsonOrFail } from './io.js';
import {
  readResultBytes,
  validateResultData,
  writeBlockedResult,
  writeResultFile,
} from './result.js';
import type {
  LedgerResult,
  RecoverResultParams,
  WriteResultParams,
} from './types.js';

/** Archive obsolete recovery evidence when an explicit new result succeeds. */
export function archiveResultRecovery(resultFile: string): void {
  const path = `${resultFile}.recovery.json`;
  try {
    lstatSync(path);
  } catch (error) {
    if (error instanceof Error && 'code' in error && error.code === 'ENOENT')
      return;
    throw error;
  }
  const raw = readResultBytes(path);
  renameSync(path, `${resultFile}.recovery.${sha256Bytes(raw)}.json`);
}

/** Return the digest of the original snapshot, including the launcher's env default. */
export function historicalSnapshotDigest(
  params: WriteResultParams,
): string | null {
  const path =
    params.historicalCommentIdsFile || process.env[HISTORICAL_COMMENT_IDS_ENV];
  return path ? sha256Bytes(readResultBytes(path)) : null;
}

/**
 * Preserve a completed candidate when final verification fails. The canonical
 * result remains blocked; the sidecar is local recovery evidence, never an
 * attestation. A later explicit blocked result invalidates its byte binding.
 */
export function saveResultRecovery(
  params: WriteResultParams,
  candidate: LedgerResult,
  actor: string,
  historicalSha256: string | null,
): void {
  if (historicalSnapshotDigest(params) !== historicalSha256) {
    fail('pre-pass comment snapshot changed during result derivation');
  }
  writeBlockedResult({
    ...params,
    blocker:
      'Completed review requires result finalization recovery; preserve the result and its recovery sidecar.',
  });
  writeResultFile(`${params.resultFile}.recovery.json`, {
    version: 1,
    repo: params.repo,
    pr: params.pr,
    actor,
    historicalSha256,
    candidate,
    blockedResult: Buffer.from(readResultBytes(params.resultFile)).toString(
      'utf8',
    ),
  });
}

/**
 * Read a digest-pinned completed candidate bound to this exact blocked result.
 * The caller must still reverify all live Git and ledger evidence before use.
 */
export function readResultRecovery(
  params: RecoverResultParams,
  actor: string,
): LedgerResult {
  const raw = readResultBytes(`${params.resultFile}.recovery.json`);
  if (
    !SHA_64_RE.test(params.expectedRecoverySha256) ||
    sha256Bytes(raw) !== params.expectedRecoverySha256
  ) {
    fail('result recovery digest mismatch');
  }
  const receipt = parseJsonOrFail<unknown>(
    Buffer.from(raw).toString('utf8'),
    'result recovery must contain valid JSON',
  );
  if (
    typeof receipt !== 'object' ||
    receipt === null ||
    Array.isArray(receipt)
  ) {
    fail('result recovery must be an object');
  }
  const value = receipt as Record<string, unknown>;
  const keys = [
    'version',
    'repo',
    'pr',
    'actor',
    'historicalSha256',
    'candidate',
    'blockedResult',
  ];
  if (
    Object.keys(value).length !== keys.length ||
    !keys.every((key) => Object.hasOwn(value, key)) ||
    value['version'] !== 1 ||
    value['repo'] !== params.repo ||
    value['pr'] !== params.pr ||
    value['actor'] !== actor ||
    value['historicalSha256'] !== historicalSnapshotDigest(params) ||
    typeof value['blockedResult'] !== 'string'
  ) {
    fail('result recovery identity or snapshot mismatch');
  }
  const blocked = validateResultData(params, value['blockedResult']);
  if (blocked.status !== 'blocked')
    fail('result recovery must preserve a blocked result');
  const candidate = validateResultData(
    params,
    JSON.stringify(value['candidate']),
  );
  if (candidate.status !== 'clean' && candidate.status !== 'changed') {
    fail('result recovery requires a completed candidate');
  }
  const current = Buffer.from(readResultBytes(params.resultFile)).toString(
    'utf8',
  );
  // Exact candidate replay supports a crash after the atomic result write but
  // before the controller records completion. Do not adopt a different result.
  const canonicalCandidate =
    JSON.stringify(candidate, Object.keys(candidate).sort()) + '\n';
  if (current !== value['blockedResult'] && current !== canonicalCandidate) {
    fail('result recovery no longer matches the saved result');
  }
  return candidate;
}
