---
name: simplify-comments
description: Audit comment density and cut comment bloat — ticket citations, incident narratives, review receipts, banners, restated code — without changing code. Use when the user asks to simplify or clean up comments, put code on a comment diet, audit comment density, or reduce token bloat in source files.
<<FM_EXTRAS>>
---

# <<INVOKE>>simplify-comments

Remove comments that record history, condense comments that carry meaning, and leave the code exactly as it was. Git history already holds provenance; a comment earns its lines by telling a future reader something the code cannot.

The helper beside this file measures and verifies: `python3 <this skill's directory>/scripts/comment-density.py`, written `comment-density.py` below. Its `--help` lists every filter.

## Verification prerequisites and boundaries

Density is an audit estimate, not a preservation certificate. For JavaScript,
TypeScript, JSX and TSX, use `--typescript <installed-typescript-module-path>`
on both baseline and final verification. This requires Node.js and TypeScript
5.9.3; point to the trusted project's `node_modules/typescript` or an existing
isolated tool installation. The helper installs nothing, loads no project
configuration or plugins, and fails closed on missing dependencies, a different
compiler version, malformed input, or unsupported constructs. Never drop the
flag to make a failed compiler verification pass.

The compiler mode compares exact non-comment parser tokens, syntax structure,
emitted JavaScript, and byte-identical anchored annotations and legal comments.
It preserves JSX text/whitespace, regular expressions, template raw text and
literal NUL bytes. Annotated comment blocks stay whole, including API tags and
examples: a comment carrying a tag, directive or legal text, together with the
consecutive `//` lines around it, must stay byte-identical and in place. Under
this mode leave such a block as it is, even where the taxonomy would delete or
condense part of it. A `changed` result names the baseline and edited line of
the first difference; restore that edit and rerun. Density counts stay
approximate even when the compiler certifies preservation. This is a comment
edit check, not a typecheck or a proof about arbitrary external tools that read
source text: inspect every Preserve item and run repository gates as well.

Without the compiler option the standard-library verifier retains its limited
lexical coverage; it cannot certify JSX, ambiguous regex contexts or JavaScript
Unicode line separators. Java Unicode escapes, cgo preambles, and nested quotes
in Kotlin/Swift/C# interpolation remain unsupported. Shell, YAML, SQL, CSS, HTML,
Prisma and Helm/template files have **audit-only** estimates: heredocs,
scalar bodies, embedded languages and template whitespace need their own
grammar-aware verifiers. They always fail certification, even if unedited.
Declaration files and unknown dialects remain outside compiler coverage.

Parser capability never changes ownership or scope. Keep generated/vendor,
fixture, migration and upstream-owned exclusions separate from syntax failures;
an explicitly named path can bypass audit filters but does not authorize edits.

## Phase 1 — Audit

1. Run `comment-density.py <path> --json` on the target path, or on the repository root when none was named. Defaults skip dependency, build, vendored, generated, and minified files.
2. Report the total density, then a table of the top candidates: path, comment lines, total lines, density. Rank by comment lines; files at or above 25% density with a few hundred lines are the strongest candidates. Leave out test fixtures and code the repository does not own.

The audit is done when the user has the totals and the ranked table. If the user asked only for an audit, stop here.

## Phase 2 — Scope

1. Pick a batch a reviewer can read line by line — one to three large files, or one small directory — within the user's authorized scope. Reuse prior authorization for already-approved batches; confirm only a material expansion or unresolved scope choice.
2. Run `git fetch origin <default-branch>`, then create a linked worktree outside the primary checkout on a new untracked branch, `git worktree add --no-track -b refactor/simplify-comments-<slug> <worktree-path> origin/<default-branch>`, and work only there.
3. Save the baseline: `comment-density.py <batch files> --json`, then run `comment-density.py --verify-against origin/<default-branch> <batch files>` before editing, adding `--typescript <module-path>` for JS/TS batches. Record the chosen verifier and compiler version. Report and exclude files the helper cannot certify; keep audit estimates and certified file counts separate.
4. Find the repository's gates in <<AGENT_DOC_CODE>> and its build manifest (`package.json`, `justfile`, `Makefile`, `pyproject.toml`): formatter, typecheck, lint including documentation lint rules, and the unit tests that cover the batch.

Scope is done when the worktree exists, the baseline is saved, and every gate has a named command.

## Phase 3 — Editorial pass

Read each file whole before editing it. Classify every comment and docstring against the taxonomy below, and edit comment text only.

- A comment mixing history with an invariant keeps the invariant, stated in the present tense, and loses the history.
- When a comment might be protecting a compliance or security rule, keep it and condense the wording. History is the only thing deleted on sight.
- If a detailed lesson learned was captured in a comment due to a product bug report or incident, summarize the invariant or reasoning into a single present-tense sentence rather than completely removing it.
- A comment that contradicts the code is a finding, not an edit: leave it and list it for the PR body.
- After a deletion, fix any neighbouring comment that pointed at it ("see above", "as noted").

The pass is done when every comment in the batch has been classified and every contradiction found is listed.

## Taxonomy

### Delete

| Category | Recognize by |
| --- | --- |
| Tracker citations | `#2528`, `org/repo#12`, `ABC-123` keys, pull-request links. Keep the rest of the sentence when it carries meaning. |
| Incident and bug-hunt narratives | Past outages, what was once observed, dated recaps, `@see` links into incident or postmortem archives. (If a comment captures a valuable lesson learned, condense to the invariant under Condense below rather than deleting entirely.) |
| Review receipts and assistant residue | `(Copilot review on PR #2724)`, `(Gemini finding #3)`, "as requested", "updated to", changelog-style notes. |
| Decorative banners | `// ─── 401 Circuit Breaker ───`, `# ===== Helpers =====`. |
| Restated code | `// increment counter` above `counter++`, `@param id - the id`, a summary that repeats the function name. |
| Commented-out code | Disabled code blocks; history keeps them. |

### Condense to one or two sentences

- **Concurrency, ordering, and retries** — races, lock order, idempotency, backoff ladders. State the invariant and why it holds, not the bug that revealed it.
- **Bug-report and incident lessons** — a comment capturing a hard-won lesson learned from a bug report or incident is condensed to a single present-tense sentence stating the invariant or reasoning, rather than deleted entirely.
- **Workarounds** — what is worked around and when it can go. A link to the third-party bug a workaround waits on stays.
- **Internal helpers** — a doc block on non-exported code becomes one line, or goes entirely when it restates the code.

### Preserve

- **Exported API documentation** — the doc on every exported class, function, type, and interface stays, with every tag tools read (`@param`, `@returns`, `@throws`, `@example`). Only filler paragraphs go.
- **Compliance and security invariants** — regulated-data boundaries, tenant scoping, authorization checks, encryption and key requirements (for example, a field that must never be written without its key identifier), audit-logging duties. Condense the wording; the whole rule stays.
- **Directives** — `@ts-expect-error`, `@ts-ignore`, `eslint-disable`, `prettier-ignore`, `@format`, `@deprecated`, `@internal`, `/// <reference>`, `//go:build`, `# type: ignore`, `# noqa`, `/*#__PURE__*/`, `webpackChunkName`, coverage ignores, shebangs, encoding lines. Their text stays byte-identical.
- **Legal text** — license headers, SPDX lines, `@license`, `/*!` blocks.
- **Executable or runtime-read text** — doctests, documentation examples run as tests, `SAFETY:` comments on unsafe blocks, docstrings read at runtime (`__doc__`, CLI help).
- **Open work** — the action text of `TODO` and `FIXME`, minus any tracker citation.

### What good looks like

Before:

```ts
// ─── Reconnect ───────────────────────────────────────
// Fix for #2528: after the March outage we saw duplicate sessions when the
// socket reconnected mid-flush. Copilot review on PR #2724 suggested holding
// the lock, so we now acquire flushLock first.
// More investigation notes live in the incident archive.
await this.flushLock.acquire();
```

After:

```ts
// Hold flushLock across reconnect so an in-flight flush cannot open a second session.
await this.flushLock.acquire();
```

The reference pilot applied this taxonomy to two large production modules: 4,918 → 3,429 lines (density 43.9% → 17.2%) and 3,554 → 2,906 lines (28.0% → 10.9%), as `comment-density.py` measures them. It removed 2,137 lines, `--verify-against` reports both files unchanged, and typecheck, lint, and every unit test passed unchanged. Expect similar ratios on narrative-heavy files and much smaller ones on files that are already terse.

## Phase 4 — Validate and ship

1. Run the formatter, then confirm `git diff --name-status origin/<default-branch>` lists only modified (`M`) files.
2. Run `comment-density.py --verify-against origin/<default-branch> <changed files>` with the same compiler option used for the baseline. It must exit 0 for every selected file. For other supported languages the legacy fingerprints preserve recognized directives, Rust doc comments, Go example output, token boundaries and significant line breaks (Python compares the AST without docstrings). Restore rejected edits; skipped inputs or unsupported syntax are failures, never evidence of unchanged code. Confirm every Preserve item in the diff: legacy checks do not cover all legal text or runtime-read docstrings, and neither mode recognizes every external tool annotation.
3. Run every gate named in Phase 2. A failure usually means a directive or a required doc was removed; restore it and rerun.
4. Commit as `refactor(comments): condense comments in <scope>`, push with `git push -u origin HEAD`, and open a draft pull request. Keep the body under 250 words: the before/after table from `--verify-against … --json` (lines and density per file), the gates run with their results, and the contradictions listed in Phase 3. Then follow the repository's review workflow.

The change is done when verification exits 0, the diff keeps every Preserve item, every gate passes on the pushed commit, and the draft pull request carries the metrics.

## Scope

This skill edits comments and docstrings in source files. It does not rename, reformat beyond the repository's formatter, add documentation to undocumented code, or touch generated, vendored, or Markdown files.
