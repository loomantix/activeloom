#!/usr/bin/env node
'use strict';

// The Python entrypoint supplies source over stdin. Never load project config,
// plugins or imports; the explicitly selected compiler is the only dependency.
const fs = require('node:fs');
const path = require('node:path');

const protectedComment =
  /@\w|\b(?:SPDX|copyright|license|eslint|prettier|biome|oxlint|istanbul|c8|v8|webpack\w*|sourceMappingURL|sourceURL|SAFETY|globals?|exported|jshint|jslint|jscs|deno-lint|noinspection|tslint)\b|[#@]__\w+__|^\/\*!|^\/\/\/|^#!/i;

/** Load only the compiler version exercised by the regression suite. */
function loadCompiler(modulePath) {
  let ts;
  try {
    ts = require(path.resolve(modulePath));
  } catch {
    throw new Error(
      'cannot load TypeScript; pass an installed typescript 5.9.3 module path',
    );
  }
  if (ts.version !== '5.9.3') {
    throw new Error(
      'unsupported TypeScript version; this verifier requires 5.9.3',
    );
  }
  return ts;
}

/** Capture syntax, literal bytes and protected trivia without source offsets. */
function inspect(ts, fileName, text) {
  const source = ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.Latest,
    true,
  );
  if (source.parseDiagnostics.length) {
    const d = source.parseDiagnostics[0];
    // Diagnostics can quote input; only expose the compiler code and location.
    throw new Error(`parse diagnostic TS${d.code} at offset ${d.start ?? 0}`);
  }
  const leaves = [];
  const shape = [];
  function visit(node) {
    if (
      node.kind >= ts.SyntaxKind.FirstJSDocNode &&
      node.kind <= ts.SyntaxKind.LastJSDocNode
    )
      return;
    shape.push(node.kind);
    const children = node.getChildren(source);
    if (children.length) {
      children.forEach(visit);
    } else if (node.kind !== ts.SyntaxKind.EndOfFileToken) {
      const start =
        node.kind === ts.SyntaxKind.JsxText ? node.pos : node.getStart(source);
      if (node.end > start)
        leaves.push({ kind: node.kind, start, end: node.end });
    }
    shape.push(-1);
  }
  visit(source);
  const tokens = leaves.map((t) => [t.kind, text.slice(t.start, t.end)]);
  const comments = [];
  const trivia = [];
  let end = 0;
  for (let index = 0; index <= leaves.length; index++) {
    const next = leaves[index]?.start ?? text.length;
    const gap = text.slice(end, next);
    const scanner = ts.createScanner(
      ts.ScriptTarget.Latest,
      false,
      ts.LanguageVariant.Standard,
      gap,
    );
    for (
      let kind = scanner.scan();
      kind !== ts.SyntaxKind.EndOfFileToken;
      kind = scanner.scan()
    ) {
      if (
        kind === ts.SyntaxKind.WhitespaceTrivia ||
        kind === ts.SyntaxKind.NewLineTrivia
      )
        continue;
      if (
        ![
          ts.SyntaxKind.SingleLineCommentTrivia,
          ts.SyntaxKind.MultiLineCommentTrivia,
          ts.SyntaxKind.ShebangTrivia,
        ].includes(kind)
      ) {
        throw new Error('unsupported non-trivia parser gap');
      }
      const raw = scanner.getTokenText();
      const start = end + scanner.getTokenPos();
      const stop = end + scanner.getTextPos();
      comments.push([start, stop]);
      if (protectedComment.test(raw)) {
        // Keep byte-exact annotations and their preceding/following token scope.
        // Line-sensitive directives must still address the same physical line.
        const lineSensitive =
          /ts-(?:ignore|expect-error)|(?:disable|ignore)-next-line|eslint-disable-line/.test(
            raw,
          );
        trivia.push([
          index,
          raw,
          /[\r\n\u2028\u2029]/.test(text.slice(end, start)),
          lineSensitive
            ? text.slice(stop, next).split(/\r\n|[\r\n\u2028\u2029]/).length - 1
            : null,
        ]);
      }
    }
    end = leaves[index]?.end ?? next;
  }
  const emitted = ts.transpileModule(text, {
    fileName,
    reportDiagnostics: true,
    compilerOptions: {
      target: ts.ScriptTarget.ESNext,
      module: ts.ModuleKind.ESNext,
      jsx: ts.JsxEmit.Preserve,
      removeComments: true,
      experimentalDecorators: true,
      emitDecoratorMetadata: true,
      newLine: ts.NewLineKind.LineFeed,
    },
  });
  if (
    emitted.diagnostics?.some((d) => d.category === ts.DiagnosticCategory.Error)
  ) {
    throw new Error('compiler emission diagnostic; cannot certify');
  }
  return { tokens, shape, trivia, emitted: emitted.outputText, comments };
}

/** Compare both source snapshots; never return their contents in diagnostics. */
function verify(ts, fileName, before, after) {
  try {
    if (
      !/\.(?:[cm]?[jt]s|[jt]sx)$/i.test(fileName) ||
      /\.d\.[cm]?ts$/i.test(fileName)
    ) {
      throw new Error(
        'unsupported compiler file extension or declaration file',
      );
    }
    const left = inspect(ts, fileName, before);
    const right = inspect(ts, fileName, after);
    for (const field of ['tokens', 'shape', 'trivia', 'emitted']) {
      if (JSON.stringify(left[field]) !== JSON.stringify(right[field])) {
        return {
          status: 'changed',
          detail: `${field} differ`,
          verifier: `typescript-${ts.version}`,
        };
      }
    }
    return { status: 'unchanged', verifier: `typescript-${ts.version}` };
  } catch (error) {
    return { status: 'error', detail: error.message };
  }
}

if (require.main === module) {
  try {
    const ts = loadCompiler(process.argv[2]);
    const request = JSON.parse(fs.readFileSync(0, 'utf8'));
    const results = request.map((item) =>
      verify(ts, item.path, item.before, item.after),
    );
    process.stdout.write(JSON.stringify(results));
  } catch (error) {
    process.stdout.write(JSON.stringify({ error: error.message }));
    process.exitCode = 1;
  }
}

module.exports = { inspect, loadCompiler, verify };
