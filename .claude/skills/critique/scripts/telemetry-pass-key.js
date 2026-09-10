#!/usr/bin/env node
// Invoke once at the pass boundary; save and reuse the key on every retry.
import { createHash, randomUUID } from 'node:crypto';

const [repo, pr, run, actor, engine, passType, round, head, ...extra] =
  process.argv.slice(2);
const token = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
let payload;
if (
  extra.length ||
  !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo ?? '') ||
  !/^[1-9][0-9]*$/.test(pr ?? '') ||
  !(run === 'standalone' || /^[0-9a-f]{64}$/.test(run ?? '')) ||
  !token.test(actor ?? '') ||
  !token.test(engine ?? '') ||
  !['review', 'refactor', 'hosted'].includes(passType) ||
  !/^[1-9][0-9]*$/.test(round ?? '') ||
  !/^[0-9a-f]{40}$/.test(head ?? '')
) {
  payload = {
    idempotencyKey: null,
    error:
      'requires repo pr run-id-or-standalone actor engine pass-type round head',
  };
} else {
  const identity = [
    repo,
    pr,
    run === 'standalone' ? randomUUID() : run,
    actor.toLowerCase(),
    engine,
    passType,
    round,
    head,
  ];
  payload = {
    idempotencyKey:
      'pass:' +
      createHash('sha256').update(JSON.stringify(identity)).digest('hex'),
    error: null,
  };
}
process.stdout.write(JSON.stringify(payload) + '\n');
