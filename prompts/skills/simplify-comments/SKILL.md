---
name: simplify-comments
description: Audit comment density and cut comment bloat — ticket citations, incident narratives, review receipts, banners, restated code — without changing code. Use when the user asks to simplify or clean up comments, put code on a comment diet, audit comment density, or reduce token bloat in source files.
<<FM_EXTRAS>>
---

# <<INVOKE>>simplify-comments

Remove comments that record history, condense comments that carry meaning, and leave the code exactly as it was. Git history already holds provenance; a comment earns its lines by telling a future reader something the code cannot.

The helper beside this file measures and verifies: `python3 <this skill's directory>/scripts/comment-density.py`, written `comment-density.py` below. Its `--help` lists every filter.

## Phase 1 — Audit

1. Run `comment-density.py <path> --json` on the target path, or on the repository root when none was named. Defaults skip dependency, build, vendored, generated, and minified files.
2. Report the total density, then a table of the top candidates: path, comment lines, total lines, density. Rank by comment lines; files at or above 25% density with a few hundred lines are the strongest candidates. Leave out test fixtures and code the repository does not own.

The audit is done when the user has the totals and the ranked table. If the user asked only for an audit, stop here.

## Phase 2 — Scope

1. Pick a batch a reviewer can read line by line — one to three large files, or one small directory — and confirm it with the user.
2. Create a linked worktree on a new branch from the up-to-date default branch, `git worktree add -b refactor/simplify-comments-<slug> ~/wt/<repo>-simplify-comments-<slug> origin/<default-branch>`, and work only there.
3. Save the baseline: `comment-density.py <batch files> --json`.
4. Find the repository's gates in <<AGENT_DOC_CODE>> and its build manifest (`package.json`, `justfile`, `Makefile`, `pyproject.toml`): formatter, typecheck, lint including documentation lint rules, and the unit tests that cover the batch.

Scope is done when the worktree exists, the baseline is saved, and every gate has a named command.

## Phase 3 — Editorial pass

Read each file whole before editing it. Classify every comment and docstring against the taxonomy below, and edit comment text only.

- A comment mixing history with an invariant keeps the invariant, stated in the present tense, and loses the history.
- When a comment might be protecting a compliance or security rule, keep it and condense the wording. History is the only thing deleted on sight.
- A comment that contradicts the code is a finding, not an edit: leave it and list it for the PR body.
- After a deletion, fix any neighbouring comment that pointed at it ("see above", "as noted").

The pass is done when every comment in the batch has been classified and every contradiction found is listed.

## Taxonomy

### Delete

| Category | Recognize by |
| --- | --- |
| Tracker citations | `#2528`, `org/repo#12`, `ABC-123` keys, pull-request links. Keep the rest of the sentence when it carries meaning. |
| Incident and bug-hunt narratives | Past outages, what was once observed, dated recaps, `@see` links into incident or postmortem archives. |
| Review receipts and assistant residue | `(Copilot review on PR #2724)`, `(Gemini finding #3)`, "as requested", "updated to", changelog-style notes. |
| Decorative banners | `// ─── 401 Circuit Breaker ───`, `# ===== Helpers =====`. |
| Restated code | `// increment counter` above `counter++`, `@param id - the id`, a summary that repeats the function name. |
| Commented-out code | Disabled code blocks; history keeps them. |

### Condense to one or two sentences

- **Concurrency, ordering, and retries** — races, lock order, idempotency, backoff ladders. State the invariant and why it holds, not the bug that revealed it.
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
// @see docs/incidents/archive/reconnect-duplicates.md
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
2. Run `comment-density.py --verify-against origin/<default-branch> <changed files>`. It compares each file's tokens with comments and layout removed (for Python, the AST without docstrings), keeping directive comments, and must exit 0. For a file reported `changed`, read its `git diff`: layout the formatter reflowed after a deletion is acceptable; restore anything else.
3. Run every gate named in Phase 2. A failure usually means a directive or a required doc was removed; restore it and rerun.
4. Commit as `refactor(comments): condense comments in <scope>`, push, and open a draft pull request. Keep the body under 250 words: the before/after table from `--verify-against … --json` (lines and density per file), the gates run with their results, and the contradictions listed in Phase 3. Then follow the repository's review workflow.

The change is done when verification exits 0, every gate passes on the pushed commit, and the draft pull request carries the metrics.

## Scope

This skill edits comments and docstrings in source files. It does not rename, reformat beyond the repository's formatter, add documentation to undocumented code, or touch generated, vendored, or Markdown files.
