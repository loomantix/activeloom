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

const WORKFLOWS = path.resolve(__dirname, '..', '..', '.github', 'workflows');
const APP = fs.readFileSync(
  path.join(WORKFLOWS, 'sync-from-upstream.yml.template'),
  'utf8',
);
const TOKEN = fs.readFileSync(
  path.join(WORKFLOWS, 'sync-from-upstream-token.yml.template'),
  'utf8',
);

test('both templates default to the same content gate', () => {
  for (const [name, body] of [
    ['app', APP],
    ['token', TOKEN],
  ]) {
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
  for (const [name, body] of [
    ['app', APP],
    ['token', TOKEN],
  ]) {
    assert.match(
      body,
      /if: vars\.SKIP_UPSTREAM_SYNC == ''/,
      `${name} template has no kill switch`,
    );
  }
});

test('both templates refuse to guess PR_BASE_BRANCH', () => {
  for (const [name, body] of [
    ['app', APP],
    ['token', TOKEN],
  ]) {
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
  for (const [name, body] of [
    ['app', APP],
    ['token', TOKEN],
  ]) {
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
