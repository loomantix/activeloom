/**
 * A fail-closed local-review ledger validation or mutation error.
 */
export class LedgerError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'LedgerError';
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/**
 * Throw a LedgerError with the given message.
 */
export function fail(message: string): never {
  throw new LedgerError(message);
}
