const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { add, listSkills } = require('../../cli/lib/add');
const { HARNESSES } = require('../../cli/lib/detect');
const upstreamDir = path.resolve(__dirname, '../..');

test('internal-docs installs through the actual add interface for all three harnesses and upgrades an existing copy', async () => {
  const homeDir = fs.mkdtempSync(
    path.join(os.tmpdir(), 'internal-docs-install-'),
  );
  try {
    for (const harness of HARNESSES)
      assert.ok(
        listSkills(upstreamDir, harness.root).includes('internal-docs'),
      );
    const options = {
      skills: ['internal-docs'],
      upstreamDir,
      facts: { homeDir },
      harnesses: HARNESSES.map((h) => h.id),
      dryRun: false,
      force: false,
    };
    assert.equal(await add(options), 0);
    for (const harness of HARNESSES) {
      const installed = path.join(
        homeDir,
        harness.home,
        'skills/internal-docs/SKILL.md',
      );
      assert.equal(
        fs.readFileSync(installed, 'utf8'),
        fs.readFileSync(
          path.join(upstreamDir, harness.root, 'skills/internal-docs/SKILL.md'),
          'utf8',
        ),
      );
    }
    const installed = path.join(
      homeDir,
      '.agents/skills/internal-docs/SKILL.md',
    );
    fs.writeFileSync(installed, 'Old install');
    assert.equal(await add(options), 0);
    assert.equal(fs.readFileSync(installed, 'utf8'), 'Old install');
    assert.equal(await add({ ...options, force: true }), 0);
    assert.equal(
      fs.readFileSync(installed, 'utf8'),
      fs.readFileSync(
        path.join(upstreamDir, '.agents/skills/internal-docs/SKILL.md'),
        'utf8',
      ),
    );
  } finally {
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
});
