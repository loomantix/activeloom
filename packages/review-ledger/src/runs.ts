import { fail } from './errors.js';
import { sha256Text } from './hash.js';

/** An authenticated run boundary emitted by the local review controller. */
export interface ReviewRun {
  id: string;
  commentId: number;
  base: string;
  maxRounds: number;
}

/** A GitHub issue-comment ID: a positive safe integer. */
function isCommentId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

const RUN =
  /^<!-- local-review-run:v1 id=([0-9a-f]{64}) tier=(lean|deep) max-rounds=([1-4]) base=([0-9a-f]{40}) start-head=([0-9a-f]{40}) supersedes=(none|[1-9][0-9]*) content-sha256=([0-9a-f]{64}) -->\n/;

/**
 * Read actor-owned run comments using the controller's canonical JSON digest.
 * Callers supply authenticated comments, just as for roster declarations.
 * Legacy PRs without run markers retain their original identity namespace.
 */
export function reviewRuns(rows: Array<Record<string, unknown>>): ReviewRun[] {
  const runs: ReviewRun[] = [];
  const aliases = new Map<number, number>();
  const seen = new Map<string, { body: string; commentId: number }>();
  const ordered = [...rows].sort((a, b) => Number(a['id']) - Number(b['id']));
  for (const row of ordered) {
    const body = row['body'];
    if (
      typeof body !== 'string' ||
      !body.startsWith('<!-- local-review-run:v1')
    )
      continue;
    const match = RUN.exec(body);
    const commentId = row['id'];
    if (!match || !isCommentId(commentId)) {
      fail(
        'local-review run marker is malformed; preserve it and repair the run declaration before retrying',
      );
    }
    const [, id, tier, cap, base, startHead, parent, digest] = match;
    const maxRounds = Number(cap);
    const supersedes = parent === 'none' ? null : Number(parent);
    if (supersedes !== null && !Number.isSafeInteger(supersedes)) {
      fail('local-review run parent must be a safe comment ID');
    }
    const expected = sha256Text(
      JSON.stringify({
        base,
        content: body.slice(match[0].length),
        max_rounds: maxRounds,
        start_head: startHead,
        supersedes,
        tier,
      }),
    );
    if (
      expected !== id ||
      expected !== digest ||
      maxRounds !== (tier === 'deep' ? 4 : 2)
    ) {
      fail('local-review run content digest or round budget is invalid');
    }
    const duplicate = seen.get(id!);
    if (duplicate) {
      if (duplicate.body !== body)
        fail('duplicate local-review run id has conflicting content');
      aliases.set(commentId, duplicate.commentId);
      continue;
    }
    const canonicalParent =
      supersedes === null ? null : (aliases.get(supersedes) ?? supersedes);
    if (canonicalParent !== (runs.at(-1)?.commentId ?? null)) {
      fail('local-review run supersession chain is incomplete or forked');
    }
    seen.set(id!, { body, commentId });
    runs.push({ id: id!, commentId, base: base!, maxRounds });
  }
  return runs;
}

/** Associate an attestation with its preceding run, independent of page order. */
export function reviewRunForComment(
  runs: ReviewRun[],
  commentId: unknown,
): ReviewRun | undefined {
  if (runs.length === 0) return undefined;
  if (!isCommentId(commentId)) {
    fail('attestation comment ID is required to recover its review run');
  }
  for (let index = runs.length - 1; index >= 0; index--) {
    if (runs[index]!.commentId < commentId) return runs[index];
  }
  return undefined;
}
