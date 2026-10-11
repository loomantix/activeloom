import { readdirSync, readFileSync } from 'node:fs';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

/** Architectural tripwire, not a sandbox for untrusted package code or gh. */
function networkViolations(name: string, source: string): string[] {
  const parsed = ts.createSourceFile(
    name,
    source,
    ts.ScriptTarget.Latest,
    true,
  );
  const violations: string[] = [];
  function visit(node: ts.Node): void {
    if (
      ts.isImportDeclaration(node) &&
      ts.isStringLiteral(node.moduleSpecifier)
    ) {
      const spec = node.moduleSpecifier.text;
      if (
        !spec.startsWith('./') &&
        !spec.startsWith('node:') &&
        spec !== '@babel/parser'
      )
        violations.push(spec);
      if (/^(?:node:)?(?:https?|net|tls|dgram|dns|http2)$/.test(spec))
        violations.push(spec);
      if (
        spec === 'node:child_process' &&
        !['github.ts', 'ledger.ts'].includes(name)
      )
        violations.push(spec);
    }
    if (
      (ts.isCallExpression(node) || ts.isNewExpression(node)) &&
      /^(?:fetch|WebSocket|XMLHttpRequest|require|import)$/.test(
        node.expression.getText(parsed),
      )
    ) {
      violations.push(node.expression.getText(parsed));
    }
    if (
      ts.isCallExpression(node) &&
      node.expression.getText(parsed) === 'execFileSync'
    ) {
      const command = node.arguments[0]?.getText(parsed);
      if (
        command !== "'git'" &&
        !(
          name === 'github.ts' &&
          command === 'command' &&
          source.includes("const command = 'gh';")
        )
      )
        violations.push(`subprocess ${command}`);
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  return violations;
}

describe('GitHub-only network architecture', () => {
  it('keeps network access behind the gh runner; remaining subprocesses are local git', () => {
    const root = new URL('../', import.meta.url);
    for (const name of readdirSync(root).filter((file) =>
      file.endsWith('.ts'),
    )) {
      expect(
        networkViolations(name, readFileSync(new URL(name, root), 'utf8')),
        name,
      ).toEqual([]);
    }
  });

  it.each([
    "import https from 'node:https';",
    "import client from 'external-ingestion';",
    "fetch('https://example.invalid');",
    "import { execFileSync } from 'node:child_process'; execFileSync('curl', []);",
  ])('rejects a new network route: %s', (source) => {
    expect(networkViolations('telemetry.ts', source).length).toBeGreaterThan(0);
  });
});
