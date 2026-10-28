const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const page = fs.readFileSync(process.argv[2], 'utf8');
const source = page.slice(
  page.indexOf('function resultForm(c)'),
  page.indexOf('// Every submission'),
);

// Minimal form DOM; the full render calls resultForm again on each snapshot.
function element(tag, cls, text) {
  return {
    tag,
    className: cls,
    textContent: text || '',
    value: '',
    children: [],
    append(...nodes) {
      this.children.push(...nodes);
    },
    addEventListener(_, callback) {
      this.submit = callback;
    },
    querySelector() {
      return this.picked;
    },
    reset() {
      this.resetCalled = true;
    },
  };
}

async function check(rejectSave) {
  let resolve, reject;
  let writes = 0;
  const context = vm.createContext({
    forms: new Map(),
    STATUSES: ['passed', 'failed', 'blocked', 'skipped'],
    REF_RE: /^[A-Za-z0-9._:#-]{1,64}$/,
    el: element,
    testedHead: () => 'a'.repeat(40),
    save: () => {
      writes++;
      return new Promise((yes, no) => {
        resolve = yes;
        reject = no;
      });
    },
  });
  vm.runInContext(source, context);
  const c = { id: 'pr12-signin', rev: 1, basis: 'a'.repeat(40) };
  const first = context.resultForm(c);
  first.picked = { value: 'failed' };
  const pending = first.submit({ preventDefault() {} });
  const visible = context.resultForm(c);
  const [button, message] = visible.children[2].children;
  assert.equal(button.disabled, true, 'snapshot must retain pending save');
  await visible.submit({ preventDefault() {} });
  assert.equal(writes, 1, 'pending save cannot be submitted again');
  if (rejectSave) reject({ code: 'quota_exceeded' });
  else resolve();
  await pending;
  assert.equal(button.disabled, false);
  if (rejectSave) {
    assert.match(message.textContent, /store is full/);
    assert.ok(!visible.resetCalled, 'failure preserves the draft');
  } else {
    assert.equal(message.textContent, 'Saved.');
    assert.equal(visible.resetCalled, true);
  }
}

Promise.all([check(false), check(true)]).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
