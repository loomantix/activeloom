---
name: onboard
description: Finish what `npx activeloom init` deliberately left blank — draft the project-specific config values from the repository, and settle which engines and models this repo's skills should run against — presenting both for confirmation. Use after `npx activeloom init` when .activeloom-config.yml still has TODO(activeloom) markers to fill.
---

# Onboard this repository

`npx activeloom init` writes a config from facts it can verify: the lockfile, the ecosystems, the declared test and lint scripts. It deliberately refuses to invent the rest, and leaves a `TODO(activeloom):` marker wherever judgement is needed.

This skill fills those markers. You are drafting for confirmation, not authoring: every value here ends up in a reviewer prompt that runs on every pull request, so a plausible-sounding guess is worse than a blank — nobody re-reads a field that looks filled in.

## When there is nothing to do

If `.activeloom-config.yml` does not exist, stop and say so. Run `npx activeloom init` first; this skill edits a config, it does not create one.

If it exists, work out what is still outstanding before doing anything:

- `TODO(activeloom):` markers in the config — steps 1, 2, and 4.
- `agent-loop.config` that has not been checked against this machine — step 3. Step 3 always confirms the required CLIs are present, whatever the config already contains; on some harnesses it also fills keys that ship empty.

Report which of the two apply and do only those. If neither applies, say the repository is already onboarded and stop. Never rewrite a value a human chose.

## 1. Read before you draft

Read enough to write each value from evidence. Most lookups here are two greps and a read, and belong inline. Reach for delegation only where this session supports it and the lookup is both genuinely independent and too large for a handful of tool calls — a sweep across many files or repos. State a word ceiling on what it returns.

Establish, in roughly this order:

- **What the project is.** `AGENTS.md`, then `README.md`. If both are absent or uninformative, the package/module metadata and the top-level directory names.
- **The stack.** Manifests and lockfiles, not guesses: `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`. Name versions only where a manifest pins one.
- **How it is tested and linted.** The declared scripts, plus the CI workflow — CI is the authority on what actually gates a merge, and it often runs more than the scripts do.
- **The conventions.** Commit message format from `git log`, lint configuration, formatter configuration, and any `CONTRIBUTING.md`.

## 2. Draft each marker

| Key                | What it must contain                                                                                                                                   |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `PROJECT_OVERVIEW` | Two or three sentences: what the project does and who uses it. No marketing.                                                                           |
| `CANONICAL_DOCS`   | Inline-code paths to the docs a reviewer should consult. Only files that exist.                                                                        |
| `STACK_TABLE`      | A Markdown table of layer and technology. Add a short "common mistakes to flag" list only if the repository gives you evidence for one.                |
| `CODE_RULES`       | Bullets a reviewer can act on: the commit convention, the type strictness, the import rules, the paths that are generated and must not be hand-edited. |
| `REVIEW_FOCUS`     | A numbered list, most important first. Replace the generic default only where this repository justifies something more specific.                       |
| `DOMAIN_RULES`     | Usually empty. Fill it only for a real domain constraint — a regulatory rule, a wire format, a compatibility guarantee.                                |

Two rules about content:

- **Every claim must be traceable to a file you read.** If you cannot point at the evidence, leave the marker in place and say which one you could not resolve.
- **Values must be valid Markdown and Prettier-clean.** They are substituted into `.github/copilot-instructions.md`, which the sync checks with the repository's Prettier configuration. A malformed table fails that check in every consumer at once.

## 3. Engines and models

`agent-loop` requires **both** the Gemini (`agy`) and Claude CLIs. Its
consumer-owned config is `.agents/skills/agent-loop/agent-loop.config`.

The Gemini and Claude review hooks, Claude effort policy, worker model, and
fallback model ship with supported defaults. Do not rewrite those values as
part of onboarding. Verify that both required CLIs are available; if either
is missing, say `agent-loop` cannot run and leave its configuration intact.

## 4. Present for confirmation

Show the drafted values before writing them. For each one give the value and the evidence in a single line — the file you took it from.

Ask explicitly about anything you had to leave marked, and about anything where the repository offered two plausible readings. Do not smooth over a genuine ambiguity by picking one.

## 5. Write, then verify

After the user confirms, write the values into `.activeloom-config.yml` — editing only the marked keys, leaving everything else untouched.

Then verify against the real engine rather than by re-reading your own edit:

```
npx activeloom init --yes
```

A clean run means the config parses and the engine rendered every target from it. The command is intentionally real rather than `--dry-run`: a dry run writes no rendered files, so scanning afterward would inspect stale output or nothing at all.

The marker scan is therefore the actual gate, not a closing formality. Grep the rendered output — not just the config — for `TODO(activeloom):`, and treat any hit as unfinished work rather than reporting the repository verified. Then tell the user which files change on the next real `init`.

## What this skill does not do

- It does not choose the tier. That is `npx activeloom init` with or without `--sync`, and it is the user's call.
- It does not set secrets, and it does not create a GitHub App. Those belong to Tier 3 and to a human.
- It does not commit. Leave the change staged for the user to review.
