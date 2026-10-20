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

test('all setup surfaces offer repository auto-glance without silent consent', () => {
  for (const root of ['.claude', '.codex', '.agents']) {
    const onboard = skill(root);
    assert.match(onboard, /absent `glance.auto` choice/);
    assert.match(onboard, /3a\. Repository auto-glance/);
    assert.match(onboard, /An explicit `false` is a settled choice/);
    const setup = fs.readFileSync(
      path.join(ROOT, root, 'skills/review-setup/SKILL.md'),
      'utf8',
    );
    const section = setup
      .split('## Repository auto-glance')[1]
      .split('\n## ')[0];
    for (const phrase of [
      'Yes (recommended',
      'No (keep human glance)',
      'Wait for an explicit answer',
      'installer `--yes` is not consent',
      'preserving all other fields',
      '`true` for Yes, `false` for No',
      'On deferral, leave the file untouched',
    ]) {
      assert.ok(section.includes(phrase), `${root}: missing ${phrase}`);
    }
    assert.ok(
      section.includes(
        `${root}/skills/hreview/scripts/herd-config.py validate`,
      ),
    );
    assert.match(section, /never in the personal review profile/);
    assert.match(section, /both must carry the configuration/);
    assert.match(setup, /even if the personal profile is already configured/);
  }
});
