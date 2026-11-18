import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseCliArgs, runCli } from '../cli.js';
import { DefaultGitHubRunner } from '../github.js';
import { SILENT_FORCE_ENV } from '../local-store.js';
import { COMMAND_NAMES, commandSynopsis } from '../usage.js';

const here = new URL('.', import.meta.url).pathname;
const cliSource = readFileSync(join(here, '..', 'cli.ts'), 'utf8');
const protocol = readFileSync(
  join(here, '..', '..', 'protocol', 'local-review-ledger.md'),
  'utf8',
);

const BOOLEAN_FLAGS = new Set([
  '--file-level',
  '--truncated',
  '--dry-run',
  '--force-review',
  '--confirmed',
]);

function flagsOf(command: string): string[] {
  return [...new Set(commandSynopsis(command)!.match(/--[a-z0-9-]+/g) ?? [])];
}

function captureStdout(run: () => number): { code: number; out: string } {
  let out = '';
  const spy = vi
    .spyOn(process.stdout, 'write')
    .mockImplementation((chunk: string | Uint8Array) => {
      out += String(chunk);
      return true;
    });
  try {
    return { code: run(), out };
  } finally {
    spy.mockRestore();
  }
}

function errorOf(run: () => unknown): string {
  try {
    run();
  } catch (error) {
    return (error as Error).message;
  }
  throw new Error('expected the command to fail');
}

describe('CLI usage', () => {
  it('documents exactly the subcommands the dispatcher handles', () => {
    const dispatched = [...cliSource.matchAll(/^ {4}case '([a-z-]+)':/gm)].map(
      (m) => m[1]!,
    );
    expect(new Set(COMMAND_NAMES)).toEqual(new Set(dispatched));
  });

  it.each(COMMAND_NAMES.flatMap((c) => flagsOf(c).map((f) => [c, f])))(
    '%s documents %s, a flag the parser accepts',
    (command, flag) => {
      const argv = BOOLEAN_FLAGS.has(flag)
        ? [command, flag]
        : [command, flag, 'value'];
      expect(() => parseCliArgs(argv)).not.toThrowError(/unknown argument/);
    },
  );

  it('lists every subcommand on bare --help', () => {
    const { code, out } = captureStdout(() => runCli(['--help']));
    expect(code).toBe(0);
    for (const name of COMMAND_NAMES) expect(out).toContain(`  ${name} `);
  });

  it.each([
    [['post-finding', '--help']],
    [['post-finding', '-h']],
    [['help', 'post-finding']],
    [['--repo', 'a/b', 'post-finding', '--help']],
  ])('prints one subcommand usage for %j', (argv) => {
    const { code, out } = captureStdout(() => runCli(argv));
    expect(code).toBe(0);
    expect(out).toMatch(/^usage: review-ledger post-finding /);
    expect(out).toContain('--content-file <file>');
    expect(out).toContain('There is no hash flag.');
  });

  it('answers help for an unknown subcommand with the list', () => {
    const { code, out } = captureStdout(() => runCli(['help', 'nope']));
    expect(code).toBe(0);
    expect(out).toContain('unknown subcommand: nope');
    expect(out).toContain('  write-result ');
  });

  it('names the real flags when a guessed one is refused', () => {
    const message = errorOf(() =>
      runCli(['post-finding', '--repo', 'a/b', '--sha256', 'abc']),
    );
    expect(message).toMatch(/^unknown argument: --sha256\n/);
    expect(message).toContain('usage: review-ledger post-finding ');
    expect(message).toContain('--content-file <file>');
    expect(message).toContain('review-ledger post-finding --help');
  });

  it('attaches usage to a missing-argument refusal', () => {
    const message = errorOf(() => runCli(['write-result', '--repo', 'a/b']));
    expect(message).toMatch(/^write-result missing required arguments\n/);
    expect(message).toContain('--before <full-sha>');
    expect(message).toContain('--classification <minor|material>');
  });

  it('attaches usage to an invalid enum value', () => {
    const message = errorOf(() =>
      runCli(['write-result', '--classification', 'clean']),
    );
    expect(message).toMatch(/--classification must be one of: minor, material/);
    expect(message).toContain('usage: review-ledger write-result ');
  });

  it('lists subcommands for an unknown subcommand', () => {
    const message = errorOf(() => runCli(['post-result']));
    expect(message).toMatch(/^unknown command: post-result\n/);
    expect(message).toContain('write-result');
  });

  it('leaves a ledger refusal without usage', () => {
    const message = errorOf(() =>
      runCli([
        'write-blocked-result',
        '--head',
        'a'.repeat(40),
        '--engine',
        'gemini',
        '--round',
        '1',
        '--base',
        'b'.repeat(40),
        '--before',
        'c'.repeat(40),
        '--result-file',
        '/nonexistent-dir/result.json',
        '--blocker-file',
        '/nonexistent-dir/blocker.txt',
      ]),
    );
    expect(message).not.toContain('usage: review-ledger');
  });

  // Every helper invocation the protocol writes out must be one the CLI
  // accepts, so a reader who copies it never meets an unknown flag.
  const documented = [
    ...protocol.matchAll(/^ *node <ledger-helper> ([a-z-]+)((?:.*\\\n)*.*)$/gm),
  ].map((m) => [m[1]!, [...new Set(m[2]!.match(/--[a-z0-9-]+/g) ?? [])]]);

  it('finds the protocol invocations', () => {
    expect(documented.map(([command]) => command)).toEqual(
      expect.arrayContaining([
        'post-finding',
        'dispose',
        'write-result',
        'validate-result',
        'export-threads',
        'attest',
      ]),
    );
  });

  it.each(documented)(
    'protocol invocation of %s uses only documented flags',
    (command, flags) => {
      expect(commandSynopsis(command as string)).toBeDefined();
      expect(flagsOf(command as string)).toEqual(
        expect.arrayContaining(flags as string[]),
      );
    },
  );
});

describe('export-threads', () => {
  const REPO = 'owner/repo';
  const page = {
    data: {
      repository: {
        pullRequest: {
          reviewThreads: {
            nodes: [
              {
                id: 'PRRT_1',
                isResolved: true,
                repository: { nameWithOwner: REPO },
                pullRequest: { number: 7 },
                comments: {
                  nodes: [
                    {
                      databaseId: 11,
                      body: 'x',
                      createdAt: '2026-11-17T00:00:00Z',
                      author: { login: 'operator' },
                    },
                  ],
                  pageInfo: { hasNextPage: false },
                },
              },
            ],
            pageInfo: { hasNextPage: false, endCursor: null },
          },
        },
      },
    },
  };
  let dir: string;

  beforeEach(() => {
    delete process.env[SILENT_FORCE_ENV];
    dir = mkdtempSync(join(tmpdir(), 'export-threads-'));
    vi.spyOn(DefaultGitHubRunner.prototype, 'runGh').mockImplementation(
      (args) =>
        args[0] === 'api' && args[1] === 'graphql'
          ? JSON.stringify([page])
          : '[]',
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    rmSync(dir, { recursive: true, force: true });
  });

  it('writes a sealed owner-only snapshot and prints its digest', () => {
    const file = join(dir, 'threads.json');
    const { code, out } = captureStdout(() =>
      runCli([
        'export-threads',
        '--repo',
        REPO,
        '--pr',
        '7',
        '--threads-file',
        file,
      ]),
    );
    expect(code).toBe(0);
    const printed = JSON.parse(out) as {
      path: string;
      sha256: string;
      threads: number;
    };
    expect(printed.path).toBe(file);
    expect(printed.threads).toBe(1);
    expect(printed.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(JSON.parse(readFileSync(file, 'utf8'))).toEqual([page]);
    expect(statSync(file).mode & 0o777).toBe(0o600);
  });

  it('refuses to overwrite an existing file', () => {
    const file = join(dir, 'threads.json');
    const argv = [
      'export-threads',
      '--repo',
      REPO,
      '--pr',
      '7',
      '--threads-file',
      file,
    ];
    captureStdout(() => runCli(argv));
    expect(() => captureStdout(() => runCli(argv))).toThrowError(/EEXIST/);
  });

  it('requires a destination', () => {
    expect(
      errorOf(() => runCli(['export-threads', '--repo', REPO, '--pr', '7'])),
    ).toMatch(/^export-threads requires --repo, --pr, and --threads-file\n/);
  });
});
