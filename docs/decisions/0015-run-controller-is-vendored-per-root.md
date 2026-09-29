# 0015 — The run controller is one implementation vendored into every root

- Status: accepted
- Date: 2026-09-28

## Context

The run controller owns the `local-review-run:v1` markers every review pass is
numbered inside. `critique` and `deepcritique` call it on each pass, and each
skill instructs the engine to stop when it is absent rather than run
unauthorized.

One copy existed, at `.codex/skills/critique/scripts/local-review-handoff.py`,
registered in `scripts/sync-targets.yml` under the `codex:` harness block. A
repository installed with `--harness claude` or `--harness gemini` alone
therefore received review skills whose first controller call could not resolve.
The failure surfaced at the first review, not at install: nothing in `init` or
`detect` checked for it.

Three documents carried the workaround — "include the Codex harness even if you
only use Claude" — and both non-Codex review workflows conceded the placement
in the same words: "It lives under `.codex/` for historical reasons and is not
Codex-only." A cross-root path that every reader has to be told to ignore is a
distribution bug wearing a documentation sentence.

Copying the script into each root was not an option: record 0007 holds that a
protocol-level piece belongs in all roots "rewritten against each root's own
launch model, never copied across", and three hand-maintained copies of one
protocol drift.

## Decision

- The controller has exactly one source of truth,
  `packages/review-ledger/protocol/local-review-handoff.py`, beside the ledger
  protocol document it shares a marker vocabulary with.
- `scripts/render-prompts.py` vendors it verbatim into every harness root at
  `<root>/skills/critique/scripts/local-review-handoff.py`, the same mechanism
  and the same `--check` that already guard `references/local-review-ledger.md`.
  The copies are generated files with one writer; a hand-edit fails the render
  check.
- Every root's prompts resolve the controller under **their own root**. No
  review prompt names another root's path, and the "historical reasons"
  concession is deleted because it is no longer true.
- Each harness target set ships its own copy, so harness selection no longer
  decides whether a repository can review at all.
- `VENDORED_DOCUMENTS` may now target a directory belonging to an _unrendered_
  skill. A rendered skill's directory stays off limits: `unowned_files` deletes
  anything there the skill render did not write, so a vendored file would be
  produced and swept by the same run. The guard names that collision instead of
  the parent directory.

This supersedes the parenthetical in record 0012 that gives the controller's
location as a `.codex/` path. The script, its commands, its marker formats, and
its budget semantics are unchanged — only where the bytes come from and where
they land.

## Consequences

`critique` and `deepcritique` work on any single selected harness. The
`review-chain-runner.py` that drives the _automatic_ chain stays Codex-only and
still requires the Codex tree; that is a defensible requirement, because an
inherently multi-engine chain is not something a single-harness repository can
run anyway. The two files are no longer coupled: needing the chain is now the
only reason to install a harness you do not use.

The drift surface shrinks rather than grows. Three roots hold the same bytes by
construction, and `critique`'s parity residual falls, which a `recorded` entry
already permits to move freely.
