import { getGitHubRunner } from './github.js';
import { parse, type ParserOptions } from '@babel/parser';

/**
 * Whether a Git range altered an executing surface ('behavioral' fails closed).
 */
export type RangeEffect = 'behavioral' | 'non-behavioral';

/**
 * Extensions treated as documentation, configuration, or fixture data.
 */
const DOCS_CONFIG_EXTENSIONS: ReadonlySet<string> = new Set([
  '.md',
  '.mdx',
  '.txt',
  '.rst',
  '.yml',
  '.yaml',
  '.json',
  '.toml',
  '.ini',
  '.csv',
]);

const DOCS_CONFIG_BASENAMES: ReadonlySet<string> = new Set([
  'LICENSE',
  'NOTICE',
  'CHANGELOG',
  'README',
  '.gitignore',
  '.gitattributes',
  '.env.example',
]);

/** Prompt directories across supported engines. */
const PROMPT_SURFACE_RE = /(^|\/)\.(claude|codex|agents)\//;

/** Paths whose content executes or gates execution regardless of extension. */
const EXECUTING_PATH_RES: readonly RegExp[] = [
  /(^|\/)\.github\/workflows\//,
  /(^|\/)\.github\/actions\//,
  /(^|\/)CODEOWNERS$/,
  /(^|\/)package\.json$/,
  /(^|\/)(pnpm-lock\.yaml|package-lock\.json|yarn\.lock)$/,
];

const DOCS_DIR_RE = /(^|\/)docs\//;

/** Test runner paths; modifications alone do not carry production behavior. */
const TEST_PATH_RES: readonly RegExp[] = [
  /(^|\/)__tests__\//,
  /(^|\/)tests?\//,
  /(^|\/)spec\//,
  /\.(test|spec)\.[cm]?[jt]sx?$/,
  /(^|\/)test_[^/]+\.py$/,
  /_test\.(go|py|rb)$/,
];

interface CommentSyntax {
  line: readonly string[];
  block: readonly (readonly [string, string])[];
}

/** Unambiguous line and block comment markers by file extension. */
const COMMENT_SYNTAX: ReadonlyMap<string, CommentSyntax> = new Map([
  ['.ts', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.tsx', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.js', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.jsx', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.mjs', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.cjs', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.go', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.rs', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.java', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.kt', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.swift', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.c', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.h', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.cpp', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.cs', { line: ['//'], block: [['/*', '*/'] as const] }],
  ['.tf', { line: ['#', '//'], block: [['/*', '*/'] as const] }],
  ['.tfvars', { line: ['#', '//'], block: [['/*', '*/'] as const] }],
  ['.hcl', { line: ['#', '//'], block: [['/*', '*/'] as const] }],
  ['.py', { line: ['#'], block: [] }],
  ['.rb', { line: ['#'], block: [] }],
  ['.sh', { line: ['#'], block: [] }],
  ['.bash', { line: ['#'], block: [] }],
  ['.sql', { line: ['--'], block: [['/*', '*/'] as const] }],
]);

const JAVASCRIPT_EXTENSIONS = new Set([
  '.ts',
  '.tsx',
  '.mts',
  '.cts',
  '.js',
  '.jsx',
  '.mjs',
  '.cjs',
]);

/** Checked lexical range from parser tokens. */
interface SourceToken {
  start: number;
  end: number;
  label: string;
}

function sourceToken(value: unknown, length: number): SourceToken {
  if (typeof value !== 'object' || value === null) {
    throw new Error('parser token is not an object');
  }
  const token = value as Record<string, unknown>;
  const type = token['type'];
  const label =
    typeof type === 'string'
      ? type
      : typeof type === 'object' && type !== null && 'label' in type
        ? type.label
        : undefined;
  const start = token['start'];
  const end = token['end'];
  if (
    typeof label !== 'string' ||
    typeof start !== 'number' ||
    typeof end !== 'number' ||
    !Number.isInteger(start) ||
    !Number.isInteger(end) ||
    start < 0 ||
    end < start ||
    end > length
  ) {
    throw new Error('parser token has an invalid source range');
  }
  return { start, end, label };
}

function isDirective(value: string): boolean {
  // Preserve complete comments: tool and type directives can span lines, and
  // their delimiters matter. Hash headings and numeric references remain prose.
  return (
    /^\s*(?::|flow-include\b)/.test(value) ||
    /^\/|@|#[a-z_]|\b(?:webpack|turbopack)|\b(?:eslint|istanbul|c8|v8|prettier|jshint|jslint|tslint|vite|globals?|exported|sourceMappingURL|sourceURL|debugId)\b/i.test(
      value,
    )
  );
}

/** Parse source with explicit module extensions or reject ambiguous lexical structure. */
function parseSource(
  source: string,
  extension: string,
): ReturnType<typeof parse> {
  const options: ParserOptions = {
    sourceType: /\.[cm][jt]s$/.test(extension)
      ? extension.startsWith('.m')
        ? 'module'
        : 'commonjs'
      : 'unambiguous',
    annexB: false,
    attachComment: false,
    tokens: true,
    plugins: [
      'decorators-legacy',
      ...(/\.[cm]?tsx?$/.test(extension) ? ['typescript' as const] : []),
      ...(/\.(?:[cm]?jsx?|tsx)$/.test(extension) ? ['jsx' as const] : []),
    ],
  };
  const parsed = parse(source, options);
  if (parsed.program.sourceType !== 'module' && /<!--|-->/.test(source)) {
    throw new Error('script source contains an HTML-like comment marker');
  }
  if (
    options.sourceType === 'unambiguous' &&
    parsed.program.sourceType === 'script'
  ) {
    const module = parse(source, { ...options, sourceType: 'module' });
    const shape = (file: ReturnType<typeof parse>) => {
      const tokens: unknown = file.tokens;
      if (!Array.isArray(tokens)) {
        throw new Error('parser tokens are unavailable');
      }
      return JSON.stringify(
        tokens.map((value) => {
          const token = sourceToken(value, source.length);
          return [token.start, token.end, token.label];
        }),
      );
    };
    if (shape(parsed) !== shape(module)) {
      throw new Error('module and script parses disagree on tokens');
    }
  }
  return parsed;
}

/** Serialize executable tokens and directive gaps, ignoring ordinary comment changes. */
function commentSkeleton(source: string, extension: string): string {
  const parsed = parseSource(source, extension);
  const tokens: unknown = parsed.tokens;
  if (!Array.isArray(tokens)) throw new Error('parser tokens are unavailable');
  const comments = new Map<number, { end: number; directive: boolean }>();
  for (const comment of parsed.comments ?? []) {
    const span = sourceToken(comment, source.length);
    comments.set(span.start, {
      end: span.end,
      directive: isDirective(comment.value),
    });
  }
  let offset = 0;
  let lastEnd = 0;
  let seenComments = 0;
  let directiveInGap = false;
  const parts: Array<readonly [string, string, string | boolean]> = [];
  for (const value of tokens) {
    const token = sourceToken(value, source.length);
    if (token.start < lastEnd) throw new Error('parser tokens overlap');
    lastEnd = token.end;
    const comment = comments.get(token.start);
    if (comment?.end === token.end) {
      seenComments += 1;
      directiveInGap ||= comment.directive;
      continue;
    }
    const gap = source.slice(offset, token.start);
    parts.push([
      token.label,
      source.slice(token.start, token.end),
      directiveInGap
        ? gap
        : parts.length > 0 &&
          token.label !== 'eof' &&
          /[\n\r\u2028\u2029]/.test(gap),
    ]);
    offset = token.end;
    directiveInGap = false;
  }
  if (
    seenComments !== comments.size ||
    parts.at(-1)?.[0] !== 'eof' ||
    lastEnd !== source.length
  ) {
    throw new Error('parser tokens did not cover the complete source');
  }
  return JSON.stringify(parts);
}

function isCommentOnlySource(
  before: string,
  after: string,
  extension: string,
): boolean {
  try {
    return (
      commentSkeleton(before, extension) === commentSkeleton(after, extension)
    );
  } catch {
    return false;
  }
}

function extensionOf(path: string): string {
  const base = path.slice(path.lastIndexOf('/') + 1);
  const dot = base.lastIndexOf('.');
  return dot <= 0 ? '' : base.slice(dot).toLowerCase();
}

function basenameOf(path: string): string {
  return path.slice(path.lastIndexOf('/') + 1);
}

function isPromptSurface(path: string): boolean {
  return PROMPT_SURFACE_RE.test(`/${path}`);
}

function isExecutingPath(path: string): boolean {
  return EXECUTING_PATH_RES.some((pattern) => pattern.test(`/${path}`));
}

function isDocsOrConfig(path: string): boolean {
  if (isPromptSurface(path) || isExecutingPath(path)) {
    return false;
  }
  if (
    DOCS_DIR_RE.test(`/${path}`) &&
    !COMMENT_SYNTAX.has(extensionOf(path)) &&
    !JAVASCRIPT_EXTENSIONS.has(extensionOf(path))
  ) {
    return true;
  }
  const base = basenameOf(path);
  if (DOCS_CONFIG_BASENAMES.has(base)) {
    return true;
  }
  return DOCS_CONFIG_EXTENSIONS.has(extensionOf(path));
}

function isTestPath(path: string): boolean {
  if (isPromptSurface(path) || isExecutingPath(path)) {
    return false;
  }
  return TEST_PATH_RES.some((pattern) => pattern.test(`/${path}`));
}

/** Decide whether a changed line is a comment or whitespace, advancing block state. */
function classifyLine(
  text: string,
  syntax: CommentSyntax,
  inBlock: boolean,
): { inert: boolean; inBlock: boolean } {
  let rest = text.trim();

  if (inBlock) {
    const close = syntax.block[0]?.[1];
    if (close === undefined) {
      return { inert: false, inBlock: false };
    }
    const end = rest.indexOf(close);
    if (end === -1) {
      return { inert: true, inBlock: true };
    }
    rest = rest.slice(end + close.length).trim();
    if (rest === '') {
      return { inert: true, inBlock: false };
    }
    return { inert: false, inBlock: false };
  }

  if (rest === '') {
    return { inert: true, inBlock: false };
  }

  if (syntax.line.some((marker) => rest.startsWith(marker))) {
    return { inert: true, inBlock: false };
  }

  for (const [open, close] of syntax.block) {
    if (!rest.startsWith(open)) {
      continue;
    }
    const end = rest.indexOf(close, open.length);
    if (end === -1) {
      return { inert: true, inBlock: true };
    }
    const tail = rest.slice(end + close.length).trim();
    return { inert: tail === '', inBlock: false };
  }

  return { inert: false, inBlock: false };
}

/** Check whether every added and removed line in a diff hunk is a comment. */
function isCommentOnlyPatch(patch: string, syntax: CommentSyntax): boolean {
  let leftBlock = false;
  let rightBlock = false;
  let inHunk = false;

  for (const raw of patch.split(/\r?\n/)) {
    if (raw.startsWith('@@')) {
      inHunk = true;
      leftBlock = false;
      rightBlock = false;
      continue;
    }
    if (!inHunk || raw.startsWith('\\ No newline')) {
      continue;
    }
    const prefix = raw.slice(0, 1);
    const text = raw.slice(1);
    if (prefix === '-') {
      const result = classifyLine(text, syntax, leftBlock);
      leftBlock = result.inBlock;
      if (!result.inert) {
        return false;
      }
    } else if (prefix === '+') {
      const result = classifyLine(text, syntax, rightBlock);
      rightBlock = result.inBlock;
      if (!result.inert) {
        return false;
      }
    }
  }

  return true;
}

/** Classify whether a git range contains behavioral changes or only inert edits. */
export function classifyRangeEffect(
  before: string,
  after: string,
): RangeEffect {
  if (before === after) {
    return 'non-behavioral';
  }

  const runner = getGitHubRunner();
  if (!runner.runGit) {
    return 'behavioral';
  }

  const status = runner
    .runGit(['diff', '--name-status', '--no-renames', `${before}..${after}`])
    .trim();
  if (status === '') {
    return 'non-behavioral';
  }

  const paths: Array<{ code: string; path: string }> = [];
  for (const row of status.split(/\r?\n/)) {
    if (row === '') {
      continue;
    }
    const [code, path] = row.split('\t', 2);
    if (code === undefined || path === undefined || !/^[AMD]$/.test(code)) {
      return 'behavioral';
    }
    paths.push({ code, path });
  }

  for (const { code, path } of paths) {
    if (isDocsOrConfig(path) || isTestPath(path)) {
      continue;
    }
    const extension = extensionOf(path);
    if (JAVASCRIPT_EXTENSIONS.has(extension)) {
      if (code !== 'M') return 'behavioral';
      try {
        const summary = runner.runGit([
          'diff',
          '--summary',
          '--no-renames',
          `${before}..${after}`,
          '--',
          path,
        ]);
        if (
          summary.trim() !== '' ||
          !isCommentOnlySource(
            runner.runGit(['show', `${before}:${path}`]),
            runner.runGit(['show', `${after}:${path}`]),
            extension,
          )
        )
          return 'behavioral';
      } catch {
        return 'behavioral';
      }
      continue;
    }
    const syntax = COMMENT_SYNTAX.get(extension);
    if (syntax === undefined) {
      return 'behavioral';
    }
    const patch = runner.runGit([
      'diff',
      '--unified=0',
      '--no-renames',
      `${before}..${after}`,
      '--',
      path,
    ]);
    if (!isCommentOnlyPatch(patch, syntax)) {
      return 'behavioral';
    }
  }

  return 'non-behavioral';
}
