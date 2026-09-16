import { execFileSync } from 'node:child_process';
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { DefaultGitHubRunner, execFailureDetail } from '../github.js';
import { SUBPROCESS_MAX_BUFFER } from '../constants.js';

/**
 * Everything here drives the production runner against real child processes.
 *
 * The rest of the suite injects a GitHubRunner, which replaces the exec options
 * wholesale — so a mocked test cannot observe an output ceiling, and dropping
 * the `maxBuffer` options would leave it green. These tests put executable
 * `gh` and `git` shims on PATH instead and call `DefaultGitHubRunner`, so the
 * production exec path is what runs.
 */
describe('subprocess output ceiling', () => {
  const OVER_DEFAULT = 2 * 1024 * 1024; // Node's default ceiling is 1 MiB.
  let binDir: string;
  let originalPath: string | undefined;

  const installShim = (name: string, body: string): void => {
    const shim = join(binDir, name);
    writeFileSync(shim, `#!/usr/bin/env node\n${body}\n`);
    chmodSync(shim, 0o755);
  };

  beforeEach(() => {
    binDir = mkdtempSync(join(tmpdir(), 'review-ledger-bin-'));
    originalPath = process.env['PATH'];
    process.env['PATH'] = `${binDir}:${originalPath ?? ''}`;
  });

  afterEach(() => {
    if (originalPath === undefined) {
      delete process.env['PATH'];
    } else {
      process.env['PATH'] = originalPath;
    }
    rmSync(binDir, { recursive: true, force: true });
  });

  it('passes gh output larger than the default ceiling through runGh', () => {
    // A PR carrying a real ledger exceeds 1 MiB in review comments alone, which
    // is what made this fail on exactly the pull requests worth verifying.
    installShim('gh', `process.stdout.write('x'.repeat(${OVER_DEFAULT}))`);

    const stdout = new DefaultGitHubRunner().runGh(['api', 'user']);

    expect(stdout.length).toBe(OVER_DEFAULT);
  });

  it('passes git output larger than the default ceiling through runGit', () => {
    installShim('git', `process.stdout.write('y'.repeat(${OVER_DEFAULT}))`);

    const stdout = new DefaultGitHubRunner().runGit(['log', '--format=%H']);

    expect(stdout.length).toBe(OVER_DEFAULT);
  });

  it('reports a gh failure with the diagnostic gh actually wrote', () => {
    installShim(
      'gh',
      "process.stderr.write('gh: Not Found (HTTP 404)');process.exit(1)",
    );

    expect(() => new DefaultGitHubRunner().runGh(['api', 'user'])).toThrow(
      /GitHub operation failed: gh: Not Found \(HTTP 404\)/,
    );
  });

  it('names the buffer, not GitHub, when the ceiling is exceeded', () => {
    // ENOBUFS arrives with an empty stderr, so the generic fallback attributed
    // a local limit to GitHub and sent readers hunting auth and rate limits.
    // Provoking a real 256 MiB overflow would cost more than it proves, so the
    // translation is asserted directly against the shape Node raises.
    expect(execFailureDetail({ code: 'ENOBUFS', stderr: '' })).toBe(
      `output exceeded the ${SUBPROCESS_MAX_BUFFER}-byte subprocess buffer`,
    );
    expect(execFailureDetail({ stderr: '  boom  ' })).toBe('boom');
    expect(execFailureDetail({ stderr: '' })).toBe('no diagnostic returned');
  });

  it('pins the default ceiling this package must not inherit', () => {
    let caught: { code?: string; stderr?: string } | undefined;
    try {
      execFileSync(
        process.execPath,
        ['-e', `process.stdout.write('x'.repeat(${OVER_DEFAULT}))`],
        { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] },
      );
    } catch (error: unknown) {
      caught = error as { code?: string; stderr?: string };
    }
    expect(caught?.code).toBe('ENOBUFS');
    expect(caught?.stderr).toBe('');
    expect(SUBPROCESS_MAX_BUFFER).toBeGreaterThan(1024 * 1024);
  });
});
