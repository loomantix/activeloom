import { readFileSync } from 'node:fs';
import { REPO_RE } from './constants.js';
import { fail } from './errors.js';
import { flattenPages, jsonOutput, runGh } from './github.js';

export const METRICS_STORE_LABEL = 'activeloom:review-metrics';
export const METRICS_STORE_WARNING = 2000;
export const METRICS_STORE_LIMIT = 2500;
export type TelemetryStore = 'pr' | 'issue';

/** Resolve the destination beside the installed bundle; never default after an error. */
export function telemetryStore(): TelemetryStore {
  let config: Record<string, unknown> = {};
  try {
    config = JSON.parse(
      readFileSync(new URL('./review-telemetry.json', import.meta.url), 'utf8'),
    );
    if (!config || typeof config !== 'object' || Array.isArray(config))
      fail('invalid review metrics configuration');
    for (const [key, value] of Object.entries(config)) {
      const allowed =
        key === 'LOOM_REVIEW_TELEMETRY_STORE'
          ? ['pr', 'issue']
          : ['LOOM_REVIEW_TELEMETRY', 'LOOM_REVIEW_TELEMETRY_EXTRACT'].includes(
                key,
              )
            ? ['on', 'off']
            : [];
      if (
        typeof value !== 'string' ||
        !allowed.includes(value.trim().toLowerCase())
      )
        fail('invalid review metrics configuration');
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const value =
    process.env['LOOM_REVIEW_TELEMETRY_STORE']?.trim() ||
    config['LOOM_REVIEW_TELEMETRY_STORE'] ||
    'pr';
  if (
    typeof value !== 'string' ||
    !['pr', 'issue'].includes(value.trim().toLowerCase())
  )
    fail('LOOM_REVIEW_TELEMETRY_STORE must be pr or issue');
  return value.trim().toLowerCase() as TelemetryStore;
}

export interface MetricsStoreIssue {
  number: number;
  comments: number;
  locked: boolean;
  state: string;
}

/** Label discovery uses the issues API, including closed stores after rotation. */
export function metricsStores(
  repo: string,
  state: 'open' | 'all' = 'open',
): MetricsStoreIssue[] {
  if (!REPO_RE.test(repo)) fail('metrics store --repo must be owner/name');
  const rows = flattenPages<Record<string, unknown>>(
    jsonOutput<unknown[]>([
      'api',
      '--paginate',
      '--slurp',
      `repos/${repo}/issues?state=${state}&labels=${encodeURIComponent(METRICS_STORE_LABEL)}&per_page=100`,
    ]),
    'metrics stores',
  );
  return rows.map((row) => {
    if (
      row['pull_request'] != null ||
      !Number.isSafeInteger(row['number']) ||
      Number(row['number']) < 1 ||
      !Number.isSafeInteger(row['comments']) ||
      Number(row['comments']) < 0 ||
      typeof row['locked'] !== 'boolean' ||
      !['open', 'closed'].includes(String(row['state']))
    ) {
      fail('invalid metrics store issue response');
    }
    return {
      number: Number(row['number']),
      comments: Number(row['comments']),
      locked: row['locked'] as boolean,
      state: String(row['state']),
    };
  });
}

export function activeMetricsStore(repo: string): MetricsStoreIssue {
  const stores = metricsStores(repo);
  if (stores.length !== 1)
    fail(
      'metrics emission needs exactly one open store; run metrics-setup or repair duplicate stores',
    );
  const store = stores[0]!;
  if (!store.locked) fail('metrics store is not locked; emission skipped');
  if (store.comments >= METRICS_STORE_LIMIT)
    fail('metrics store is full; run metrics-rotate');
  if (store.comments >= METRICS_STORE_WARNING)
    process.stderr.write(
      'review metrics: store nearing capacity; run metrics-rotate\n',
    );
  return store;
}

/** Explicit operator command only. A review pass never calls this. */
export function setupMetricsStore(
  repo: string,
  rotate = false,
): MetricsStoreIssue {
  const existing = metricsStores(repo);
  if (existing.length > 1)
    fail('multiple open metrics stores; repair before setup or rotation');
  if (existing.length === 1 && !rotate) {
    if (!existing[0]!.locked)
      fail('existing metrics store is not locked; lock it before proceeding');
    return existing[0]!;
  }
  if (rotate && existing.length !== 1)
    fail('rotation needs exactly one open metrics store');
  const labels = flattenPages<Record<string, unknown>>(
    jsonOutput<unknown[]>([
      'api',
      '--paginate',
      '--slurp',
      `repos/${repo}/labels?per_page=100`,
    ]),
    'labels',
  );
  if (!labels.some((label) => label['name'] === METRICS_STORE_LABEL)) {
    jsonOutput(['api', '-X', 'POST', `repos/${repo}/labels`], {
      name: METRICS_STORE_LABEL,
      color: 'ededed',
      description: 'ActiveLoom review metrics store',
    });
  }
  // Close the newly created issue, lock and verify, then replace the active store.
  // A failed rotation can leave no active store, but never an unlocked writer target.
  const created = jsonOutput<Record<string, unknown>>(
    ['api', '-X', 'POST', `repos/${repo}/issues`],
    {
      title: 'ActiveLoom review metrics',
      labels: [METRICS_STORE_LABEL],
      body: 'Machine-readable review metrics. Subscribers receive a notification per record. Read with review-ledger export or report. Keep this issue locked; retain closed stores for historical export.',
    },
  );
  const number = created['number'];
  if (!Number.isSafeInteger(number) || Number(number) < 1)
    fail('store creation returned no issue number');
  const path = `repos/${repo}/issues/${number}`;
  jsonOutput(['api', '-X', 'PATCH', path], { state: 'closed' });
  runGh(['api', '-X', 'PUT', `${path}/lock`]);
  const locked = jsonOutput<Record<string, unknown>>(['api', path]);
  if (locked['locked'] !== true || locked['state'] !== 'closed')
    fail('new metrics store lock could not be verified');
  if (existing[0])
    jsonOutput(
      ['api', '-X', 'PATCH', `repos/${repo}/issues/${existing[0].number}`],
      { state: 'closed' },
    );
  jsonOutput(['api', '-X', 'PATCH', path], { state: 'open' });
  const active = activeMetricsStore(repo);
  if (active.number !== number)
    fail(
      'metrics store setup raced with another operator; repair the labeled stores',
    );
  return active;
}
