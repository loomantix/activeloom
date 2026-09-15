import { describe, expect, it } from 'vitest';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { existsSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { sha256Bytes } from '../hash.js';
import {
  readResult,
  validateResult,
  validateResultData,
  writeBlockedResult,
  writeResultFile,
} from '../result.js';

describe('review result data validation, reading, and writing', () => {
  const baseSha = '1111111111111111111111111111111111111111';
  const beforeSha = '2222222222222222222222222222222222222222';
  const afterSha = '3333333333333333333333333333333333333333';

  it('validates clean review result', () => {
    const validClean = {
      version: 3,
      status: 'clean',
      engine: 'gemini',
      round: 1,
      baseSha,
      beforeSha,
      afterSha: beforeSha,
      classification: null,
      findingFingerprints: [],
      finalLaneComplete: true,
    };
    const res = validateResultData(
      {
        engine: 'gemini',
        round: 1,
        base: baseSha,
        before: beforeSha,
        head: beforeSha,
      },
      Buffer.from(JSON.stringify(validClean)),
    );
    expect(res.status).toBe('clean');
  });

  it('validates changed review result with classification and fingerprints', () => {
    const validChanged = {
      version: 3,
      status: 'changed',
      engine: 'antigravity',
      round: 1,
      baseSha,
      beforeSha,
      afterSha,
      classification: 'minor',
      findingFingerprints: ['fp-1', 'fp-2'],
      finalLaneComplete: true,
    };
    const res = validateResultData(
      {
        engine: 'antigravity',
        round: 1,
        base: baseSha,
        before: beforeSha,
        head: afterSha,
      },
      Buffer.from(JSON.stringify(validChanged)),
    );
    expect(res.status).toBe('changed');
    expect(res.findingFingerprints).toEqual(['fp-1', 'fp-2']);
  });

  it('requires material classification on round 3+ changed result', () => {
    const minorRound3 = {
      version: 3,
      status: 'changed',
      engine: 'codex',
      round: 3,
      baseSha,
      beforeSha,
      afterSha,
      classification: 'minor',
      findingFingerprints: ['fp-1'],
      finalLaneComplete: true,
    };
    expect(() =>
      validateResultData(
        {
          engine: 'codex',
          round: 3,
          base: baseSha,
          before: beforeSha,
          head: afterSha,
        },
        Buffer.from(JSON.stringify(minorRound3)),
      ),
    ).toThrowError(/changed review result conflicts with the observed pass/);
  });

  it('validates and writes blocked review result', () => {
    const tempFile = join(tmpdir(), `test-blocked-${Date.now()}.json`);
    try {
      const res = writeBlockedResult({
        engine: 'claude',
        round: 1,
        base: baseSha,
        before: beforeSha,
        head: afterSha,
        resultFile: tempFile,
        blocker: 'Integration test environment unavailable',
      });
      expect(res.status).toBe('blocked');
      expect(res.finalLaneComplete).toBe(false);
      expect(res.blocker).toBe('Integration test environment unavailable');
      expect(existsSync(tempFile)).toBe(true);
      expect(res.resultSha256).toBe(sha256Bytes(readFileSync(tempFile)));

      const readBack = readResult(tempFile);
      expect(readBack.status).toBe('blocked');
      expect(readBack.blocker).toBe('Integration test environment unavailable');
    } finally {
      if (existsSync(tempFile)) unlinkSync(tempFile);
    }
  });

  it('rejects malformed result files when reading', () => {
    const tempFile = join(tmpdir(), `test-invalid-${Date.now()}.json`);
    try {
      writeFileSync(tempFile, JSON.stringify({ status: 'clean' }));
      expect(() => readResult(tempFile)).toThrowError(/identity fields/);
    } finally {
      if (existsSync(tempFile)) unlinkSync(tempFile);
    }
  });

  it('writes and reads result file atomically with validateResult', () => {
    const tempFile = join(tmpdir(), `test-result-${Date.now()}.json`);
    try {
      const data = {
        version: 3,
        status: 'clean',
        engine: 'gemini',
        round: 2,
        baseSha,
        beforeSha,
        afterSha: beforeSha,
        classification: null,
        findingFingerprints: [],
        finalLaneComplete: true,
      };
      writeResultFile(tempFile, data);

      const validated = validateResult({
        engine: 'gemini',
        round: 2,
        base: baseSha,
        before: beforeSha,
        head: beforeSha,
        resultFile: tempFile,
      });
      expect(validated.verified).toBe(true);
      expect(validated.resultSha256).toBeDefined();
    } finally {
      if (existsSync(tempFile)) unlinkSync(tempFile);
    }
  });
});
