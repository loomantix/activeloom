import { defineConfig } from 'tsup';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

// Read at build time so the version travels inside the artifact. The
// single-file build is vendored away from this package.json, so nothing can
// resolve it at runtime.
const { version } = createRequire(import.meta.url)('./package.json') as {
  version: string;
};
const define = { __PACKAGE_VERSION__: JSON.stringify(version) };
const parserLicense = readFileSync(
  createRequire(import.meta.url).resolve('@babel/parser/LICENSE'),
  'utf8',
);

export default defineConfig([
  {
    entry: { index: 'src/index.ts', bin: 'src/bin.ts' },
    format: ['esm', 'cjs'],
    dts: true,
    clean: true,
    sourcemap: true,
    target: 'es2022',
    define,
  },
  // Single-file, self-contained build of the CLI. Engine repos vendor this one
  // artifact verbatim and run it as `node review-ledger.js`, so it must not
  // depend on sibling chunks, source maps, or an install step. Its bytes are
  // compared against a fresh build by ActiveLoom's CI —
  // keep it unminified so a reviewer can read what they are committing.
  {
    entry: { 'review-ledger.bundle': 'src/bin.ts' },
    format: ['esm'],
    dts: false,
    clean: false,
    sourcemap: false,
    splitting: false,
    minify: false,
    noExternal: ['@babel/parser'],
    banner: { js: `/*! Bundled @babel/parser (MIT)\n${parserLicense}*/` },
    target: 'es2022',
    define,
  },
]);
