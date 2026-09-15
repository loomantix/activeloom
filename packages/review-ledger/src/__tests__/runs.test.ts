import { describe, expect, it } from 'vitest';
import { reviewRunForComment, reviewRuns } from '../runs.js';
import { sha256Text } from '../hash.js';
import { attestationsAtHead } from '../roster.js';

const BASE = 'a'.repeat(40);
const HEAD = 'b'.repeat(40);

function run(commentId: number, supersedes: number | null = null) {
  const content = 'Continue review. Café.\n';
  const id = sha256Text(
    JSON.stringify({
      base: BASE,
      content,
      max_rounds: 4,
      start_head: HEAD,
      supersedes,
      tier: 'deep',
    }),
  );
  return {
    id: commentId,
    body: `<!-- local-review-run:v1 id=${id} tier=deep max-rounds=4 base=${BASE} start-head=${HEAD} supersedes=${supersedes ?? 'none'} content-sha256=${id} -->\n${content}`,
  };
}

function pass(id: number, head: string) {
  return {
    id,
    body: `<!-- local-review-pass:v3 engine=codex round=1 base=${BASE} head=${head} result-sha256=${'c'.repeat(64)} -->\nReviewed.\n`,
  };
}

describe('review run boundaries', () => {
  it('preserves legacy identity and sorts paginated comments by ID', () => {
    const runs = reviewRuns([run(30, 10), run(10)]);
    expect(reviewRunForComment(runs, 5)).toBeUndefined();
    expect(reviewRunForComment(runs, 20)?.commentId).toBe(10);
    expect(reviewRunForComment(runs, 40)?.commentId).toBe(30);
  });

  it('tolerates a retried identical run declaration without resetting its boundary', () => {
    const first = run(10);
    const duplicate = { ...first, id: 30 };
    expect(
      reviewRuns([first, duplicate, run(40, 30)]).map((r) => r.commentId),
    ).toEqual([10, 40]);
  });

  it('rejects tampered authorization and a forked chain', () => {
    const first = run(10);
    expect(() =>
      reviewRuns([{ ...first, body: first.body + 'changed' }]),
    ).toThrow(/digest/);
    expect(() => reviewRuns([first, run(30, 9)])).toThrow(/forked/);
  });

  it('rejects a malformed run marker and a cap that does not match its tier', () => {
    expect(() =>
      reviewRuns([{ id: 10, body: '<!-- local-review-run:v1 garbage -->\n' }]),
    ).toThrow(/malformed/);
    const content = 'Lean run claiming a deep budget.\n';
    const id = sha256Text(
      JSON.stringify({
        base: BASE,
        content,
        max_rounds: 4,
        start_head: HEAD,
        supersedes: null,
        tier: 'lean',
      }),
    );
    expect(() =>
      reviewRuns([
        {
          id: 10,
          body: `<!-- local-review-run:v1 id=${id} tier=lean max-rounds=4 base=${BASE} start-head=${HEAD} supersedes=none content-sha256=${id} -->\n${content}`,
        },
      ]),
    ).toThrow(/round budget/);
  });

  it('does not accept quoted or fenced run declarations as a new namespace', () => {
    expect(
      reviewRuns([{ ...run(10), body: `Quoted:\n${run(10).body}` }]),
    ).toEqual([]);
  });

  it('counts exact-head coverage across restarts without erasing legacy history', () => {
    const rows = [
      pass(5, BASE),
      run(10),
      pass(20, BASE),
      run(30, 10),
      pass(40, HEAD),
    ];
    expect(attestationsAtHead(rows.reverse(), HEAD)).toEqual([
      { engine: 'codex', round: 1, status: 'clean' },
    ]);
  });

  it('collapses identical delivery duplicates but rejects contradictory evidence in one run', () => {
    const rows = [run(10), pass(20, HEAD), pass(21, HEAD)];
    expect(attestationsAtHead(rows, HEAD)).toHaveLength(1);
    expect(() => attestationsAtHead([...rows, pass(22, BASE)], HEAD)).toThrow(
      /duplicated/,
    );
  });

  it('never guesses the run of an attestation missing its comment ID', () => {
    expect(() =>
      attestationsAtHead([run(10), { body: pass(20, HEAD).body }], HEAD),
    ).toThrow(/comment ID/);
  });
});
