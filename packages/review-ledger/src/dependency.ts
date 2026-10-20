import type { ChangedFile } from './types.js';

/**
 * Recognise a changed file whose whole change is a dependency pin.
 *
 * A dependency update is routed to a dependency analysis rather than a review
 * chain, so this decides whether a review chain is skipped. It therefore fails
 * closed: a file is a pin change only when its kind is known and every changed
 * line is one this module recognises. Anything else — a script edited beside a
 * version, a new workflow step, a manifest with no line content supplied —
 * keeps the normal review route.
 */

/** Lockfiles: any content change is a dependency resolution change. */
const LOCKFILE_BASENAMES: ReadonlySet<string> = new Set([
  'pnpm-lock.yaml',
  'package-lock.json',
  'npm-shrinkwrap.json',
  'yarn.lock',
  'bun.lockb',
  'bun.lock',
  'deno.lock',
  'cargo.lock',
  'poetry.lock',
  'uv.lock',
  'pipfile.lock',
  'gemfile.lock',
  'composer.lock',
  'go.sum',
  'go.work.sum',
  'flake.lock',
  'podfile.lock',
  'pubspec.lock',
  'mix.lock',
  'package.resolved',
  'packages.lock.json',
  'gradle.lockfile',
  '.terraform.lock.hcl',
]);

/** Manifests whose dependency entries are version lines. */
const MANIFEST_BASENAMES: ReadonlySet<string> = new Set([
  'package.json',
  'pnpm-workspace.yaml',
  'deno.json',
  'deno.jsonc',
  'composer.json',
  'pyproject.toml',
  'pipfile',
  'setup.cfg',
  'cargo.toml',
  'go.mod',
  'go.work',
  'gemfile',
  'podfile',
  'pubspec.yaml',
  'pom.xml',
  'build.gradle',
  'build.gradle.kts',
  'mix.exs',
]);

const REQUIREMENTS_FILE = /^requirements(-[^.]+)?\.(txt|in)$/;

/** Keys that name the package's own release, not a dependency. */
const OWN_VERSION_KEYS: ReadonlySet<string> = new Set(['version']);

/** A version constraint, range, or exact pin; must contain a digit. */
const VERSION_VALUE =
  /^(?=[^\d]*\d)[\^~<>=!*vV]*\s*\d[\w.*+-]*(\s*(\|\||,|\s)\s*[\^~<>=!]*\s*v?\d[\w.*+-]*)*$/;

/** Workspace and alias protocols a manifest uses in place of a range. */
const PROTOCOL_VALUE = /^(workspace|catalog|npm|jsr):\S*$/;

/** `name@version`, as in a package-manager pin. */
const AT_VERSION_VALUE = /^@?[\w./-]+@v?\d[\w.+-]*(\+sha\d*\.[0-9a-f]+)?$/;

/** A line that carries only structure: braces, brackets, commas. */
const STRUCTURE_ONLY = /^[[\]{}(),]*$/;

/** Changed manifest lines, without the diff `+` or `-`. */
function isVersionValue(raw: string): boolean {
  const value = raw.trim().replace(/^["']|["']$/g, '');
  return (
    VERSION_VALUE.test(value) ||
    PROTOCOL_VALUE.test(value) ||
    AT_VERSION_VALUE.test(value)
  );
}

/**
 * A `key: value` or `key = value` dependency entry with a version value.
 *
 * Covers JSON, YAML (including a pnpm catalog), and TOML. A TOML inline table
 * qualifies when its `version` field is a version value.
 */
function isKeyedVersionLine(line: string): boolean {
  const match =
    /^(?:-\s+)?["']?(@?[\w][\w@./:-]*)["']?\s*[:=]\s*(.+?)\s*,?$/.exec(line);
  if (!match) {
    return false;
  }
  const key = match[1]!.toLowerCase();
  const value = match[2]!;
  if (OWN_VERSION_KEYS.has(key)) {
    return false;
  }
  if (value.startsWith('{')) {
    const inline = /\bversion\s*=\s*("[^"]*"|'[^']*')/.exec(value);
    return inline !== null && isVersionValue(inline[1]!);
  }
  return isVersionValue(value);
}

function isRequirementLine(line: string): boolean {
  if (line.startsWith('#') || /^--hash=\S+(\s*\\)?$/.test(line)) {
    return true;
  }
  return /^[A-Za-z0-9][\w.\-[\],]*\s*(===|==|>=|<=|~=|!=|<|>)\s*[\w.*+!-]+(\s*,\s*(===|==|>=|<=|~=|!=|<|>)\s*[\w.*+!-]+)*(\s*;[^\\]*)?(\s*\\)?$/.test(
    line,
  );
}

function isGoModLine(line: string): boolean {
  return (
    line.startsWith('//') ||
    /^(require\s+)?[\w.~/-]+\s+v\d[\w.+-]*(\s*\/\/\s*indirect)?$/.test(line) ||
    /^(go|toolchain)\s+(go)?\d[\w.]*$/.test(line)
  );
}

function isMavenLine(line: string): boolean {
  return /^<version>\s*v?\d[\w.+-]*\s*<\/version>$/.test(line);
}

function isGradleLine(line: string): boolean {
  return /^\w+\s*\(?\s*["'][\w.-]+:[\w.-]+:v?\d[\w.+-]*["']\s*\)?$/.test(line);
}

function isGemLine(line: string): boolean {
  return /^(gem|pod)\s+["'][\w.-]+["']\s*,\s*["'][~<>=!\s\d.]+["'](\s*,\s*["'][~<>=!\s\d.]+["'])*$/.test(
    line,
  );
}

function isMixLine(line: string): boolean {
  return /^\{:\w+,\s*"[~<>=\s\d.]+"\}\s*,?$/.test(line);
}

function isManifestLine(name: string, line: string): boolean {
  if (STRUCTURE_ONLY.test(line)) {
    return true;
  }
  if (name === 'go.mod' || name === 'go.work') {
    return isGoModLine(line);
  }
  if (name === 'pom.xml') {
    return isMavenLine(line);
  }
  if (name === 'build.gradle' || name === 'build.gradle.kts') {
    return isGradleLine(line);
  }
  if (name === 'gemfile' || name === 'podfile') {
    return isGemLine(line);
  }
  if (name === 'mix.exs') {
    return isMixLine(line);
  }
  if (name === 'setup.cfg' || name === 'pyproject.toml' || name === 'pipfile') {
    // A PEP 508 string in a list, or a requirement in a setup.cfg block.
    const quoted = /^["']([^"']+)["']\s*,?$/.exec(line);
    if (quoted && isRequirementLine(quoted[1]!.trim())) {
      return true;
    }
    if (name === 'setup.cfg' && isRequirementLine(line)) {
      return true;
    }
  }
  if (line.startsWith('#') && !name.endsWith('.json')) {
    return true;
  }
  return isKeyedVersionLine(line);
}

/** An Actions `uses:` reference pinned to a tag or commit. */
function isActionPinLine(line: string): boolean {
  return /^(-\s+)?uses:\s*["']?[\w.-]+\/[\w./-]+@[\w.-]+["']?(\s+#.*)?$/.test(
    line,
  );
}

/** A container image reference, pinned by tag, digest, or both. */
function isImageReference(value: string): boolean {
  return /^["']?[\w.-]+(:\d+)?(\/[\w.-]+)*(:[\w.-]+)?(@sha256:[0-9a-f]{64})?["']?$/.test(
    value,
  );
}

function isImageLine(line: string): boolean {
  const match = /^(-\s+)?image:\s*(\S+)(\s+#.*)?$/.exec(line);
  return match !== null && isImageReference(match[2]!);
}

/** A Helm-style values entry for an image tag or digest. */
function isImageTagLine(line: string): boolean {
  return /^(tag|digest):\s*["']?(v?\d[\w.+-]*|sha256:[0-9a-f]{64})["']?(\s+#.*)?$/.test(
    line,
  );
}

function isDockerfileFromLine(line: string): boolean {
  const match = /^FROM\s+(--platform=\S+\s+)?(\S+)(\s+AS\s+[\w.-]+)?$/i.exec(
    line,
  );
  return match !== null && isImageReference(match[2]!);
}

function isWorkflowPath(path: string, name: string): boolean {
  return (
    path.startsWith('.github/workflows/') ||
    path.startsWith('.github/actions/') ||
    name === 'action.yml' ||
    name === 'action.yaml'
  );
}

function lineKind(
  path: string,
  name: string,
): ((line: string) => boolean) | 'lockfile' | null {
  if (LOCKFILE_BASENAMES.has(name)) {
    return 'lockfile';
  }
  if (MANIFEST_BASENAMES.has(name)) {
    return (line) => isManifestLine(name, line);
  }
  if (REQUIREMENTS_FILE.test(name)) {
    return isRequirementLine;
  }
  if (/^dockerfile(\.|$)/.test(name) || /\.dockerfile$/.test(name)) {
    return isDockerfileFromLine;
  }
  if (name.endsWith('.yml') || name.endsWith('.yaml')) {
    if (isWorkflowPath(path, name)) {
      return (line) => isActionPinLine(line) || isImageLine(line);
    }
    if (/^values[\w.-]*\.ya?ml$/.test(name)) {
      return (line) => isImageLine(line) || isImageTagLine(line);
    }
    return isImageLine;
  }
  return null;
}

/**
 * Whether one changed file is a dependency pin change and nothing else.
 *
 * `changedLines` holds the added and deleted lines without their diff prefix;
 * blank lines are ignored. A lockfile qualifies on its name, and a submodule
 * gitlink on its mode. Every other kind needs its lines, and a file with no
 * lines supplied, or with no changed line at all, does not qualify.
 */
export function isDependencyPinChange(file: ChangedFile): boolean {
  if (file.submodule === true) {
    return true;
  }
  const path = file.path.replace(/\\/g, '/').replace(/^\.\//, '');
  const name = path.slice(path.lastIndexOf('/') + 1).toLowerCase();
  const kind = lineKind(path, name);
  if (kind === null) {
    return false;
  }
  if (kind === 'lockfile') {
    return true;
  }
  const lines = (file.changedLines ?? [])
    .map((line) => line.trim())
    .filter((line) => line !== '');
  if (lines.length === 0) {
    return false;
  }
  return lines.every(kind);
}
