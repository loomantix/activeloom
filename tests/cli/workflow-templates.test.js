'use strict';

/**
 * Verifies structural and behavioral parity across Tier 2 and Tier 3 workflow templates.
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
  // Normalize whitespace across continuation lines.
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
  // Pin required template placeholders.
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

test("both templates preserve today's open sync PR", () => {
  for (const [name, body] of TEMPLATES) {
    assert.match(
      body,
      /\.headRefName != \\\"\$\{BRANCH\}\\\"/,
      `${name}: close-prior filter does not exclude the current branch`,
    );
  }
});

test('both templates publish the replacement before closing prior PRs', () => {
  for (const [name, body] of TEMPLATES) {
    assert.ok(
      body.indexOf('gh pr create') < body.indexOf('gh pr list'),
      `${name}: prior PRs are closed before the replacement is open`,
    );
  }
});

test('only the app template uses App credentials', () => {
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

test('the app template permits anonymous reads from public upstreams', () => {
  assert.doesNotMatch(APP, /Validate UPSTREAM_READ_TOKEN is configured/);
  assert.match(APP, /if \[ -n "\$\{UPSTREAM_READ_TOKEN:-\}" \]/);
  assert.match(
    APP,
    /If the upstream is private, configure UPSTREAM_READ_TOKEN/,
  );
});

test('the token template grants itself the write scope it needs', () => {
  assert.match(
    TOKEN,
    /^permissions:\n {2}contents: write\n {2}pull-requests: write$/m,
  );
});

test('the token template names the repository setting it depends on', () => {
  assert.match(
    TOKEN,
    /Allow GitHub Actions to create and approve pull requests/,
  );
  assert.match(TOKEN, /::error::GITHUB_TOKEN may not open pull requests/);
});

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
    assert.match(body, /^ {2}UPSTREAM_REPO: 'acme\/consumer-upstream'$/m);
    assert.match(body, /^ {2}PR_BASE_BRANCH: 'trunk'$/m);
    assert.doesNotMatch(body, /<owner>\/<repo>/);
    assert.doesNotMatch(body, /^ {2}PR_BASE_BRANCH: ''$/m);
  }
});

test('the installed workflow tracks the ref the trees came from', () => {
  // Verify UPSTREAM_REF matches the ref used during init.
  assert.match(
    workflowFor(TIERS[2], { upstreamRef: 'main' }),
    /^ {2}UPSTREAM_REF: 'main'$/m,
  );
  // Local upstreamDir preserves default sync-v2 ref.
  assert.match(
    workflowFor(TIERS[2], { upstreamRef: 'local' }),
    /^ {2}UPSTREAM_REF: sync-v2$/m,
  );
});

test('workflow substitutions quote legal Git punctuation as YAML scalars', () => {
  const body = workflowFor(TIERS[2], {
    upstreamRef: "release/o'clock",
    baseBranch: "release/o'clock",
  });
  assert.match(body, /^ {2}UPSTREAM_REF: 'release\/o''clock'$/m);
  assert.match(body, /^ {2}PR_BASE_BRANCH: 'release\/o''clock'$/m);
});

test('an existing workflow must match the requested tier unless force replaces it', () => {
  const repoDir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-wf-'));
  try {
    const options = {
      tier: TIERS[2],
      upstreamDir: REPO_ROOT_FOR_CLI,
      repoDir,
      upstreamRepo: 'acme/consumer-upstream',
      upstreamRef: 'sync-v2',
      baseBranch: 'trunk',
      dryRun: false,
      force: false,
    };
    const first = writeWorkflow(options);
    assert.strictEqual(first.ready, true);
    assert.strictEqual(writeWorkflow(options).ready, true);
    const mismatch = writeWorkflow({ ...options, tier: TIERS[3] });
    assert.strictEqual(mismatch.ready, false);
    assert.match(mismatch.note, /--force/);
    const replacement = writeWorkflow({
      ...options,
      tier: TIERS[3],
      force: true,
    });
    assert.strictEqual(replacement.ready, true);
    assert.match(fs.readFileSync(replacement.dest, 'utf8'), /SYNC_APP_ID/);
  } finally {
    fs.rmSync(repoDir, { recursive: true, force: true });
  }
});

test('a template missing a placeholder is refused by name', () => {
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
