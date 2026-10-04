const assert = require('node:assert/strict');
const { test } = require('node:test');
const { compose } = require('./compose.cjs');

test('nested partials preserve Markdown, whitespace and vocabulary tokens', () => {
  assert.deepEqual(
    compose({
      partials: {
        'review/gate': '## Gate\n\n{{> review/rule}}\n',
        'review/rule': 'Use `<<WORKFLOW_DOC>>` & <base-sha> exactly.\n',
      },
      templates: {
        claude: 'Claude instructions\n\n{{> review/gate}}\nTail\n',
        codex: 'Codex instructions\n\n{{> review/gate}}\nTail\n',
      },
    }),
    {
      claude:
        'Claude instructions\n\n## Gate\n\nUse `<<WORKFLOW_DOC>>` & <base-sha> exactly.\nTail\n',
      codex:
        'Codex instructions\n\n## Gate\n\nUse `<<WORKFLOW_DOC>>` & <base-sha> exactly.\nTail\n',
    },
  );
});

test('one policy edit reaches each engine without changing its instructions', () => {
  const templates = {
    claude: 'Claude\n{{> policy}}\n',
    codex: 'Codex\n{{> policy}}\n',
    agents: 'Agy\n{{> policy}}\n',
  };
  const before = compose({ partials: { policy: 'Old policy\n' }, templates });
  const after = compose({ partials: { policy: 'New policy\n' }, templates });
  for (const name of Object.keys(templates)) {
    assert.equal(after[name], before[name].replace('Old policy', 'New policy'));
  }
});

for (const source of [
  '{{#if engine}}hidden{{/if}}',
  '{{#each engines}}hidden{{/each}}',
  '{{value}}',
  '{{{value}}}',
  '{{> (lookup . "name")}}',
  '{{> ../outside}}',
  '{{> policy context}}',
  '{{> policy mode="hidden"}}',
  '{{#> missing}}fallback{{/missing}}',
  '{{~> policy}}',
]) {
  test(`rejects branching or dynamic composition: ${source}`, () => {
    assert.throws(
      () =>
        compose({
          partials: { policy: 'policy' },
          templates: { entry: source },
        }),
      /only static partial includes/,
    );
  });
}

test('missing and cyclic partials fail before producing output', () => {
  assert.throws(
    () => compose({ partials: {}, templates: { entry: '{{> missing}}' } }),
    /^Error: entry: Missing partial: missing$/,
  );
  assert.throws(
    () => compose({ partials: { outer: '{{> missing}}' }, templates: {} }),
    /^Error: outer: Missing partial: missing$/,
  );
  assert.throws(
    () => compose({ partials: { a: '{{> b}}', b: '{{> a}}' }, templates: {} }),
    /Partial cycle/,
  );
});

test('a parse error names the template that caused it', () => {
  assert.throws(
    () => compose({ partials: {}, templates: { entry: '{{#if x}}open' } }),
    /^Error: entry: Parse error on line 1/,
  );
});

test('validates unused partials and does not retain them between builds', () => {
  assert.throws(
    () =>
      compose({
        partials: { unused: '{{#if x}}hidden{{/if}}' },
        templates: {},
      }),
    /only static partial includes/,
  );
  compose({ partials: { policy: 'ok' }, templates: { entry: '{{> policy}}' } });
  assert.throws(
    () => compose({ partials: {}, templates: { entry: '{{> policy}}' } }),
    /Missing partial/,
  );
});
