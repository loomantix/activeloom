'use strict';

/**
 * The two sync workflow templates may differ in identity — and nowhere else
 * that matters.
 *
 * Tier 2 (`sync-from-upstream-token.yml.template`, `GITHUB_TOKEN`) and Tier 3
 * (`sync-from-upstream.yml.template`, GitHub App) are separate files rather
 * than one file with a switch, because they differ in permissions, credential,
 * and commit mechanism — a difference in kind, not a flag. The cost of that
 * choice is duplication, and duplication drifts.
 *
 * So pin the parts that must never diverge: both must invoke the same engine
 * with the same arguments, default to the same content gate, honour the same
 * kill switch, and refuse to guess `PR_BASE_BRANCH`. A tier-2 workflow that
 * quietly tracked `main` while tier 3 tracked `sync-v2` would ship unreviewed
 * upstream content to exactly the consumers who chose the lower-trust tier.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT_FOR_CLI = path.resolve(__dirname, '..', '..');
const WORKFLOWS = path.join(REPO_ROOT_FOR_CLI, '.github', 'workflows');
const APP = fs.readFileSync(
  path.join(WORKFLOWS, 'sync-from-upstream.yml.template'),
  'utf8',
);
const TOKEN = fs.readFileSync(
  path.join(WORKFLOWS, 'sync-from-upstream-token.yml.template'),
  'utf8',
);
const TEMPLATES = [
  ['app', APP],
  ['token', TOKEN],
];

test('both templates default to the same content gate', () => {
  for (const [name, body] of TEMPLATES) {
    assert.match(
      body,
      /^ {2}UPSTREAM_REF: sync-v2$/m,
      `${name} template does not pin sync-v2`,
    );
  }
});

test('both templates invoke the same engine with the same arguments', () => {
  // Whitespace-normalised: the two files wrap the continuation differently and
  // that is not a difference worth failing on.
  const invocation = (body) => {
    const m =
      /python3 \/tmp\/upstream\/scripts\/sync-engine\.py[\s\S]*?--consumer-dir \./.exec(
        body,
      );
    assert.ok(m, 'no sync-engine invocation found');
    return m[0].replace(/\s+/g, ' ').trim();
  };
  assert.strictEqual(invocation(APP), invocation(TOKEN));
});

test('both templates honour the kill switch', () => {
  for (const [name, body] of TEMPLATES) {
    assert.match(
      body,
      /if: vars\.SKIP_UPSTREAM_SYNC == ''/,
      `${name} template has no kill switch`,
    );
  }
});

test('both templates refuse to guess PR_BASE_BRANCH', () => {
  for (const [name, body] of TEMPLATES) {
    assert.match(
      body,
      /PR_BASE_BRANCH: ''/,
      `${name} template ships a default base branch`,
    );
    assert.match(
      body,
      /::error::PR_BASE_BRANCH is empty/,
      `${name} template does not validate PR_BASE_BRANCH`,
    );
  }
});

test('both templates carry the placeholders the CLI substitutes', () => {
  // `writeWorkflow` throws when neither placeholder is found, but only after it
  // has already decided to write. Pinning them here fails at build time
  // instead, where the fix is obvious.
  for (const [name, body] of TEMPLATES) {
    assert.ok(
      body.includes('UPSTREAM_REPO: <owner>/<repo>'),
      `${name}: no UPSTREAM_REPO placeholder`,
    );
    assert.ok(
      body.includes("PR_BASE_BRANCH: ''"),
      `${name}: no PR_BASE_BRANCH placeholder`,
    );
  }
});

test('only the app template uses App credentials', () => {
  // The whole claim of Tier 2 is "no secrets". A stray `secrets.SYNC_APP_*`
  // reference in the token template would make it fail on a repo that has none,
  // which is every repo the tier is aimed at.
  assert.match(APP, /secrets\.SYNC_APP_ID/);
  assert.ok(
    !/SYNC_APP_ID/.test(TOKEN),
    'the token template must not reference App secrets',
  );
  assert.ok(
    !/UPSTREAM_READ_TOKEN/.test(TOKEN),
    'the token template must not reference an upstream read token — it cannot read a private upstream',
  );
  assert.ok(
    !/create-signed-commit\.py/.test(TOKEN),
    'the token template must not claim to create signed commits',
  );
});

test('the token template grants itself the write scope it needs', () => {
  // With no App token, GITHUB_TOKEN is the only identity, so the job needs
  // write scope the App variant does not. Getting this wrong fails at the push,
  // several minutes into a scheduled run nobody is watching.
  assert.match(
    TOKEN,
    /^permissions:\n {2}contents: write\n {2}pull-requests: write$/m,
  );
});

test('the token template names the repository setting it depends on', () => {
  // `GITHUB_TOKEN` cannot open a PR unless the repo allows it, and the raw
  // error does not name the setting. Both the header and the failure path have
  // to say so, or every Tier 2 adopter files the same issue.
  assert.match(
    TOKEN,
    /Allow GitHub Actions to create and approve pull requests/,
  );
  assert.match(TOKEN, /::error::GITHUB_TOKEN may not open pull requests/);
});

// --- which template each tier actually receives -----------------------------
//
// Everything above compares the two templates as files. That says nothing about
// the branch that decides which one a consumer gets, and until this block that
// branch had no test at all: changing `tier.n === 3` to `tier.n >= 2` kept the
// whole suite green while every Tier 2 consumer received a workflow referencing
// `secrets.SYNC_APP_ID` — a secret those repositories have no reason to hold,
// since needing none is the entire promise of the tier. The failure would first
// appear on someone else's scheduled run.

const os = require('node:os');
const { writeWorkflow } = require(
  path.join(REPO_ROOT_FOR_CLI, 'cli', 'lib', 'init.js'),
);
const { TIERS } = require(
  path.join(REPO_ROOT_FOR_CLI, 'cli', 'lib', 'tiers.js'),
);

/** Write a workflow for `tier` into a throwaway repo and return its body. */
const workflowFor = (tier, overrides = {}) => {
  const repoDir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-wf-'));
  try {
    const result = writeWorkflow({
      tier,
      upstreamDir: REPO_ROOT_FOR_CLI,
      repoDir,
      upstreamRepo: 'acme/consumer-upstream',
      upstreamRef: 'sync-v2',
      baseBranch: 'trunk',
      dryRun: false,
      force: false,
      ...overrides,
    });
    return fs.readFileSync(result.dest, 'utf8');
  } finally {
    fs.rmSync(repoDir, { recursive: true, force: true });
  }
};

test('tier 2 gets the token template and tier 3 the App template', () => {
  const tier2 = workflowFor(TIERS[2]);
  assert.doesNotMatch(
    tier2,
    /SYNC_APP_ID/,
    'tier 2 received the App template — those consumers hold no App secrets',
  );
  assert.match(tier2, /secrets\.GITHUB_TOKEN/);

  const tier3 = workflowFor(TIERS[3]);
  assert.match(tier3, /SYNC_APP_ID/);
});

test('the installed workflow carries the substituted values, not placeholders', () => {
  for (const tier of [TIERS[2], TIERS[3]]) {
    const body = workflowFor(tier);
    assert.match(body, /^ {2}UPSTREAM_REPO: acme\/consumer-upstream$/m);
    assert.match(body, /^ {2}PR_BASE_BRANCH: 'trunk'$/m);
    assert.doesNotMatch(body, /<owner>\/<repo>/);
    assert.doesNotMatch(body, /^ {2}PR_BASE_BRANCH: ''$/m);
  }
});

test('the installed workflow tracks the ref the trees came from', () => {
  // `init --sync --ref main` renders trees from `main`; a workflow left on the
  // template's `sync-v2` would open a PR reverting them on its next run.
  assert.match(
    workflowFor(TIERS[2], { upstreamRef: 'main' }),
    /^ {2}UPSTREAM_REF: main$/m,
  );
  // `--upstream-dir` has no remote ref to track, so the template's own default
  // is left standing rather than replaced with the word `local`.
  assert.match(
    workflowFor(TIERS[2], { upstreamRef: 'local' }),
    /^ {2}UPSTREAM_REF: sync-v2$/m,
  );
});

test('a template missing a placeholder is refused by name', () => {
  // The guard used to compare the whole body before and after, which passed as
  // long as *either* substitution landed — so a drifted `PR_BASE_BRANCH` line
  // installed a workflow the consumer's own validate step then rejected.
  const upstreamDir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-up-'));
  const repoDir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-wf-'));
  try {
    fs.mkdirSync(path.join(upstreamDir, '.github', 'workflows'), {
      recursive: true,
    });
    fs.writeFileSync(
      path.join(
        upstreamDir,
        '.github',
        'workflows',
        'sync-from-upstream-token.yml.template',
      ),
      'env:\n  UPSTREAM_REPO: <owner>/<repo>\n  UPSTREAM_REF: sync-v2\n',
    );
    assert.throws(
      () =>
        writeWorkflow({
          tier: TIERS[2],
          upstreamDir,
          repoDir,
          upstreamRepo: 'acme/consumer-upstream',
          upstreamRef: 'sync-v2',
          baseBranch: 'trunk',
          dryRun: false,
          force: false,
        }),
      /PR_BASE_BRANCH placeholder/,
    );
  } finally {
    fs.rmSync(upstreamDir, { recursive: true, force: true });
    fs.rmSync(repoDir, { recursive: true, force: true });
  }
});
