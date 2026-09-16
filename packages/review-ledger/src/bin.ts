#!/usr/bin/env node
/**
 * Executable entry point for the `review-ledger` CLI.
 *
 * The runnable side effect lives here rather than in `cli.ts` so that importing
 * the library never starts a CLI run. Guarding by inspecting `process.argv[1]`
 * cannot do that safely: the package's own installed path contains the CLI's
 * name, so any such check fires on plain imports too.
 */
import { runCli } from './cli.js';

try {
  const exitCode = runCli();
  if (exitCode !== 0) {
    process.exitCode = exitCode;
  }
} catch (error: unknown) {
  // console.error + exitCode rather than stderr.write + process.exit: when
  // stderr is a pipe, process.exit can terminate before the write drains and
  // the run fails with no diagnostic at all.
  const err = error as { message?: string; name?: string; cause?: unknown };
  const kind = err.name === 'LedgerError' ? '' : 'unexpected error: ';
  const cause =
    err.cause === undefined
      ? ''
      : `\n  caused by: ${(err.cause as { message?: string })?.message ?? String(err.cause)}`;
  console.error(
    `review-ledger: ${kind}${err.message ?? String(error)}${cause}`,
  );
  process.exitCode = 1;
}
