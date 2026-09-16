// Injected by tsup's `define` at build time; absent when the sources are run
// directly (vitest, tsx), which is why every read goes through a `typeof`
// guard in constants.ts.
declare const __PACKAGE_VERSION__: string | undefined;
