import { describe, expect, it } from 'vitest';
import {
  buildDispositionBody,
  buildFindingBody,
  matchDisposition,
  matchFinding,
  matchPseudoV3,
  validateContentString,
} from '../protocol.js';
import { sha256Text } from '../hash.js';

describe('protocol serialization and parsing', () => {
  const headSha = '0123456789abcdef0123456789abcdef01234567';

  it('validates content string preventing empty, NUL, and injected markers', () => {
    expect(() => validateContentString('')).toThrowError(
      'comment content is empty',
    );
    expect(() => validateContentString('   \n  ')).toThrowError(
      'comment content is empty',
    );
    expect(() => validateContentString('test\x00data')).toThrowError(
      'comment content contains NUL',
    );
    expect(() =>
      validateContentString('test <!-- local-review:v3 ...'),
    ).toThrowError('comment content must not contain local-review markers');
    expect(validateContentString('Valid review prose')).toBe(
      'Valid review prose',
    );
  });

  it('builds and parses v3 finding body with matching content sha', () => {
    const content = 'This is a high severity bug in auth validation.';
    const { marker, body } = buildFindingBody({
      engine: 'gemini',
      round: 1,
      head: headSha,
      fingerprint: 'auth-bug-01',
      occurrence: 1,
      severity: 'blocking',
      lens: 'security-reviewer',
      content,
    });

    expect(marker).toContain('engine=gemini');
    expect(marker).toContain('content-sha256=' + sha256Text(content));
    expect(body).toBe(`${marker}\n${content}`);

    const match = matchFinding(body);
    expect(match).not.toBeNull();
    expect(match?.engine).toBe('gemini');
    expect(match?.round).toBe(1);
    expect(match?.head).toBe(headSha);
    expect(match?.fingerprint).toBe('auth-bug-01');
    expect(match?.occurrence).toBe(1);
    expect(match?.severity).toBe('blocking');
    expect(match?.lens).toBe('security-reviewer');
  });

  it('fails if content hash is tampered', () => {
    const content = 'Original content';
    const { marker } = buildFindingBody({
      engine: 'codex',
      round: 1,
      head: headSha,
      fingerprint: 'fp1',
      occurrence: 1,
      severity: 'major',
      lens: 'code-reviewer',
      content,
    });
    const tampered = `${marker}\nTampered content`;
    expect(() => matchFinding(tampered)).toThrowError(/invalid content hash/);
  });

  it('builds and parses v3 disposition body', () => {
    const content = 'Fixed in commit 1234567.';
    const { marker, body } = buildDispositionBody({
      engine: 'claude',
      round: 1,
      head: headSha,
      fingerprint: 'fp1',
      occurrence: 1,
      outcome: 'fixed',
      content,
    });

    expect(marker).toContain('outcome=fixed');
    const match = matchDisposition(body);
    expect(match).not.toBeNull();
    expect(match?.outcome).toBe('fixed');
  });

  it('rejects invalid runtime marker identity before serialization', () => {
    const finding = {
      engine: 'codex',
      round: 1,
      head: headSha,
      fingerprint: 'fp1',
      occurrence: 1,
      severity: 'major',
      lens: 'code-reviewer',
      content: 'Finding content',
    } as const;
    const disposition = {
      engine: 'codex',
      round: 1,
      head: headSha,
      fingerprint: 'fp1',
      occurrence: 1,
      outcome: 'fixed',
      content: 'Disposition content',
    } as const;

    expect(() => buildFindingBody({ ...finding, round: 0 })).toThrowError(
      'round must be a positive integer',
    );
    expect(() =>
      buildFindingBody({ ...finding, occurrence: Number.MAX_SAFE_INTEGER + 1 }),
    ).toThrowError('occurrence must be a positive integer');
    expect(() => buildFindingBody({ ...finding, head: 'bad' })).toThrowError(
      '--head must be a full 40-character lowercase commit SHA',
    );
    expect(() =>
      buildFindingBody({ ...finding, engine: 'unknown' as 'codex' }),
    ).toThrowError('engine must be one of');
    expect(() =>
      buildFindingBody({ ...finding, severity: 'unknown' as 'major' }),
    ).toThrowError('severity must be one of');
    expect(() =>
      buildDispositionBody({ ...disposition, outcome: 'unknown' as 'fixed' }),
    ).toThrowError('outcome must be one of');
  });

  it('matches pseudo v3 historical comments', () => {
    const historical =
      'Settled review comment\n<!-- local-review:v3 engine=claude fingerprint=fp1 outcome=deferred -->';
    const pseudo = matchPseudoV3(historical);
    expect(pseudo).not.toBeNull();
    expect(pseudo?.fingerprint).toBe('fp1');
    expect(pseudo?.outcome).toBe('deferred');
  });
});
