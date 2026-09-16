'use strict';

/**
 * Tier 0 — `npx activeloom add <skill>`.
 *
 * Installs skills directly into user agent configuration directories.
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
 * Walk every file under `dir`, depth-first, ignoring symlinks.
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
 * Verify that a skill tree contains no unsubstituted placeholder tokens.
 *
 * @param {string} dir
 * @param {string} label
 */
function assertNoPlaceholders(dir, label) {
  for (const full of walkFiles(dir)) {
    // Skills are text by construction.
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
 * Copy a skill directory, preserving permissions and skipping symlinks.
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
  'agents',
  'prompt-stack.json',
  'references',
]);

/** Install harness-level files referenced by skills outside their own tree. */
function installSupportFiles(upstreamDir, harness, homeDir, dryRun, force) {
  const sourceRoot = path.join(upstreamDir, harness.root);
  const destRoot = path.join(homeDir, harness.home);

  const install = (src, dest) => {
    let stat;
    try {
      stat = fs.lstatSync(src);
    } catch (error) {
      if (error.code === 'ENOENT') return;
      throw error;
    }
    if (stat.isSymbolicLink()) return;
    if (stat.isDirectory()) {
      if (!dryRun) fs.mkdirSync(dest, { recursive: true });
      for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
        if (entry.isSymbolicLink()) continue;
        install(path.join(src, entry.name), path.join(dest, entry.name));
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
  // Track skills found across any selected harness.
  const found = new Set();

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

      found.add(name);

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
        fs.mkdirSync(destRoot, { recursive: true });
        const staging = fs.mkdtempSync(path.join(destRoot, `.tmp-${name}-`));
        let backup = null;
        let promoted = false;
        try {
          copyTree(src, staging);
          let destExists = false;
          try {
            fs.lstatSync(dest);
            destExists = true;
          } catch (error) {
            if (error.code !== 'ENOENT') throw error;
          }
          if (destExists) {
            backup = fs.mkdtempSync(path.join(destRoot, `.tmp-${name}-old-`));
            fs.rmdirSync(backup);
            fs.renameSync(dest, backup);
          }
          try {
            fs.renameSync(staging, dest);
            promoted = true;
          } catch (renameError) {
            if (backup) {
              try {
                fs.renameSync(backup, dest);
                backup = null;
              } catch (restoreError) {
                throw new Error(
                  `${renameError.message}; could not restore the original skill: ${restoreError.message}. Original skill preserved at ${backup}.`,
                  { cause: renameError },
                );
              }
            }
            throw renameError;
          }
          ui.step(`installed ${ui.bold(name)} to ${dest}`);
        } finally {
          fs.rmSync(staging, { recursive: true, force: true });
          if (backup && promoted) {
            fs.rmSync(backup, { recursive: true, force: true });
          }
        }
      }
      installed += 1;
    }
  }

  ui.info('');
  if (dryRun) {
    ui.ok(
      `Dry run: ${installed} would install, ${skipped} skipped. Nothing written.`,
    );
    return skills.some((name) => !found.has(name)) ? 1 : 0;
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
  // Fail only when a requested name exists in none of the selected harnesses.
  return skills.some((name) => !found.has(name)) ? 1 : 0;
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
