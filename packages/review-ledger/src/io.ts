import { lstatSync } from 'node:fs';
import { fail, LedgerError } from './errors.js';

/**
 * Assert a path is a regular, non-symlink file, failing with the given message.
 */
export function assertRegularFile(pathValue: string, message: string): void {
  try {
    const stat = lstatSync(pathValue);
    if (stat.isSymbolicLink() || !stat.isFile()) {
      fail(message);
    }
  } catch (err) {
    if (err instanceof LedgerError) throw err;
    fail(message);
  }
}

/**
 * Parse JSON, converting a parse failure into a LedgerError with the message.
 */
export function parseJsonOrFail<T = unknown>(raw: string, message: string): T {
  try {
    return JSON.parse(raw) as T;
  } catch (error) {
    throw new LedgerError(message, { cause: error });
  }
}
