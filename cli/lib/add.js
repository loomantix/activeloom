'use strict';

/**
 * Tier 0 — `npx activeloom add <skill>`.
 *
 * Installs skills into the user's own agent config directory. No repository, no
 * account, no key, nothing committed. This is the tier the acceptance criterion
 * is about ("a working review skill in under two minutes"), and every choice
 * here defends it: no YAML, no Python, no substitution engine, no config file,
 * and one network round trip.
 *
 * It can afford to be this simple because of a property of the manifest rather
 * than an assumption about it: skills are verbatim sync targets — the only two
 * targets carrying `<<KEY>>` substitutions are `.claude/settings.json` and
 * `.github/copilot-instructions.md`, neither of which is a skill.
 * `assertNoPlaceholders` turns that property into an enforced precondition, so
 * a future skill that starts needing substitution fails loudly here instead of
 * installing a prompt with a literal `<<KEY>>` in front of a model.
 */

const fs = require('node:fs');
const path = require('node:path');
const ui = require('./ui');
const {
  HARNESSES,
  chooseHarnesses: sharedChooseHarnesses,
} = require('./detect');

/** Matches the substitution engine's placeholder grammar exactly. */
const PLACEHOLDER = /<<[A-Z][A-Z0-9_]*>>/;

/**
 * List the skills an upstream tree ships for a harness.
 *
 * @param {string} upstreamDir
 * @param {string} harnessRoot
 * @returns {string[]}
 */
function listSkills(upstreamDir, harnessRoot) {
  const skillsDir = path.join(upstreamDir, harnessRoot, 'skills');
  if (!fs.existsSync(skillsDir)) return [];
  return fs
    .readdirSync(skillsDir, { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .map((e) => e.name)
    .sort();
}

/**
 * Walk every file under `dir`, depth-first.
 *
 * `withFileTypes` gives `lstat`-based entries, so a symlink is neither
 * `isDirectory()` nor `isFile()` and is skipped by both branches below. That is
 * the property this walk relies on, and `copyTree` relies on the same one; no
 * shipped skill contains a symlink, so nothing is lost by dropping them.
 *
 * @param {string} dir
 * @returns {string[]} absolute file paths
 */
function walkFiles(dir) {
  /** @type {string[]} */
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walkFiles(full));
    else if (entry.isFile()) out.push(full);
  }
  return out;
}

/**
 * Refuse to install a tree containing an unsubstituted placeholder.
 *
 * Installing one would put a literal `<<KEY>>` in front of a model, which reads
 * as an instruction it cannot satisfy. Failing here is recoverable; shipping a
 * corrupted prompt is not, and it would stay invisible until a review went wrong.
 *
 * @param {string} dir
 * @param {string} label
 */
function assertNoPlaceholders(dir, label) {
  for (const full of walkFiles(dir)) {
    // Read as UTF-8 and test: a binary file yields replacement characters
    // rather than a false match, and skills are text by construction.
    if (PLACEHOLDER.test(fs.readFileSync(full, 'utf8'))) {
      throw new Error(
        `${label} contains an unsubstituted placeholder (${full}). ` +
          `Skills are meant to be verbatim sync targets — this one needs \`init\`, not \`add\`.`,
      );
    }
  }
}

/**
 * Pick the harnesses to install into.
 *
 * Explicit `--harness` wins. Otherwise every harness this machine shows
 * evidence of, because a config directory or an installed CLI is evidence the
 * user actually runs it. With no evidence at all we install for Claude Code and
 * say so — a first-time user has no config directory yet, and refusing to act
 * would fail the two-minute criterion at the first step.
 *
 * @param {ReturnType<import('./detect').detect>} facts
 * @param {string[]} requested
 * @returns {{ids: string[], reason: string}}
 */
function chooseHarnesses(facts, requested) {
  return sharedChooseHarnesses(facts, requested, {
    noEvidenceReason: 'no agent config found — defaulting to Claude Code',
  });
}

/**
 * Copy a skill directory, preserving the executable bit.
 *
 * Modes matter: `issues/scripts/ready.py` is a `0755` sync target invoked
 * directly, so a copy that flattened permissions would install a skill that
 * silently cannot run its own helper.
 *
 * The `isDirectory()`/`isFile()` pair is load-bearing here, not incidental
 * tidiness: `withFileTypes` reports a symlink as neither, so a link planted in
 * a malformed upstream tree is dropped rather than followed out of the
 * destination. This is the copy, so this is where that guarantee matters.
 *
 * @param {string} src
 * @param {string} dest
 */
function copyTree(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const from = path.join(src, entry.name);
    const to = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyTree(from, to);
    } else if (entry.isFile()) {
      fs.copyFileSync(from, to);
      fs.chmodSync(to, fs.statSync(from).mode & 0o777);
    }
  }
}

const SUPPORT_PATHS = Object.freeze([
  'MODEL_NOTES.md',
  'REVIEW_WORKFLOW.md',
  'SKILL_AUTHORING.md',
  'prompt-stack.json',
  'references',
]);

/** Install harness-level files referenced by skills outside their own tree. */
function installSupportFiles(upstreamDir, harness, homeDir, dryRun, force) {
  const sourceRoot = path.join(upstreamDir, harness.root);
  const destRoot = path.join(homeDir, harness.home);

  const install = (src, dest) => {
    const stat = fs.statSync(src);
    if (stat.isDirectory()) {
      if (!dryRun) fs.mkdirSync(dest, { recursive: true });
      for (const entry of fs.readdirSync(src)) {
        install(path.join(src, entry), path.join(dest, entry));
      }
      return;
    }
    if (fs.existsSync(dest) && !force) return;
    if (dryRun) {
      ui.step(`would install support file ${dest}`);
      return;
    }
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(src, dest);
    fs.chmodSync(dest, stat.mode & 0o777);
  };

  for (const relative of SUPPORT_PATHS) {
    const src = path.join(sourceRoot, relative);
    if (!fs.existsSync(src)) continue;
    install(src, path.join(destRoot, relative));
  }
}

/**
 * @param {object} args
 * @param {string[]} args.skills      Skill names, or empty to list what exists.
 * @param {string} args.upstreamDir   Unpacked upstream tree.
 * @param {ReturnType<import('./detect').detect>} args.facts
 * @param {string[]} args.harnesses   From `--harness`, possibly empty.
 * @param {boolean} args.dryRun
 * @param {boolean} args.force        Replace an existing install.
 * @returns {Promise<number>} process exit code
 */
async function add({ skills, upstreamDir, facts, harnesses, dryRun, force }) {
  const chosen = chooseHarnesses(facts, harnesses);

  if (skills.length === 0) {
    ui.info(`Skills available for ${ui.bold(chosen.ids.join(', '))}:\n`);
    for (const id of chosen.ids) {
      const harness = HARNESSES.find((h) => h.id === id);
      ui.info(`  ${ui.bold(id)}`);
      for (const name of listSkills(upstreamDir, harness.root)) ui.step(name);
      ui.info('');
    }
    ui.info(`Install one with ${ui.bold('npx activeloom add <skill>')}.`);
    return 0;
  }

  let installed = 0;
  let skipped = 0;

  for (const id of chosen.ids) {
    const harness = HARNESSES.find((h) => h.id === id);
    const available = listSkills(upstreamDir, harness.root);
    const destRoot = path.join(facts.homeDir, harness.home, 'skills');
    if (skills.some((name) => available.includes(name))) {
      installSupportFiles(upstreamDir, harness, facts.homeDir, dryRun, force);
    }

    for (const name of skills) {
      if (!available.includes(name)) {
        ui.warn(
          `${id}: no skill named "${name}" (have: ${available.join(', ')})`,
        );
        skipped += 1;
        continue;
      }

      const src = path.join(upstreamDir, harness.root, 'skills', name);
      const dest = path.join(destRoot, name);
      assertNoPlaceholders(src, `${id}/${name}`);

      if (fs.existsSync(dest) && !force) {
        ui.warn(
          `${id}: ${dest} already exists — re-run with --force to replace`,
        );
        skipped += 1;
        continue;
      }

      if (dryRun) {
        ui.step(`would install ${ui.bold(name)} to ${dest}`);
      } else {
        fs.rmSync(dest, { recursive: true, force: true });
        copyTree(src, dest);
        ui.step(`installed ${ui.bold(name)} to ${dest}`);
      }
      installed += 1;
    }
  }

  ui.info('');
  if (dryRun) {
    ui.ok(
      `Dry run: ${installed} would install, ${skipped} skipped. Nothing written.`,
    );
    return 0;
  }

  ui.ok(`${installed} installed, ${skipped} skipped (${chosen.reason}).`);
  if (installed > 0) {
    ui.info('');
    ui.info('Start a new agent session and the skill is available.');
    ui.info(
      ui.dim(
        'To share these with your team instead of just this machine, run `npx activeloom init`.',
      ),
    );
  }
  // Nothing installed and something was asked for is a failed run, not a quiet
  // no-op: a script that pipes this into `&&` needs to see it.
  return installed === 0 && skipped > 0 ? 1 : 0;
}

module.exports = {
  add,
  listSkills,
  chooseHarnesses,
  assertNoPlaceholders,
  copyTree,
  walkFiles,
  installSupportFiles,
};
