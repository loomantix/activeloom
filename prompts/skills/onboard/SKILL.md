---
name: onboard
description: Finish what `npx activeloom init` deliberately left blank — draft the project-specific config values from the repository, and settle which engines and models this repo's skills should run against — presenting both for confirmation.<<DESC_TRIGGER>>
<<FM_EXTRAS>>
---

# Onboard this repository

`npx activeloom init` writes a config from facts it can verify: the lockfile, the ecosystems, the declared test and lint scripts. It deliberately refuses to invent the rest, and leaves a `TODO(activeloom):` marker wherever judgement is needed.

This skill fills those markers. You are drafting for confirmation, not authoring: every value here ends up in a reviewer prompt that runs on every pull request, so a plausible-sounding guess is worse than a blank — nobody re-reads a field that looks filled in.

## When there is nothing to do

If `.activeloom-config.yml` does not exist, stop and say so. Run `npx activeloom init` first; this skill edits a config, it does not create one.

If it exists, work out what is still outstanding before doing anything:

- `TODO(activeloom):` markers in the config — steps 1, 2, and 4.
- Empty model or hook keys in `agent-loop.config` — step 3.

Report which of the two apply and do only those. If neither applies, say the repository is already onboarded and stop. Never rewrite a value a human chose.

## 1. Read before you draft

Read enough to write each value from evidence. <<SUBAGENT_GUIDANCE>>

Establish, in roughly this order:

- **What the project is.** <<AGENT_DOC_CODE>>, then `README.md`. If both are absent or uninformative, the package/module metadata and the top-level directory names.
- **The stack.** Manifests and lockfiles, not guesses: `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`. Name versions only where a manifest pins one.
- **How it is tested and linted.** The declared scripts, plus the CI workflow — CI is the authority on what actually gates a merge, and it often runs more than the scripts do.
- **The conventions.** Commit message format from `git log`, lint configuration, formatter configuration, and any `CONTRIBUTING.md`.

## 2. Draft each marker

| Key | What it must contain |
| --- | --- |
| `PROJECT_OVERVIEW` | Two or three sentences: what the project does and who uses it. No marketing. |
| `CANONICAL_DOCS` | Inline-code paths to the docs a reviewer should consult. Only files that exist. |
| `STACK_TABLE` | A Markdown table of layer and technology. Add a short "common mistakes to flag" list only if the repository gives you evidence for one. |
| `CODE_RULES` | Bullets a reviewer can act on: the commit convention, the type strictness, the import rules, the paths that are generated and must not be hand-edited. |
| `REVIEW_FOCUS` | A numbered list, most important first. Replace the generic default only where this repository justifies something more specific. |
| `DOMAIN_RULES` | Usually empty. Fill it only for a real domain constraint — a regulatory rule, a wire format, a compatibility guarantee. |

Two rules about content:

- **Every claim must be traceable to a file you read.** If you cannot point at the evidence, leave the marker in place and say which one you could not resolve.
- **Values must be valid Markdown and Prettier-clean.** They are substituted into `.github/copilot-instructions.md`, which the sync checks with the repository's Prettier configuration. A malformed table fails that check in every consumer at once.

## 3. Engines and models

`init` installs the full skill set. Several skills cannot run without an engine the user actually has, and one — `agent-loop` — requires **both** the Claude and Codex CLIs and refuses to run without both. Its preflight checks the hook strings rather than the CLIs, so a missing engine surfaces only after the loop has opened a draft PR.

None of this is detectable. A binary on `PATH` does not tell you whether the user has a working plan, which models it exposes, or which engine they actually drive sessions with. **Ask.**

### Ask, in this order

1. **Which engines do you drive sessions with?** Reconcile the answer against `harnesses:` in `.activeloom-config.yml`. If they differ, say so and ask which is right — a harness in the config that the user does not run means synced prompts nobody reads.
2. **Do you have both the Claude and Codex CLIs?** If not, say plainly that `agent-loop` will fail partway through a run, and offer to leave its config empty. Do not write half a roster.
3. **Which model for the `agent-loop` worker, and which for each reviewer?** Ask for the exact model identifier.

### Never invent a model identifier

Model names change, and one you recall may not exist or may not be on the user's plan. A wrong identifier is the worst kind of wrong here: it is committed, it looks deliberate, and it fails at the point of use rather than at the point of writing.

Take the identifier from the user, or from a command they run — `<<ENGINE_CLI>>` and the other engine CLIs can report what they are configured for. Never supply one from memory, and never "fill in a sensible default".

### What to write

Only into `.claude/skills/agent-loop/agent-loop.config` (or the equivalent root for the harness), which is consumer-owned and never overwritten by a later sync:

| Key | Value |
| --- | --- |
| `worker_model` | The exact identifier the user gave. Left empty, the worker silently follows whatever the CLI defaults to that week. |
| `worker_fallback_model` | Only if the user names one. |
| `claude_review_hook` | A literal shell command. Model and effort are flags on it — there are no dedicated keys. |
| `codex_review_hook` | Same, for the other engine. |
| `claude_effort_policy` | The literal effort string the user chose. |

The file's own comments show the flag placement for each hook and name the environment variables a hook must carry, or preflight rejects it. Follow them rather than composing a command from scratch.

Leave a key empty rather than guessing it. An empty key is a documented "not configured yet"; a wrong one is a run that fails late.

## 4. Present for confirmation

Show the drafted values before writing them. For each one give the value and the evidence in a single line — the file you took it from.

Ask explicitly about anything you had to leave marked, and about anything where the repository offered two plausible readings. Do not smooth over a genuine ambiguity by picking one.

## 5. Write, then verify

After the user confirms, write the values into `.activeloom-config.yml` — editing only the marked keys, leaving everything else untouched.

Then verify against the real engine rather than by re-reading your own edit:

```
npx activeloom init --dry-run
```

A clean dry run means the config parses and the engine renders every target from it. It does **not** mean the values are filled in: `init` writes the unfilled keys as marker *values*, so `PROJECT_OVERVIEW: TODO(activeloom): ...` substitutes as cleanly as real prose and a wholly untouched config passes.

The marker scan is therefore the actual gate, not a closing formality. Grep the rendered output — not just the config — for `TODO(activeloom):`, and treat any hit as unfinished work rather than reporting the repository verified. Then tell the user which files change on the next real `init`.

## What this skill does not do

- It does not choose the tier. That is `npx activeloom init` with or without `--sync`, and it is the user's call.
- It does not set secrets, and it does not create a GitHub App. Those belong to Tier 3 and to a human.
- It does not invent a model identifier, and it does not pick one for the user. See step 3.
- It does not commit. Leave the change staged for the user to review.
