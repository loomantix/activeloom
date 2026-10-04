const Handlebars = require('./node_modules/handlebars');

// Composition is static: engine-specific structure lives in its own template.
function parseTemplate(name, source) {
  const ast = Handlebars.parse(source);
  const dependencies = [];
  for (const node of ast.body) {
    if (node.type === 'ContentStatement') continue;
    if (
      node.type !== 'PartialStatement' ||
      node.name.type !== 'PathExpression' ||
      !/^[a-z][a-z0-9-]*(?:\/[a-z][a-z0-9-]*)*$/.test(node.name.original) ||
      node.params.length ||
      node.hash ||
      node.strip.open ||
      node.strip.close
    ) {
      throw new Error(`${name}: only static partial includes are supported`);
    }
    dependencies.push(node.name.original);
  }
  return { ast, dependencies };
}

function compose({ partials, templates }) {
  const parsed = new Map(
    Object.entries(partials).map(([name, source]) => [
      name,
      parseTemplate(name, source),
    ]),
  );
  const visited = new Set();
  function visit(name, ancestors = new Set()) {
    if (ancestors.has(name)) throw new Error(`Partial cycle at ${name}`);
    if (visited.has(name)) return;
    const partial = parsed.get(name);
    if (!partial) throw new Error(`Missing partial: ${name}`);
    const next = new Set(ancestors).add(name);
    for (const dependency of partial.dependencies) visit(dependency, next);
    visited.add(name);
  }
  for (const name of parsed.keys()) visit(name);

  const engine = Handlebars.create();
  const options = {
    noEscape: true,
    strict: true,
    data: false,
    preventIndent: true,
  };
  for (const [name, { ast }] of parsed) {
    engine.registerPartial(name, engine.compile(ast, options));
  }
  return Object.fromEntries(
    Object.entries(templates).map(([name, source]) => {
      const { ast, dependencies } = parseTemplate(name, source);
      for (const dependency of dependencies) visit(dependency);
      return [name, engine.compile(ast, options)({})];
    }),
  );
}

module.exports = { compose };

if (require.main === module) {
  try {
    const input = require('node:fs').readFileSync(0, 'utf8');
    process.stdout.write(JSON.stringify(compose(JSON.parse(input))));
  } catch (error) {
    process.stderr.write(`Prompt composition failed: ${error.message}\n`);
    process.exitCode = 1;
  }
}
