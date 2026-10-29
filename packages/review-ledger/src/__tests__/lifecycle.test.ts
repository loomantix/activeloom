import { describe, expect, it } from 'vitest';
import {
  lifecycleState,
  requireLifecycleOwner,
  transitionJson,
  type OwnerTransition,
} from '../lifecycle.js';
import { sha256Text } from '../hash.js';

const repo = 'example/project';
const head = 'a'.repeat(40);
const base = 'b'.repeat(40);
const timestamp = '2026-01-01T01:00:00Z';

function comment(
  id: number,
  actor: string,
  body: string,
): Record<string, unknown> {
  return {
    id,
    user: { login: actor },
    body,
    created_at: timestamp,
    lastEditedAt: null,
  };
}

function run(
  id: number,
  actor: string,
  content = 'Explicit synthetic review authorization.',
  supersedes: number | null = null,
) {
  const digest = sha256Text(
    JSON.stringify({
      base,
      content,
      max_rounds: 2,
      start_head: head,
      supersedes,
      tier: 'lean',
    }),
  );
  return {
    ...comment(
      id,
      actor,
      `<!-- local-review-run:v1 id=${digest} tier=lean max-rounds=2 base=${base} start-head=${head} supersedes=${supersedes ?? 'none'} content-sha256=${digest} -->\n${content}`,
    ),
    digest,
  };
}

function transfer(id = 20, to = 'new-owner', source = run(10, 'old-owner')) {
  const binding: OwnerTransition = {
    version: 1,
    approval_id: 'c'.repeat(16),
    created: '2026-01-01T00:00:00Z',
    expires: '2026-01-01T04:00:00Z',
    from_actor: 'old-owner',
    to_actor: to,
    repo,
    pr: 7,
    head,
    run_comment_id: 10,
    run_id: source.digest,
  };
  const json = transitionJson(binding);
  const digest = sha256Text(json);
  return {
    ...comment(
      id,
      to,
      `<!-- local-review-transition:v1 id=${digest} -->\n${json}`,
    ),
    digest,
    binding,
  };
}

describe('PR-wide lifecycle with actor-owned evidence', () => {
  it('retains no-run and ordinary same-actor paths', () => {
    expect(lifecycleState([], repo, 7, 'old-owner').state).toBe('none');
    const first = run(10, 'old-owner');
    expect(lifecycleState([first], repo, 7, 'old-owner').state).toBe('active');
    const end = comment(
      11,
      'old-owner',
      `<!-- local-review-run-end:v1 id=${first.digest} outcome=aborted head=${head} -->`,
    );
    expect(lifecycleState([first, end], repo, 7, 'old-owner').state).toBe(
      'ended',
    );
    expect(
      lifecycleState(
        [
          first,
          end,
          run(30, 'old-owner', 'New separately authorized budget.', 10),
        ],
        repo,
        7,
        'old-owner',
      ).state,
    ).toBe('active');
  });

  it('blocks foreign active and ended history instead of reporting none', () => {
    const first = run(10, 'old-owner');
    expect(lifecycleState([first], repo, 7, 'new-owner').state).toBe(
      'foreign-active',
    );
    const end = comment(
      11,
      'old-owner',
      `<!-- local-review-run-end:v1 id=${first.digest} outcome=aborted head=${head} -->`,
    );
    const state = lifecycleState([first, end], repo, 7, 'new-owner');
    expect(state.state).toBe('foreign-ended');
    expect(() => requireLifecycleOwner(state)).toThrow();
    expect(() =>
      lifecycleState([first, end, run(30, 'new-owner')], repo, 7, 'new-owner'),
    ).toThrow();
  });

  it('never globally deduplicates identical run digests from two actors', () => {
    const first = run(10, 'old-owner');
    const second = run(11, 'new-owner');
    expect(first.digest).toBe(second.digest);
    expect(() => lifecycleState([second, first], repo, 7, 'new-owner')).toThrow(
      /unrelated/,
    );
  });

  it('records abandonment without fabricating an end and requires a bound fresh run', () => {
    const first = run(10, 'old-owner');
    const transition = transfer();
    const pending = lifecycleState([transition, first], repo, 7, 'new-owner');
    expect(pending.state).toBe('transitioned');
    expect(pending.run?.outcome).toBeNull();
    expect(() => requireLifecycleOwner(pending)).toThrow();
    expect(() =>
      lifecycleState(
        [first, transition, run(30, 'new-owner')],
        repo,
        7,
        'new-owner',
      ),
    ).toThrow(/bind/);
    const next = run(
      30,
      'new-owner',
      `<!-- local-review-owner-transition:v1 id=${transition.digest} -->\nSeparate restart authorization.`,
    );
    const active = lifecycleState(
      [first, transition, next],
      repo,
      7,
      'new-owner',
    );
    expect(active.state).toBe('active');
    expect(active.run?.commentId).toBe(30);
    expect(
      lifecycleState([first, transition, next], repo, 7, 'old-owner').state,
    ).toBe('foreign-active');
  });

  it('allows exact transition replay but blocks competing owners and third-party forgery', () => {
    const first = run(10, 'old-owner');
    const transition = transfer();
    expect(
      lifecycleState(
        [first, transition, { ...transition, id: 21 }],
        repo,
        7,
        'new-owner',
      ).transitions,
    ).toHaveLength(1);
    expect(() =>
      lifecycleState(
        [first, transition, transfer(21, 'other-owner')],
        repo,
        7,
        'new-owner',
      ),
    ).toThrow(/competing/);
    expect(() =>
      lifecycleState(
        [first, { ...transition, user: { login: 'third-party' } }],
        repo,
        7,
        'new-owner',
      ),
    ).toThrow(/authorship/);
  });

  it('rejects edited, malformed, expired and wrong-scope transition records', () => {
    const first = run(10, 'old-owner');
    const transition = transfer();
    for (const replacement of [
      { ...transition, lastEditedAt: timestamp },
      { ...transition, body: '<!-- local-review-transition:v1 bad -->' },
      { ...transition, created_at: '2026-01-02T00:00:00Z' },
    ])
      expect(() =>
        lifecycleState([first, replacement], repo, 7, 'new-owner'),
      ).toThrow();
    expect(() =>
      lifecycleState([first, transition], repo, 8, 'new-owner'),
    ).toThrow(/scope/);
    expect(() =>
      lifecycleState(
        [{ ...first, lastEditedAt: timestamp }],
        repo,
        7,
        'old-owner',
      ),
    ).toThrow(/edited/);
  });

  it('late old-owner evidence stays historical, but a late successor fails closed', () => {
    const first = run(10, 'old-owner');
    const transition = transfer();
    const late = comment(
      22,
      'old-owner',
      '<!-- local-review-pass:v3 historical context -->',
    );
    expect(
      lifecycleState([first, transition, late], repo, 7, 'new-owner').state,
    ).toBe('transitioned');
    const end = comment(
      23,
      'old-owner',
      `<!-- local-review-run-end:v1 id=${first.digest} outcome=aborted head=${head} -->`,
    );
    expect(
      lifecycleState([first, transition, end], repo, 7, 'new-owner').state,
    ).toBe('transitioned');
    expect(() =>
      lifecycleState(
        [
          first,
          transition,
          end,
          run(24, 'old-owner', 'Old owner resumed.', 10),
        ],
        repo,
        7,
        'new-owner',
      ),
    ).toThrow();
  });
});
