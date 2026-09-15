import { describe, expect, it, vi } from 'vitest';
import { runCli } from '../cli.js';
import { PACKAGE_VERSION, PROTOCOL_VERSION } from '../constants.js';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

describe('CLI command parser and execution', () => {
  it.each([
    undefined,
    {},
    { posted: 0 },
    {
      posted: 0,
      chainInducedRegressions: 0,
      bySeverityAndOutcome: Object.fromEntries(
        ['blocking', 'major', 'minor', 'nit'].map((severity) => [
          severity,
          { validFixed: 0, validDeferred: 0, invalidDismissed: 0 },
        ]),
      ),
    },
  ])(
    'distinguishes absent findings from measured zero findings: %s',
    (measurement) => {
      const directory = mkdtempSync(join(tmpdir(), 'telemetry-cli-'));
      const changeset = join(directory, 'changeset.json');
      writeFileSync(
        changeset,
        JSON.stringify({
          classifierVersion: 1,
          reviewSignificantFiles: 0,
          files: { app: 0, test: 0, docsConfig: 0, generated: 0 },
          linesChanged: {
            app: 0,
            test: 0,
            comment: null,
            docsConfig: 0,
            generated: 0,
            blank: 0,
          },
          linesByLanguage: {},
        }),
      );
      const findings = join(directory, 'findings.json');
      if (measurement !== undefined)
        writeFileSync(findings, JSON.stringify(measurement));
      const stdout = vi
        .spyOn(process.stdout, 'write')
        .mockImplementation(() => true);
      try {
        expect(
          runCli([
            'emit-telemetry',
            '--repo',
            'owner/repo',
            '--pr',
            '123',
            '--engine',
            'codex',
            '--pass-type',
            'review',
            '--trigger',
            'interactive',
            '--stance',
            'adversarial',
            '--status',
            'changed',
            '--token-source',
            'unavailable',
            '--round',
            '1',
            '--base',
            'a'.repeat(40),
            '--head',
            'b'.repeat(40),
            '--telemetry-run-id',
            'c'.repeat(64),
            '--changeset-file',
            changeset,
            '--dry-run',
            ...(measurement === undefined ? [] : ['--findings-file', findings]),
          ]),
        ).toBe(0);
        if (measurement && 'bySeverityAndOutcome' in measurement) {
          expect(stdout).toHaveBeenCalledWith(
            expect.stringMatching(/local-review-telemetry:v1/),
          );
          expect(stdout).toHaveBeenCalledWith(
            expect.stringMatching(/"idempotencyKey": "run:[0-9a-f]{64}"/),
          );
        } else {
          expect(stdout).toHaveBeenCalledWith(
            expect.stringMatching(/findings/),
          );
          expect(stdout).toHaveBeenCalledWith(
            expect.stringMatching(/"emitted":false/),
          );
        }
      } finally {
        stdout.mockRestore();
        rmSync(directory, { recursive: true });
      }
    },
  );
  it('outputs protocol version when requested', () => {
    const stdoutSpy = vi
      .spyOn(process.stdout, 'write')
      .mockImplementation(() => true);
    const code = runCli(['--protocol-version']);
    expect(code).toBe(0);
    expect(stdoutSpy).toHaveBeenCalledWith(`${PROTOCOL_VERSION}\n`);
    stdoutSpy.mockRestore();
  });

  it('outputs the package version when requested', () => {
    const stdoutSpy = vi
      .spyOn(process.stdout, 'write')
      .mockImplementation(() => true);
    const code = runCli(['--version']);
    expect(code).toBe(0);
    expect(stdoutSpy).toHaveBeenCalledWith(`${PACKAGE_VERSION}\n`);
    stdoutSpy.mockRestore();
  });

  it('reports the package version without requiring a subcommand', () => {
    // `--version` has to answer for a vendored single file with no package
    // around it, so it must never fall through to subcommand validation.
    const stdoutSpy = vi
      .spyOn(process.stdout, 'write')
      .mockImplementation(() => true);
    expect(() => runCli(['--version'])).not.toThrow();
    stdoutSpy.mockRestore();
  });

  it('fails when no subcommand is provided', () => {
    expect(() => runCli([])).toThrowError(/subcommand required/);
  });

  it('rejects an unknown subcommand', () => {
    expect(() => runCli(['not-a-command'])).toThrowError(/unknown command/);
  });

  it('rejects verify-ledger without its required arguments', () => {
    expect(() => runCli(['verify-ledger', '--repo', 'a/b'])).toThrowError(
      /verify-ledger requires --repo, --pr, and --head/,
    );
  });

  it.each([
    ['preflight-anchor', /preflight-anchor requires/],
    ['post-finding', /post-finding requires/],
    ['reopen-occurrence', /reopen-occurrence missing required parameters/],
    ['dispose', /dispose missing required parameters/],
    ['reply', /reply requires/],
    ['post-pr-comment', /post-pr-comment requires/],
    ['validate-result', /validate-result missing required arguments/],
    ['finalize', /finalize requires/],
    ['write-result', /write-result missing required arguments/],
    ['write-blocked-result', /write-blocked-result missing required arguments/],
    ['resolve', /resolve requires/],
    ['reconcile', /reconcile requires/],
    ['read-result', /read-result requires --file or --result-file/],
    ['format-findings', /format-findings requires/],
  ])('dispatches %s to its command-specific validation', (command, message) => {
    expect(() => runCli([command])).toThrowError(message);
  });

  it.each([
    ['--head', 'b'.repeat(40)],
    ['--base', '0'.repeat(40)],
    ['--engine', 'claude'],
    ['--round', '1'],
    ['--expected-result-sha256', 'f'.repeat(64)],
  ])('refuses finalize with the explicit identity flag %s', (flag, value) => {
    expect(() =>
      runCli([
        'finalize',
        '--repo',
        'a/b',
        '--pr',
        '1',
        '--result-file',
        'result.json',
        flag,
        value,
      ]),
    ).toThrowError(/finalize reads identity and digest from the saved result/);
  });

  it.each(['0', '-1', '1.5', '1junk', '9007199254740992'])(
    'rejects malformed numeric argument %s',
    (value) => {
      expect(() => runCli(['verify-ledger', '--pr', value])).toThrowError(
        /--pr must be a positive/,
      );
    },
  );

  it('requires a sealed result for attestation', () => {
    const sha = 'a'.repeat(40);
    expect(() =>
      runCli([
        'attest',
        '--repo',
        'owner/repo',
        '--pr',
        '1',
        '--head',
        sha,
        '--engine',
        'codex',
        '--round',
        '1',
        '--base',
        sha,
        '--before',
        sha,
        '--result-file',
        'result.json',
      ]),
    ).toThrowError(/--expected-result-sha256/);
  });

  it.each([
    ['emit-telemetry', '--unknown'],
    ['emit-telemetry', '--round', '0'],
    ['emit-telemetry', '--head', 'not-a-sha'],
    ['emit-telemetry', '--repo'],
  ])(
    'reports telemetry parse and validation failures without throwing',
    (...argv) => {
      const stdoutSpy = vi
        .spyOn(process.stdout, 'write')
        .mockImplementation(() => true);
      try {
        expect(runCli(argv)).toBe(0);
        expect(stdoutSpy).toHaveBeenCalledWith(
          expect.stringMatching(/"emitted":false/),
        );
      } finally {
        stdoutSpy.mockRestore();
      }
    },
  );

  it('keeps telemetry non-failing when boolean flags precede the command', () => {
    const stdoutSpy = vi
      .spyOn(process.stdout, 'write')
      .mockImplementation(() => true);
    try {
      expect(runCli(['--truncated', 'emit-telemetry', '--unknown'])).toBe(0);
      expect(stdoutSpy).toHaveBeenCalledWith(
        expect.stringMatching(/"emitted":false/),
      );
    } finally {
      stdoutSpy.mockRestore();
    }
  });
});
