'use strict';

/**
 * Deterministic environment detection. No model in the loop.
 *
 * Everything here is a file test, a PATH lookup, or a `git`/`gh` invocation
 * whose output is parsed structurally. That constraint is deliberate: `init`
 * writes files into someone's repository, and a wrong-but-plausible guess from
 * a model is far more expensive to notice than a blank we asked about. The
 * agent-side `onboard` skill is where judgement belongs — it drafts the prose
 * fields for a human to confirm, and it is never in this path.
 *
 * Every field is either a verified fact or `null`. Nothing is inferred from
 * something else being present.
 */

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

/**
 * Harnesses this CLI knows how to install, in manifest declaration order.
 *
 * `id` is the name a consumer config's `harnesses:` list uses and `root` is the
 * prompt directory it owns — the two differ for gemini (`gemini` / `.agents`),
 * which is exactly the kind of detail a second copy gets wrong. This table has
 * to exist statically because `add` reads it before any upstream tree has been
 * fetched, so `tests/cli/harness-table.test.js` asserts it against
 * `scripts/sync-targets.yml` and fails the build if the two ever disagree.
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
 * Run a command and return trimmed stdout, or `null` if it fails for any
 * reason. Detection is advisory by construction, so a missing tool, a
 * non-zero exit, and a timeout are all the same answer: we do not know.
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
 * Is `name` an executable on PATH?
 *
 * `command -v` rather than `which`: `which` is not installed everywhere and
 * reports differently across platforms, while `command -v` is POSIX shell
 * builtin behaviour. On Windows we fall back to `where`.
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
 * Which harnesses does this machine and this repo show evidence of?
 *
 * Two independent signals, reported separately rather than merged. A repo that
 * already carries `.codex/` wants codex synced even if this particular
 * developer has no codex CLI installed, and a developer with the claude CLI
 * does not thereby make claude the right choice for a repo the whole team
 * shares. Merging them would silently pick a side; the caller decides.
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
 * Package manager, from the lockfile actually present.
 *
 * Lockfile rather than a `packageManager` field: the lockfile is what the repo
 * demonstrably uses, whereas the field is a declaration that may be aspirational
 * or stale. Order matters only for the pathological repo carrying two, where
 * the more specific tool wins over npm's default.
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
 * A repo may legitimately be several of these at once (a Python service with a
 * TypeScript frontend), so this returns every match rather than picking one.
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
 * Test and lint entry points, read from `package.json` scripts.
 *
 * Only what is declared: a repo whose tests run via a bare `pytest` with no
 * script wrapper reports `null` here, and `onboard` asks. Guessing `pytest`
 * from the presence of a `tests/` directory is exactly the plausible-but-wrong
 * answer this module refuses to produce.
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
    // A malformed package.json is the repo's problem to fix, not a reason to
    // abort onboarding — report unknown and carry on.
    return empty;
  }
}

/**
 * Git and GitHub facts.
 *
 * `owner/repo` is parsed from the `origin` remote and normalised across the
 * SSH and HTTPS forms. A repo with no origin, or an origin that is not GitHub,
 * yields `null` — which is what downgrades tiers 2 and 3 out of reach, since
 * both write a GitHub Actions workflow.
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
    // Both forms in one pattern, anchored at the host so a path component
    // that merely contains "github.com" cannot match. The `.git` suffix is
    // optional because both forms appear in the wild without it.
    const m =
      /^(?:git@github\.com:|(?:ssh|https?):\/\/(?:[^@/]*@)?github\.com\/)([^/]+)\/(.+?)(?:\.git)?$/.exec(
        remote,
      );
    if (m) slug = `${m[1]}/${m[2]}`;
  }

  // The remote's HEAD, not the local branch: sync PRs target a branch of the
  // *repository*, and the local checkout may be sitting on anything.
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
 * Collect every fact the CLI can establish without asking or guessing.
 *
 * @param {object} [options]
 * @param {string} [options.repoDir] Directory to inspect. Defaults to cwd.
 * @param {string} [options.homeDir] Home directory. Injectable for tests.
 */
function detect(options = {}) {
  const repoDir = options.repoDir ?? process.cwd();
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
 * One resolver rather than one per command: `add` and `init` differ only in
 * whether repo evidence outranks machine evidence and in how they phrase the
 * no-evidence default. The `--harness` validation, the known-id set, and the
 * fall back to Claude Code are the same decision in both, and a second copy of
 * them drifts silently — the error text and the default harness would have to
 * be changed in two files.
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
    return { ids: [...requested], reason: 'requested with --harness' };
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
