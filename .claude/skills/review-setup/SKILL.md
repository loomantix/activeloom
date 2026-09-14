---
name: review-setup
description: Set up or change the per-user review profile — the model, effort, and engine order that local review launchers and the automatic review chain use. Use when a launcher or the runner reports that no review profile exists, or when the user asks to change a reviewer's model or effort, or the order engines review a pull request in.
---

# /review-setup — per-user review profile

The profile is a file outside every repository. Launchers and the review-chain runner refuse to start without it, so no reviewer runs on a model, effort, or order the user did not confirm.

Run the helper beside this file: `python3 -I <this skill's directory>/scripts/review-profile.py <command>`. It is the only writer of the profile — never edit the JSON directly. Report a helper error to the user verbatim; do not retry with a guessed value.

## First run

1. Run `show`. If it reports `"configured": true`, go to [Changing a setting](#changing-a-setting).
2. Run `detect` and `defaults`. Present one table — engine, CLI installed, recommended model, recommended effort, note — then the recommended order for `lean` and `deep`.
3. Explain `inherit` where it appears: the engine CLI's own configured default model applies, so the model changes whenever that configuration does.
4. Ask the user to accept the recommendations or name changes. Take model identifiers from the user or from the engine CLI's own model listing; never supply one from memory. Tell the user an engine whose CLI is not installed cannot run until it is, and suggest leaving it out of the orders.
5. Write the profile: `init --accept-defaults`, followed on the same line by one assignment per change (for example `init --accept-defaults claude.effort=high order.lean=codex,claude`).
6. Run `show` and present the stored values.

Setup is complete when `show` reports `"configured": true` and the user has seen the stored values.

## Changing a setting

Show the current values with `show` (add `--repo OWNER/REPO` for a repository-specific change), state the change, and write it once the user confirms.

| Request                                 | Command                                                             |
| --------------------------------------- | ------------------------------------------------------------------- |
| "use sonnet for Claude reviews"         | `set claude.model=sonnet`                                           |
| "run Codex reviews at max effort"       | `set codex.effort=max`                                              |
| "fall back to Sol at medium"            | `set codex.fallback.model=gpt-5.6-sol codex.fallback.effort=medium` |
| "disable the Codex fallback"            | `set codex.fallback=none`                                           |
| "deep reviews go Codex, then Claude"    | `set order.deep=codex,claude`                                       |
| "in this repo, add Gemini to lean runs" | `set --repo OWNER/REPO order.lean=claude,codex,gemini`              |
| "drop this repo's Codex model override" | `unset --repo OWNER/REPO codex.model`                               |

`unset --repo OWNER/REPO` with no keys removes that repository's whole override. The change is complete when `show` (with the same `--repo`) prints the new value.

Codex's optional fallback is a complete, explicit model-and-effort pair. No fallback is enabled by default. Set both values together; a repository override replaces the whole pair. The automatic runner switches once after a recognized capacity rejection only when code and review evidence are unchanged, then keeps that fallback for the remaining Codex passes. It preserves the failed attempt and round budget and reports the actual model and effort. Standalone launchers do not retry. Other engines' fallbacks are not supported.

## Newer recommendations

When `show` reports a `defaults_version` older than `current_defaults_version`, tell the user the recommendations changed and show where `defaults` now differs from their stored values. Change nothing unless they choose to.

## Scope

- An automatic review run already in progress keeps the settings it started with; a change applies to the next run.
- This skill does not start reviews, resolve a review tier, install engine CLIs, or edit an engine CLI's own configuration.
