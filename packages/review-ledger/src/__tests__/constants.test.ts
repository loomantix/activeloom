import { describe, expect, it } from 'vitest';
import {
  DISPOSITION_V1_RE,
  DISPOSITION_V3_RE,
  FINDING_V1_RE,
  FINDING_V3_RE,
  HUNK_WITH_LEFT_RE,
  PROTOCOL_VERSION,
  PSEUDO_V3_RE,
  SHA_RE,
  SUPPORTED_ENGINES,
  SUPPORTED_SEVERITIES,
  TOKEN_RE,
} from '../constants.js';

describe('constants and protocol regexes', () => {
  it('has protocol version 3', () => {
    expect(PROTOCOL_VERSION).toBe(3);
  });

  it('supports codex, claude, gemini, antigravity', () => {
    expect(SUPPORTED_ENGINES).toEqual([
      'codex',
      'claude',
      'gemini',
      'antigravity',
    ]);
  });

  it('supports severities', () => {
    expect(SUPPORTED_SEVERITIES).toEqual(['blocking', 'major', 'minor', 'nit']);
  });

  it('validates commit SHA-1 regex', () => {
    expect(SHA_RE.test('0123456789abcdef0123456789abcdef01234567')).toBe(true);
    expect(SHA_RE.test('0123456789ABCDEF0123456789ABCDEF01234567')).toBe(false);
    expect(SHA_RE.test('short')).toBe(false);
  });

  it('validates protocol token regex', () => {
    expect(TOKEN_RE.test('valid-token_123.abc:def/ghi')).toBe(true);
    expect(TOKEN_RE.test('has space')).toBe(false);
    expect(TOKEN_RE.test('has$special')).toBe(false);
  });

  it('matches hunk header with left line number', () => {
    const match = HUNK_WITH_LEFT_RE.exec('@@ -10,5 +12,7 @@');
    expect(match?.groups?.['left']).toBe('10');
    expect(match?.groups?.['right']).toBe('12');
  });

  it('matches valid v3 finding marker across all engines', () => {
    for (const engine of SUPPORTED_ENGINES) {
      const marker = `<!-- local-review:v3 engine=${engine} round=1 head=0123456789abcdef0123456789abcdef01234567 fingerprint=fp1 occurrence=1 severity=blocking lens=security content-sha256=${'a'.repeat(64)} -->`;
      const match = FINDING_V3_RE.exec(marker);
      expect(match).not.toBeNull();
      expect(match?.groups?.['engine']).toBe(engine);
      expect(match?.groups?.['round']).toBe('1');
      expect(match?.groups?.['severity']).toBe('blocking');
    }
  });

  it('matches valid v3 disposition marker', () => {
    const marker = `<!-- local-review-disposition:v3 engine=antigravity round=2 head=0123456789abcdef0123456789abcdef01234567 fingerprint=fp1 occurrence=2 outcome=fixed content-sha256=${'b'.repeat(64)} -->`;
    const match = DISPOSITION_V3_RE.exec(marker);
    expect(match).not.toBeNull();
    expect(match?.groups?.['engine']).toBe('antigravity');
    expect(match?.groups?.['outcome']).toBe('fixed');
  });

  it('matches legacy v1 finding and disposition', () => {
    const f1 =
      '<!-- local-review:v1 engine=claude round=1 head=0123456789abcdef0123456789abcdef01234567 fingerprint=fp1 -->';
    expect(FINDING_V1_RE.exec(f1)).not.toBeNull();

    const d1 =
      '<!-- local-review-disposition:v1 engine=codex round=1 head=0123456789abcdef0123456789abcdef01234567 fingerprint=fp1 outcome=deferred -->';
    expect(DISPOSITION_V1_RE.exec(d1)).not.toBeNull();
  });

  it('matches historical pseudo v3 marker', () => {
    const pseudo =
      '<!-- local-review:v3 engine=claude fingerprint=fp-historical outcome=deferred -->';
    expect(PSEUDO_V3_RE.exec(pseudo)).not.toBeNull();
  });
});
