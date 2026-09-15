import { createHash } from 'node:crypto';
import { beforeEach, describe, expect, it } from 'vitest';
import {
  attestationsAtHead,
  buildRosterBody,
  coverage,
  coverageTier,
  matchRoster,
  parseReviewers,
  postRoster,
  readRoster,
  resolveRoster,
  verifyCoverage,
} from '../index.js';
import { resetGitHubRunner, setGitHubRunner } from '../github.js';
import type { GitHubRunner, SupportedEngine } from '../types.js';

const HEAD = '1111111111111111111111111111111111111111';
const OLD_HEAD = '3333333333333333333333333333333333333333';
const BASE = '2222222222222222222222222222222222222222';
const DIGEST = 'a'.repeat(64);

class MockRunner implements GitHubRunner {
  public actor = 'test-actor';
  public issueComments: Array<Record<string, unknown>> = [];
  public commentIdSeq = 500;
  public prHead = HEAD;
  public concurrentRosterBody: string | null = null;
  public moveHeadAfterCommentRead = false;

  runGh(args: string[], payload?: unknown): string {
    const cmd = args.join(' ');
    if (cmd.startsWith('api user')) {
      return JSON.stringify({ login: this.actor });
    }
    if (cmd.includes('pr view')) {
      return this.prHead;
    }
    if (cmd.includes('/issues/') && cmd.includes('/comments?per_page=100')) {
      const response = JSON.stringify([this.issueComments]);
      if (this.moveHeadAfterCommentRead) {
        this.prHead = OLD_HEAD;
      }
      return response;
    }
    if (cmd.includes('-X DELETE') && cmd.includes('/issues/comments/')) {
      const id = parseInt(args.at(-1)!.split('/').pop()!, 10);
      this.issueComments = this.issueComments.filter((c) => c['id'] !== id);
      return '';
    }
    if (cmd.includes('/issues/comments/')) {
      const id = parseInt(args[1]!.split('/').pop()!, 10);
      const row = this.issueComments.find((c) => c['id'] === id);
      return JSON.stringify(
        row ?? { id, user: { login: this.actor }, body: '' },
      );
    }
    if (cmd.includes('-X POST') && cmd.includes('/issues/')) {
      if (this.concurrentRosterBody !== null) {
        this.issueComments.push({
          id: this.commentIdSeq++,
          user: { login: this.actor },
          body: this.concurrentRosterBody,
        });
        this.concurrentRosterBody = null;
      }
      const id = this.commentIdSeq++;
      const body = (payload as { body?: string })?.body ?? '';
      this.issueComments.push({ id, user: { login: this.actor }, body });
      return JSON.stringify({ id });
    }
    throw new Error(`unexpected gh call: ${cmd}`);
  }

  runGit(): string {
    return '';
  }
}

let runner: MockRunner;

function passMarker(engine: string, round: number, head: string): string {
  return `<!-- local-review-pass:v3 engine=${engine} round=${round} base=${BASE} head=${head} result-sha256=${DIGEST} -->\nNo new material findings.`;
}

function completeMarker(engine: string, round: number, head: string): string {
  return (
    `<!-- local-review-complete:v3 engine=${engine} round=${round} base=${BASE} before=${OLD_HEAD} ` +
    `head=${head} classification=minor fingerprints=src/a.ts:leak result-sha256=${DIGEST} -->\nFixed.`
  );
}

function addComment(body: string): number {
  const id = runner.commentIdSeq++;
  runner.issueComments.push({ id, user: { login: runner.actor }, body });
  return id;
}

/** Post a v1 roster directly; the builder only writes v2. */
function legacyRosterBody(
  author: SupportedEngine,
  reviewers: string,
  content: string,
): string {
  // Digest over the reason prose alone — the v1 grammar this package still reads.
  const sha = createHash('sha256').update(content, 'utf8').digest('hex');
  return (
    `<!-- local-review-roster:v1 author=${author} reviewers=${reviewers} ` +
    `content-sha256=${sha} -->\n${content}`
  );
}

function declareRoster(
  author: SupportedEngine,
  reviewers: readonly SupportedEngine[],
  options?: { head?: string; supersedes?: number | null; content?: string },
): number {
  const { body } = buildRosterBody({
    author,
    reviewers,
    head: options?.head ?? HEAD,
    supersedes: options?.supersedes ?? null,
    content: options?.content ?? 'Roster for this relay.',
  });
  return addComment(body);
}

beforeEach(() => {
  resetGitHubRunner();
  runner = new MockRunner();
  setGitHubRunner(runner);
});

describe('parseReviewers', () => {
  it('reads none as a deliberate empty roster', () => {
    expect(parseReviewers('none', 'claude')).toEqual([]);
  });

  it('reads a comma-separated list', () => {
    expect(parseReviewers('codex,gemini', 'claude')).toEqual([
      'codex',
      'gemini',
    ]);
  });

  it('rejects the author appearing as its own reviewer', () => {
    expect(() => parseReviewers('codex,claude', 'claude')).toThrow(
      /author engine cannot also be listed/,
    );
  });

  it('rejects duplicates and unsupported engines', () => {
    expect(() => parseReviewers('codex,codex', 'claude')).toThrow(/distinct/);
    expect(() => parseReviewers('copilot', 'claude')).toThrow(/must be one of/);
  });

  it('rejects more than two reviewers', () => {
    expect(() => parseReviewers('codex,gemini,antigravity', 'claude')).toThrow(
      /at most two reviewers/,
    );
  });
});

describe('buildRosterBody', () => {
  it('binds the declaration to a commit and to its predecessor', () => {
    const { marker } = buildRosterBody({
      author: 'claude',
      reviewers: ['codex'],
      head: HEAD,
      supersedes: 501,
      content: 'Codex reviews this one.',
    });
    expect(marker).toContain(`author=claude reviewers=codex head=${HEAD}`);
    expect(marker).toContain('supersedes=501');
    expect(marker).toMatch(/declaration-sha256=[0-9a-f]{64} -->$/);
  });

  it('refuses content that embeds a protocol marker', () => {
    const embedded = `Solo because reasons.\n${passMarker('codex', 1, HEAD)}`;
    expect(() =>
      buildRosterBody({
        author: 'claude',
        reviewers: ['codex'],
        head: HEAD,
        supersedes: null,
        content: embedded,
      }),
    ).toThrow(/must not contain local-review markers/);
  });

  it('refuses an unsupported author', () => {
    expect(() =>
      buildRosterBody({
        author: 'copilot' as never,
        reviewers: [],
        head: HEAD,
        supersedes: null,
        content: 'x',
      }),
    ).toThrow(/author must be one of/);
  });

  it('refuses a roster with more than two reviewers', () => {
    expect(() =>
      buildRosterBody({
        author: 'claude',
        reviewers: ['codex', 'gemini', 'antigravity'],
        head: HEAD,
        supersedes: null,
        content: 'Too many reviewers.',
      }),
    ).toThrow(/at most two reviewers/);
  });

  it('refuses a head that is not a full commit SHA', () => {
    expect(() =>
      buildRosterBody({
        author: 'claude',
        reviewers: [],
        head: 'HEAD',
        supersedes: null,
        content: 'Solo.',
      }),
    ).toThrow(/40-character lowercase commit SHA/);
  });

  it('refuses a non-positive supersedes id', () => {
    expect(() =>
      buildRosterBody({
        author: 'claude',
        reviewers: [],
        head: HEAD,
        supersedes: 0,
        content: 'Solo.',
      }),
    ).toThrow(/supersedes must be a positive comment id/);
  });

  it('emits none rather than an empty value for a solo relay', () => {
    const { marker } = buildRosterBody({
      author: 'claude',
      reviewers: [],
      head: HEAD,
      supersedes: null,
      content: 'Solo: prototype spike, no external consumers.',
    });
    expect(marker).toContain('author=claude reviewers=none');
    expect(marker).toContain('supersedes=none');
  });

  it('round-trips through matchRoster', () => {
    const { body } = buildRosterBody({
      author: 'gemini',
      reviewers: ['claude', 'codex'],
      head: HEAD,
      supersedes: 7,
      content: 'Full coverage requested.',
    });
    expect(matchRoster(body)).toMatchObject({
      version: 2,
      author: 'gemini',
      reviewers: ['claude', 'codex'],
      head: HEAD,
      supersedes: 7,
    });
  });

  it('refuses a roster whose recorded reason was edited after the fact', () => {
    const { marker } = buildRosterBody({
      author: 'claude',
      reviewers: [],
      head: HEAD,
      supersedes: null,
      content: 'Solo: prototype spike.',
    });
    expect(() => matchRoster(`${marker}\nSolo: reviewed by everyone.`)).toThrow(
      /invalid declaration hash/,
    );
  });
});

describe('roster:v1 forgery regressions', () => {
  // The v1 defect: content-sha256 covered only the prose, so each of these
  // in-place edits left a valid digest behind. Every one of them must now fail.
  const real = buildRosterBody({
    author: 'claude',
    reviewers: ['codex'],
    head: HEAD,
    supersedes: null,
    content: 'Codex reviews this.',
  });

  it('refuses a reviewers=none edit of a posted roster', () => {
    const forged = real.body.replace('reviewers=codex', 'reviewers=none');
    expect(() => matchRoster(forged)).toThrow(/invalid declaration hash/);
  });

  it('refuses an author edit of a posted roster', () => {
    const forged = real.body.replace('author=claude', 'author=gemini');
    expect(() => matchRoster(forged)).toThrow(/invalid declaration hash/);
  });

  it('refuses a head edit of a posted roster', () => {
    const forged = real.body.replace(`head=${HEAD}`, `head=${OLD_HEAD}`);
    expect(() => matchRoster(forged)).toThrow(/invalid declaration hash/);
  });

  it('refuses a supersedes edit of a posted roster', () => {
    const forged = real.body.replace('supersedes=none', 'supersedes=501');
    expect(() => matchRoster(forged)).toThrow(/invalid declaration hash/);
  });

  it('does not report a forged solo roster as an acknowledged solo relay', () => {
    addComment(real.body.replace('reviewers=codex', 'reviewers=none'));
    expect(() => coverage({ repo: 'o/r', pr: 1, head: HEAD })).toThrow(
      /invalid declaration hash/,
    );
  });
});

describe('matchRoster', () => {
  it('reads a v1 roster, binding it to no commit', () => {
    const body = legacyRosterBody('claude', 'codex', 'Codex reviews this.');
    expect(matchRoster(body)).toMatchObject({
      version: 1,
      author: 'claude',
      reviewers: ['codex'],
      head: null,
      supersedes: null,
    });
  });

  it('hard-stops on two roster candidates in one comment', () => {
    const first = buildRosterBody({
      author: 'claude',
      reviewers: [],
      head: HEAD,
      supersedes: null,
      content: 'Solo.',
    }).body;
    const second = legacyRosterBody('claude', 'codex', 'Codex reviews.');
    expect(() => matchRoster(`${first}\n${second}`)).toThrow(
      /more than one local-review roster marker/,
    );
  });

  it('rejects a roster marker from an unsupported version', () => {
    expect(() =>
      matchRoster('<!-- local-review-roster:v9 author=claude -->\nreason'),
    ).toThrow(/unsupported protocol version/);
  });

  it('returns null for a body carrying no roster marker', () => {
    expect(matchRoster('looks good to me')).toBeNull();
  });
});

describe('resolveRoster', () => {
  it('reports absence rather than inventing a default', () => {
    expect(readRoster({ repo: 'o/r', pr: 1 })).toMatchObject({
      present: false,
      version: null,
      author: null,
      reviewers: [],
      chain: [],
    });
  });

  it('does not share mutable arrays between absent reports', () => {
    const first = readRoster({ repo: 'o/r', pr: 1 });
    first.reviewers.push('codex');
    first.chain.push(500);

    expect(readRoster({ repo: 'o/r', pr: 1 })).toMatchObject({
      reviewers: [],
      chain: [],
    });
  });

  it('resolves the newest link of a supersession chain', () => {
    const first = declareRoster('claude', ['codex', 'gemini']);
    const second = declareRoster('claude', ['codex'], { supersedes: first });
    const report = readRoster({ repo: 'o/r', pr: 1 });
    expect(report).toMatchObject({
      version: 2,
      reviewers: ['codex'],
      commentId: second,
      supersedes: first,
    });
    expect(report.chain).toEqual([first, second]);
  });

  it('lets a v2 roster supersede a pre-existing v1 declaration', () => {
    const legacy = addComment(
      legacyRosterBody('claude', 'codex', 'Codex reviews this.'),
    );
    const replacement = declareRoster('claude', [], { supersedes: legacy });
    expect(readRoster({ repo: 'o/r', pr: 1 })).toMatchObject({
      version: 2,
      reviewers: [],
      commentId: replacement,
      chain: [legacy, replacement],
    });
  });

  it('refuses a v2 roster that ignores a v1 roster already on the pull request', () => {
    addComment(legacyRosterBody('claude', 'codex', 'Codex reviews this.'));
    declareRoster('claude', []);
    expect(() => readRoster({ repo: 'o/r', pr: 1 })).toThrow(
      /does not cover every roster/,
    );
  });

  it('supersedes a pre-existing v1 roster automatically when posting', () => {
    const legacy = addComment(
      legacyRosterBody('claude', 'codex', 'Codex reviews this.'),
    );
    const posted = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      author: 'claude',
      reviewers: ['codex'],
      content: 'Same roster, recorded as v2 evidence.',
    });
    expect(posted.supersedes).toBe(legacy);
    expect(posted.chain).toEqual([legacy, posted.comment_id]);
  });

  it('refuses a chain whose superseded roster was deleted', () => {
    declareRoster('claude', ['codex'], { supersedes: 499 });
    expect(() => readRoster({ repo: 'o/r', pr: 1 })).toThrow(
      /supersedes comment 499, which is not a roster on this pull request/,
    );
  });

  it('refuses a forked chain', () => {
    const first = declareRoster('claude', ['codex', 'gemini']);
    declareRoster('claude', ['codex'], { supersedes: first });
    declareRoster('claude', ['gemini'], { supersedes: first });
    expect(() => readRoster({ repo: 'o/r', pr: 1 })).toThrow(
      /supersession chain forks/,
    );
  });

  it('refuses two unlinked roots', () => {
    declareRoster('claude', ['codex']);
    declareRoster('claude', ['gemini']);
    expect(() => readRoster({ repo: 'o/r', pr: 1 })).toThrow(
      /more than one supersession chain/,
    );
  });

  it('refuses a link that supersedes a later declaration', () => {
    // The replacement's own comment id (800) predates the roster it claims to
    // replace (900), so the chain does not run in the order it asserts.
    const push = (id: number, body: string): void => {
      runner.issueComments.push({ id, user: { login: runner.actor }, body });
    };
    push(
      900,
      buildRosterBody({
        author: 'claude',
        reviewers: ['codex'],
        head: HEAD,
        supersedes: null,
        content: 'Roster for this relay.',
      }).body,
    );
    push(
      800,
      buildRosterBody({
        author: 'claude',
        reviewers: ['gemini'],
        head: HEAD,
        supersedes: 900,
        content: 'Roster for this relay.',
      }).body,
    );
    expect(() => readRoster({ repo: 'o/r', pr: 1 })).toThrow(
      /supersedes a later declaration/,
    );
  });

  it('rejects two conflicting v1 declarations', () => {
    addComment(legacyRosterBody('claude', 'codex', 'Codex reviews.'));
    addComment(legacyRosterBody('claude', 'gemini', 'Gemini reviews.'));
    expect(() => readRoster({ repo: 'o/r', pr: 1 })).toThrow(
      /declared more than once/,
    );
  });

  it('ignores roster-shaped comments authored by anyone but the actor', () => {
    runner.issueComments.push({
      id: 900,
      user: { login: 'someone-else' },
      body: buildRosterBody({
        author: 'claude',
        reviewers: [],
        head: HEAD,
        supersedes: null,
        content: 'Solo.',
      }).body,
    });
    expect(readRoster({ repo: 'o/r', pr: 1 }).present).toBe(false);
  });

  it('refuses a roster row carrying no comment id', () => {
    expect(() =>
      resolveRoster([
        {
          body: buildRosterBody({
            author: 'claude',
            reviewers: [],
            head: HEAD,
            supersedes: null,
            content: 'Solo.',
          }).body,
        },
      ]),
    ).toThrow(/malformed/);
  });
});

describe('postRoster', () => {
  it('declares the roster and replays an identical re-post', () => {
    const first = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      author: 'claude',
      reviewers: ['codex'],
      content: 'Codex reviews this one.',
    });
    expect(first).toMatchObject({
      replayed: false,
      reviewers: ['codex'],
      head: HEAD,
      supersedes: null,
      superseded: false,
    });

    const second = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      author: 'claude',
      reviewers: ['codex'],
      content: 'Codex reviews this one.',
    });
    expect(second.replayed).toBe(true);
    expect(second.comment_id).toBe(first.comment_id);
    expect(runner.issueComments).toHaveLength(1);
  });

  it('records a late narrow to solo as a visible, ordered replacement', () => {
    const declared = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      author: 'claude',
      reviewers: ['codex', 'gemini'],
      content: 'Two reviewers: this touches auth.',
    });

    const narrowed = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      author: 'claude',
      reviewers: [],
      content: 'Narrowed to solo: the auth change was dropped from this PR.',
    });

    expect(narrowed).toMatchObject({
      reviewers: [],
      supersedes: declared.comment_id,
      superseded: true,
      replayed: false,
    });
    expect(narrowed.chain).toEqual([declared.comment_id, narrowed.comment_id]);
    // The superseded declaration stays on the pull request.
    expect(runner.issueComments).toHaveLength(2);
    expect(readRoster({ repo: 'o/r', pr: 1 }).reviewers).toEqual([]);
  });

  it('records a widened roster the same way', () => {
    const declared = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      author: 'claude',
      reviewers: [],
      content: 'Solo: docs only.',
    });
    const widened = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      author: 'claude',
      reviewers: ['codex'],
      content: 'Source landed; codex reviews it.',
    });
    expect(widened.supersedes).toBe(declared.comment_id);
    expect(widened.chain).toHaveLength(2);
  });

  it('re-declares an unchanged roster at a new head as a new link', () => {
    const declared = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      author: 'claude',
      reviewers: [],
      content: 'Solo: internal tooling spike.',
    });
    runner.prHead = OLD_HEAD;
    const restated = postRoster({
      repo: 'o/r',
      pr: 1,
      head: OLD_HEAD,
      author: 'claude',
      reviewers: [],
      content: 'Solo: internal tooling spike.',
    });
    expect(restated.replayed).toBe(false);
    expect(restated.head).toBe(OLD_HEAD);
    expect(restated.supersedes).toBe(declared.comment_id);
  });

  it('converges concurrent identical posts onto one roster', () => {
    const declaration = {
      author: 'claude' as const,
      reviewers: ['codex'] as const,
      content: 'Codex reviews this one.',
    };
    runner.concurrentRosterBody = buildRosterBody({
      ...declaration,
      head: HEAD,
      supersedes: null,
    }).body;

    const posted = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      ...declaration,
    });

    expect(posted).toMatchObject({ comment_id: 500, replayed: true });
    expect(runner.issueComments).toHaveLength(1);
    expect(readRoster({ repo: 'o/r', pr: 1 }).commentId).toBe(500);
  });

  it('refuses a head that is not the pull request head', () => {
    runner.prHead = OLD_HEAD;
    expect(() =>
      postRoster({
        repo: 'o/r',
        pr: 1,
        head: HEAD,
        author: 'claude',
        reviewers: [],
        content: 'Solo.',
      }),
    ).toThrow(/PR head mismatch/);
  });
});

describe('attestationsAtHead', () => {
  it('counts pass and completion markers naming the exact head', () => {
    const rows = [
      { body: passMarker('codex', 1, HEAD) },
      { body: completeMarker('gemini', 2, HEAD) },
    ];
    expect(attestationsAtHead(rows, HEAD)).toEqual([
      { engine: 'codex', round: 1, status: 'clean' },
      { engine: 'gemini', round: 2, status: 'changed' },
    ]);
  });

  it('ignores an attestation naming an earlier head', () => {
    const rows = [{ body: passMarker('codex', 1, OLD_HEAD) }];
    expect(attestationsAtHead(rows, HEAD)).toEqual([]);
  });

  it('ignores unrelated comments', () => {
    expect(attestationsAtHead([{ body: 'looks good to me' }], HEAD)).toEqual(
      [],
    );
  });

  it('refuses a marker that is not the first line of the body', () => {
    const quoted = `Round 1 summary. Codex posted:\n\n${passMarker('codex', 1, HEAD)}`;
    expect(attestationsAtHead([{ body: quoted }], HEAD)).toEqual([]);
  });

  it('refuses a marker quoted inside a fenced block', () => {
    const fenced = [
      'Here is what we will post:',
      '',
      '```',
      passMarker('codex', 1, HEAD),
      '```',
    ].join('\n');
    expect(attestationsAtHead([{ body: fenced }], HEAD)).toEqual([]);
  });

  it('refuses a marker with no content after it', () => {
    const bare = passMarker('codex', 1, HEAD).split('\n')[0]!;
    expect(attestationsAtHead([{ body: bare }], HEAD)).toEqual([]);
  });

  it('reads a completion marker even when another body quoted a stale pass', () => {
    const rows = [
      { body: `context\n${passMarker('codex', 1, OLD_HEAD)}` },
      { body: completeMarker('codex', 2, HEAD) },
    ];
    expect(attestationsAtHead(rows, HEAD)).toEqual([
      { engine: 'codex', round: 2, status: 'changed' },
    ]);
  });

  it('rejects conflicting attestations with the same engine-round identity', () => {
    expect(() =>
      attestationsAtHead(
        [
          { body: passMarker('codex', 1, OLD_HEAD) },
          { body: completeMarker('codex', 1, HEAD) },
        ],
        HEAD,
      ),
    ).toThrow(/attestation identity is duplicated/);
  });
});

describe('coverageTier', () => {
  it('maps non-author reviewer counts onto the declared tiers', () => {
    expect(coverageTier(0)).toBe('solo');
    expect(coverageTier(1)).toBe('cross');
    expect(coverageTier(2)).toBe('full');
    expect(coverageTier(3)).toBe('full');
  });
});

describe('coverage', () => {
  it('does not count the author engine towards cross-model coverage', () => {
    declareRoster('claude', ['codex']);
    addComment(passMarker('claude', 1, HEAD));

    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.authorAttested).toBe(true);
    expect(report.nonAuthorAttested).toEqual([]);
    expect(report.tier).toBe('solo');
    expect(report.missingReviewers).toEqual(['codex']);
    expect(report.roundComplete).toBe(false);
  });

  it('reaches cross when one declared reviewer attests the head', () => {
    declareRoster('claude', ['codex']);
    addComment(passMarker('codex', 1, HEAD));

    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.tier).toBe('cross');
    expect(report.roundComplete).toBe(true);
    expect(report.missingReviewers).toEqual([]);
    expect(report.rosterVersion).toBe(2);
    expect(report.rosterHead).toBe(HEAD);
    expect(report.rosterStale).toBe(false);
  });

  it('reaches full with two non-author engines at the same head', () => {
    declareRoster('claude', ['codex', 'gemini']);
    addComment(passMarker('codex', 1, HEAD));
    addComment(completeMarker('gemini', 1, HEAD));

    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.tier).toBe('full');
    expect(report.roundComplete).toBe(true);
  });

  it('does not count undeclared engines towards the coverage tier', () => {
    declareRoster('claude', ['codex']);
    addComment(passMarker('codex', 1, HEAD));
    addComment(passMarker('gemini', 1, HEAD));

    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.attestedAtHead).toEqual(['codex', 'gemini']);
    expect(report.nonAuthorAttested).toEqual(['codex']);
    expect(report.tier).toBe('cross');
  });

  it('drops an engine whose attestation names a superseded head', () => {
    declareRoster('claude', ['codex', 'gemini']);
    addComment(passMarker('codex', 1, HEAD));
    addComment(passMarker('gemini', 1, OLD_HEAD));

    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.attestedAtHead).toEqual(['codex']);
    expect(report.missingReviewers).toEqual(['gemini']);
    expect(report.tier).toBe('cross');
    expect(report.roundComplete).toBe(false);
  });

  it('does not attribute non-author coverage when no roster names an author', () => {
    addComment(passMarker('claude', 1, HEAD));
    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.rosterPresent).toBe(false);
    expect(report.nonAuthorAttested).toEqual([]);
    expect(report.tier).toBe('solo');
  });

  it('ignores an attestation authored by anyone but the actor', () => {
    declareRoster('claude', ['codex']);
    runner.issueComments.push({
      id: runner.commentIdSeq++,
      user: { login: 'someone-else' },
      body: passMarker('codex', 1, HEAD),
    });
    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.attestedAtHead).toEqual([]);
    expect(report.missingReviewers).toEqual(['codex']);
  });

  it('marks a declared solo relay acknowledged and complete', () => {
    declareRoster('claude', []);
    addComment(passMarker('claude', 1, HEAD));
    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.tier).toBe('solo');
    expect(report.soloDeclared).toBe(true);
    expect(report.soloAcknowledged).toBe(true);
    expect(report.roundComplete).toBe(true);
  });

  it('does not report a zero-attestation solo relay as complete', () => {
    declareRoster('claude', []);
    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.soloAcknowledged).toBe(true);
    expect(report.authorAttested).toBe(false);
    expect(report.roundComplete).toBe(false);
  });

  it('does not let a solo roster declared at an earlier head govern this one', () => {
    declareRoster('claude', [], { head: OLD_HEAD });
    addComment(passMarker('claude', 1, HEAD));
    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.rosterStale).toBe(true);
    expect(report.rosterHead).toBe(OLD_HEAD);
    expect(report.soloDeclared).toBe(true);
    expect(report.soloAcknowledged).toBe(false);
    expect(report.roundComplete).toBe(false);
  });

  it('reads a v1 solo roster as declared but not acknowledged', () => {
    addComment(legacyRosterBody('claude', 'none', 'Solo: prototype spike.'));
    addComment(passMarker('claude', 1, HEAD));
    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.rosterVersion).toBe(1);
    expect(report.rosterHead).toBeNull();
    expect(report.soloDeclared).toBe(true);
    expect(report.soloAcknowledged).toBe(false);
    expect(report.roundComplete).toBe(false);
  });

  it('still holds a v1 roster to its declared reviewers', () => {
    addComment(legacyRosterBody('claude', 'codex', 'Codex reviews this.'));
    addComment(passMarker('codex', 1, HEAD));
    const report = coverage({ repo: 'o/r', pr: 1, head: HEAD });
    expect(report.rosterVersion).toBe(1);
    expect(report.tier).toBe('cross');
    expect(report.roundComplete).toBe(true);
  });

  it('asserts the actor before reading actor-owned evidence', () => {
    declareRoster('claude', ['codex']);
    expect(() =>
      coverage({ repo: 'o/r', pr: 1, head: HEAD, actor: 'other-actor' }),
    ).toThrow(/authenticated GitHub actor changed/);
    expect(() =>
      readRoster({ repo: 'o/r', pr: 1, actor: 'other-actor' }),
    ).toThrow(/authenticated GitHub actor changed/);
  });

  it('rechecks the exact head after reading coverage evidence', () => {
    declareRoster('claude', ['codex']);
    addComment(passMarker('codex', 1, HEAD));
    runner.moveHeadAfterCommentRead = true;
    expect(() => coverage({ repo: 'o/r', pr: 1, head: HEAD })).toThrow(
      /PR head mismatch/,
    );
  });
});

describe('verifyCoverage', () => {
  it('refuses a pull request with no declared roster', () => {
    addComment(passMarker('codex', 1, HEAD));
    expect(() => verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toThrow(
      /no local-review roster is declared/,
    );
  });

  it('refuses a relay whose declared reviewer has not attested this head', () => {
    declareRoster('claude', ['codex', 'gemini']);
    addComment(passMarker('codex', 1, HEAD));
    expect(() => verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toThrow(
      /declared reviewers have not attested this head: gemini/,
    );
  });

  it('accepts a solo relay that was declared up front and reviewed', () => {
    declareRoster('claude', []);
    addComment(passMarker('claude', 1, HEAD));
    expect(verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toMatchObject({
      tier: 'solo',
      soloAcknowledged: true,
      authorAttested: true,
    });
  });

  it('refuses a declared solo relay carrying no attestation at all', () => {
    declareRoster('claude', []);
    expect(() => verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toThrow(
      /solo relay still requires the author engine to attest/,
    );
  });

  it('refuses a solo relay declared over earlier code, naming the way out', () => {
    declareRoster('claude', [], { head: OLD_HEAD });
    addComment(passMarker('claude', 1, HEAD));
    expect(() => verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toThrow(
      new RegExp(`declared at ${OLD_HEAD}, not at ${HEAD}`),
    );
    expect(() => verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toThrow(
      /post-roster --head/,
    );
  });

  it('refuses a v1 solo roster, whose declaration sits outside its hash', () => {
    addComment(legacyRosterBody('claude', 'none', 'Solo: prototype spike.'));
    addComment(passMarker('claude', 1, HEAD));
    expect(() => verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toThrow(
      /roster:v1 grammar/,
    );
  });

  it('clears a stale solo roster in one deliberate re-declaration', () => {
    const stale = declareRoster('claude', [], { head: OLD_HEAD });
    addComment(passMarker('claude', 1, HEAD));
    expect(() => verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toThrow(
      /declared at/,
    );

    const redeclared = postRoster({
      repo: 'o/r',
      pr: 1,
      head: HEAD,
      author: 'claude',
      reviewers: [],
      content: 'Still solo: the later commits are test fixtures only.',
    });

    expect(redeclared.supersedes).toBe(stale);
    expect(verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toMatchObject({
      soloAcknowledged: true,
      roundComplete: true,
      rosterChain: [stale, redeclared.comment_id],
    });
  });

  it('accepts a complete cross-model relay', () => {
    declareRoster('gemini', ['claude']);
    addComment(passMarker('claude', 2, HEAD));
    expect(verifyCoverage({ repo: 'o/r', pr: 1, head: HEAD })).toMatchObject({
      tier: 'cross',
      roundComplete: true,
    });
  });
});
