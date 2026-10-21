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

/**
 * A single version token: digits and dots (or wildcards), with optional
 * pre-release or build metadata after a dot, or a PEP 440 pre/post/dev suffix.
 */
const VERSION_TOKEN =
  /(?:[*xX]|\d+(?:(?:\.(?:\d+|[*xX]))+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?|(?:a|b|rc|alpha|beta|pre|post|dev)\d*)?)/;

/** A version constraint, range, or exact pin; must contain a digit. */
const VERSION_VALUE = new RegExp(
  `^(?=[^\\d]*\\d)[\\^~<>=!*vV]*\\s*${VERSION_TOKEN.source}(\\s*(\\|\\||,|\\s)\\s*[\\^~<>=!]*\\s*v?${VERSION_TOKEN.source})*$`,
);

/** Workspace and alias protocols a manifest uses in place of a range. */
const PROTOCOL_VALUE =
  /^(?:workspace:(?:[*^~]|[\^~<>=!vV]*\d[\dA-Za-z.*+-]*)|catalog:[\w.-]*|(?:npm|jsr):@?[\w./-]+@[\^~<>=!*vV\d][\dA-Za-z.*+-]*)$/;

/** `name@version`, as in a package-manager pin. */
const AT_VERSION_VALUE =
  /^@?[\w./-]+@v?\d+(?:\.\d+)*(?:-[0-9A-Za-z.-]+)?(\+(?:sha\d*\.[0-9a-f]+|[0-9A-Za-z.-]+))?$/;

/** A line that carries only structure: braces, brackets, commas. */
const STRUCTURE_ONLY = /^[[\]{}(),]*$/;

function requiresQuotedManifestValue(name: string): boolean {
  return (
    name.endsWith('.json') ||
    name.endsWith('.jsonc') ||
    name.endsWith('.toml') ||
    name === 'pipfile'
  );
}

/** Changed manifest lines, without the diff `+` or `-`. */
function isVersionValue(raw: string, requireQuoted = false): boolean {
  const trimmed = raw.trim().replace(/,$/, '').trim();
  const isQuoted =
    (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"));
  if (requireQuoted && !isQuoted) {
    return false;
  }
  const value = isQuoted ? trimmed.slice(1, -1).trim() : trimmed;
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
 * qualifies when its `version` field is a version value and it carries no git
 * or path source override.
 */
function isKeyedVersionLine(name: string, line: string): boolean {
  const match =
    /^(?:-\s+)?(?:"(@?[\w][\w@./:-]*)"|'(@?[\w][\w@./:-]*)'|(@?[\w][\w@./-]*))\s*[:=]\s*(.+)$/.exec(
      line,
    );
  if (!match) {
    return false;
  }
  const key = (match[1] ?? match[2] ?? match[3])!.toLowerCase();
  const value = match[4]!.trim().replace(/,$/, '').trim();
  if (OWN_VERSION_KEYS.has(key)) {
    return false;
  }
  if (value.startsWith('{')) {
    if (!value.endsWith('}') || /\b(git|path|branch|rev|tag)\s*=/.test(value)) {
      return false;
    }
    const inline = /\bversion\s*=\s*("[^"]*"|'[^']*')/.exec(value);
    return inline !== null && isVersionValue(inline[1]!, true);
  }
  return isVersionValue(value, requiresQuotedManifestValue(name));
}

function isRequirementPinLine(line: string): boolean {
  return /^[A-Za-z0-9][\w.\-[\],]*\s*(===|==|>=|<=|~=|!=|<|>)\s*[\w.*+!-]+(\s*,\s*(===|==|>=|<=|~=|!=|<|>)\s*[\w.*+!-]+)*(\s*;[^\\]*)?(\s*\\)?$/.test(
    line,
  );
}

function isRequirementAuxiliaryLine(line: string): boolean {
  return line.startsWith('#') || /^--hash=\S+(\s*\\)?$/.test(line);
}

function isGoModPinLine(line: string): boolean {
  return (
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

function isManifestPinLine(name: string, line: string): boolean {
  if (name === 'go.mod' || name === 'go.work') {
    return isGoModPinLine(line);
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
    if (quoted && isRequirementPinLine(quoted[1]!.trim())) {
      return true;
    }
    if (name === 'setup.cfg' && isRequirementPinLine(line)) {
      return true;
    }
  }
  return isKeyedVersionLine(name, line);
}

function isManifestAuxiliaryLine(name: string, line: string): boolean {
  if (STRUCTURE_ONLY.test(line)) {
    return true;
  }
  if (name === 'go.mod' || name === 'go.work') {
    return line.startsWith('//');
  }
  return line.startsWith('#') && !name.endsWith('.json');
}

/** An Actions `uses:` reference pinned to a tag or commit. */
function isActionPinLine(line: string): boolean {
  return /^(-\s+)?uses:\s*["']?[\w.-]+\/[\w./-]+@(?:v?\d[\dA-Za-z.+-]*|[0-9a-f]{40}|[0-9a-f]{64})["']?(\s+#.*)?$/.test(
    line,
  );
}

/** A container image reference, pinned by tag, digest, or both. */
function isImageReference(value: string): boolean {
  return /^["']?[\w.-]+(:\d+(?=\/))?(\/[\w.-]+)*(?::[\w][\w.-]*(?:@sha256:[0-9a-f]{64})?|@sha256:[0-9a-f]{64})["']?$/.test(
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

interface LineClassifier {
  isPin: (line: string) => boolean;
  isAllowed: (line: string) => boolean;
}

function pinOnly(isPin: (line: string) => boolean): LineClassifier {
  return { isPin, isAllowed: isPin };
}

function lineKind(
  path: string,
  name: string,
): LineClassifier | 'lockfile' | null {
  if (LOCKFILE_BASENAMES.has(name)) {
    return 'lockfile';
  }
  if (MANIFEST_BASENAMES.has(name)) {
    return {
      isPin: (line) => isManifestPinLine(name, line),
      isAllowed: (line) =>
        isManifestPinLine(name, line) || isManifestAuxiliaryLine(name, line),
    };
  }
  if (REQUIREMENTS_FILE.test(name)) {
    return {
      isPin: isRequirementPinLine,
      isAllowed: (line) =>
        isRequirementPinLine(line) || isRequirementAuxiliaryLine(line),
    };
  }
  if (/^dockerfile(\.|$)/.test(name) || /\.dockerfile$/.test(name)) {
    return pinOnly(isDockerfileFromLine);
  }
  if (name.endsWith('.yml') || name.endsWith('.yaml')) {
    if (isWorkflowPath(path, name)) {
      return pinOnly((line) => isActionPinLine(line) || isImageLine(line));
    }
    if (/^values[\w.-]*\.ya?ml$/.test(name)) {
      return pinOnly((line) => isImageLine(line) || isImageTagLine(line));
    }
    return pinOnly(isImageLine);
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
  return lines.every(kind.isAllowed) && lines.some(kind.isPin);
}
