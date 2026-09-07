'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const skill = (root) =>
  fs.readFileSync(
    path.join(ROOT, root, 'skills', 'onboard', 'SKILL.md'),
    'utf8',
  );

test('onboard renders each agent-loop contract for its harness', () => {
  const claude = skill('.claude');
  const codex = skill('.codex');
  const agents = skill('.agents');

  assert.match(claude, /requires \*\*both\*\* the Claude and Codex CLIs/);
  assert.match(claude, /`codex_review_hook`/);

  assert.match(codex, /requires \*\*both\*\* the Codex and Claude CLIs/);
  assert.match(codex, /review hooks and Claude effort policy are pinned/);
  assert.doesNotMatch(codex, /\| `codex_review_hook` \|/);

  assert.match(
    agents,
    /requires \*\*both\*\* the Gemini \(`agy`\) and Claude CLIs/,
  );
  assert.match(
    agents,
    /worker model, and\s+fallback model ship with supported defaults/,
  );
  assert.doesNotMatch(agents, /`codex_review_hook`/);
});
