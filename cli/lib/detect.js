'use strict';

/**
 * Deterministic environment detection via file tests, PATH lookups, and git/gh queries.
 *
 * Fields return verified facts or null without heuristic inference.
 */

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

/**
 * Harnesses supported by the CLI, in manifest declaration order.
 */
const HARNESSES = Object.freeze([
  Object.freeze({
    id: 'claude',
    root: '.claude',
    home: '.claude',
    cli: 'claude',
  }),
  Object.freeze({ id: 'codex', root: '.codex', home: '.codex', cli: 'codex' }),
  Object.freeze({ id: 'gemini', root: '.agents', home: '.agents', cli: 'agy' }),
]);

/**
 * Run a command and return trimmed stdout, or null on error or empty output.
 *
 * @param {string} file
 * @param {readonly string[]} args
 * @param {string} [cwd]
 * @returns {string | null}
 */
function tryExec(file, args, cwd) {
  try {
    const out = execFileSync(file, args, {
      cwd,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 10_000,
    });
    const trimmed = out.trim();
    return trimmed === '' ? null : trimmed;
  } catch {
    return null;
  }
}

/**
 * Check whether an executable exists on PATH.
 *
 * @param {string} name
 * @returns {boolean}
 */
function onPath(name) {
  if (process.platform === 'win32') return tryExec('where', [name]) !== null;
  return tryExec('sh', ['-c', `command -v ${name}`]) !== null;
}

/** @param {string} p @returns {boolean} */
function isDir(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

/** @param {string} p @returns {boolean} */
function isFile(p) {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

/**
 * Detect harness signals present in the repository and on the local machine.
 *
 * @param {string} repoDir
 * @param {string} homeDir
 */
function detectHarnesses(repoDir, homeDir) {
  return HARNESSES.map((h) => ({
    id: h.id,
    root: h.root,
    /** This repo already has the harness's prompt root checked in. */
    inRepo: isDir(path.join(repoDir, h.root)),
    /** This machine has the harness's config directory. */
    onMachine: isDir(path.join(homeDir, h.home)),
    /** This machine has the harness's CLI on PATH. */
    cliInstalled: onPath(h.cli),
  }));
}

/**
 * Detect package manager from present lockfiles.
 *
 * @param {string} repoDir
 * @returns {string | null}
 */
function detectPackageManager(repoDir) {
  /** @type {ReadonlyArray<[string, string]>} */
  const lockfiles = [
    ['pnpm-lock.yaml', 'pnpm'],
    ['bun.lockb', 'bun'],
    ['yarn.lock', 'yarn'],
    ['package-lock.json', 'npm'],
  ];
  for (const [file, manager] of lockfiles) {
    if (isFile(path.join(repoDir, file))) return manager;
  }
  return null;
}

/**
 * The language ecosystems present, by their canonical marker file.
 *
 * @param {string} repoDir
 * @returns {string[]}
 */
function detectEcosystems(repoDir) {
  /** @type {ReadonlyArray<[string, string]>} */
  const markers = [
    ['package.json', 'node'],
    ['pyproject.toml', 'python'],
    ['requirements.txt', 'python'],
    ['go.mod', 'go'],
    ['Cargo.toml', 'rust'],
    ['Gemfile', 'ruby'],
    ['pubspec.yaml', 'dart'],
  ];
  const found = new Set();
  for (const [file, ecosystem] of markers) {
    if (isFile(path.join(repoDir, file))) found.add(ecosystem);
  }
  return [...found];
}

/**
 * Detect test, lint, and format script entry points from package.json.
 *
 * @param {string} repoDir
 * @returns {{test: string | null, lint: string | null, format: string | null}}
 */
function detectScripts(repoDir) {
  const empty = { test: null, lint: null, format: null };
  const pkgPath = path.join(repoDir, 'package.json');
  if (!isFile(pkgPath)) return empty;
  try {
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
    const scripts =
      pkg && typeof pkg.scripts === 'object' && pkg.scripts !== null
        ? pkg.scripts
        : {};
    const pick = (...names) => {
      for (const name of names) {
        if (typeof scripts[name] === 'string') return name;
      }
      return null;
    };
    return {
      test: pick('test', 'tests', 'test:unit'),
      lint: pick('lint', 'lint:check', 'eslint'),
      format: pick('format', 'format:check', 'prettier'),
    };
  } catch {
    // Ignore malformed package.json.
    return empty;
  }
}

/**
 * Detect git and GitHub repository facts from origin remote and HEAD.
 *
 * @param {string} repoDir
 */
function detectGit(repoDir) {
  const inRepo =
    tryExec('git', ['rev-parse', '--is-inside-work-tree'], repoDir) === 'true';
  if (!inRepo) {
    return {
      inRepo: false,
      remote: null,
      slug: null,
      defaultBranch: null,
      ghAuthenticated: false,
    };
  }

  const remote = tryExec('git', ['remote', 'get-url', 'origin'], repoDir);
  let slug = null;
  if (remote) {
    // Match SSH or HTTPS origin URLs for github.com repositories.
    const m =
      /^(?:git@github\.com:|(?:ssh|https?):\/\/(?:[^@/]*@)?github\.com\/)([^/]+)\/(.+?)(?:\.git)?$/.exec(
        remote,
      );
    if (m) slug = `${m[1]}/${m[2]}`;
  }

  // Use remote HEAD rather than local branch for repository default.
  const originHead = tryExec(
    'git',
    ['symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD'],
    repoDir,
  );
  const defaultBranch = originHead
    ? originHead.replace(/^refs\/remotes\/origin\//, '')
    : null;

  return {
    inRepo: true,
    remote,
    slug,
    defaultBranch,
    ghAuthenticated: tryExec('gh', ['auth', 'status']) !== null,
  };
}

/**
 * Collect environment facts without interactive prompts or heuristics.
 *
 * @param {object} [options]
 * @param {string} [options.repoDir] Directory to inspect. Defaults to cwd.
 * @param {string} [options.homeDir] Home directory. Injectable for tests.
 */
function detect(options = {}) {
  const requestedDir = path.resolve(options.repoDir ?? process.cwd());
  const topLevel = tryExec(
    'git',
    ['rev-parse', '--show-toplevel'],
    requestedDir,
  );
  const repoDir = topLevel ? path.resolve(topLevel) : requestedDir;
  const homeDir = options.homeDir ?? require('node:os').homedir();

  return {
    repoDir,
    homeDir,
    harnesses: detectHarnesses(repoDir, homeDir),
    packageManager: detectPackageManager(repoDir),
    ecosystems: detectEcosystems(repoDir),
    scripts: detectScripts(repoDir),
    git: detectGit(repoDir),
    hasWorkflows: isDir(path.join(repoDir, '.github', 'workflows')),
    hasConfig: isFile(path.join(repoDir, '.activeloom-config.yml')),
  };
}

/**
 * Pick the harnesses a command should act on.
 *
 * @param {{harnesses: ReturnType<typeof detectHarnesses>}} facts
 * @param {readonly string[]} requested  ids passed with `--harness`
 * @param {object} options
 * @param {boolean} [options.preferRepo]  repo evidence outranks machine evidence
 * @param {string} options.noEvidenceReason  phrasing for the Claude Code default
 * @returns {{ids: string[], reason: string}}
 */
function chooseHarnesses(
  facts,
  requested,
  { preferRepo = false, noEvidenceReason },
) {
  if (requested.length > 0) {
    const known = new Set(HARNESSES.map((h) => h.id));
    const unknown = requested.filter((id) => !known.has(id));
    if (unknown.length > 0) {
      throw new Error(
        `unknown harness ${unknown.join(', ')}. Known: ${[...known].join(', ')}.`,
      );
    }
    return { ids: [...new Set(requested)], reason: 'requested with --harness' };
  }

  if (preferRepo) {
    const inRepo = facts.harnesses.filter((h) => h.inRepo).map((h) => h.id);
    if (inRepo.length > 0)
      return { ids: inRepo, reason: 'already checked into this repo' };
  }

  const onMachine = facts.harnesses
    .filter((h) => h.onMachine || h.cliInstalled)
    .map((h) => h.id);
  if (onMachine.length > 0)
    return { ids: onMachine, reason: 'detected on this machine' };

  return { ids: ['claude'], reason: noEvidenceReason };
}

module.exports = {
  detect,
  detectHarnesses,
  chooseHarnesses,
  detectPackageManager,
  detectEcosystems,
  detectScripts,
  detectGit,
  onPath,
  HARNESSES,
};
