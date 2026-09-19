---
name: review-setup
description: Set up or change the per-user review profile — the model, effort, and engine order that local review launchers, agent-loop workers, and the automatic review chain use. Use when a launcher, the runner, or an entry skill's preflight reports that the review profile is missing or incomplete, or when the user asks to change a reviewer's or worker's model or effort, mark an engine unavailable, or change the order engines review a pull request in.
<<FM_EXTRAS>>
---

# <<INVOKE>>review-setup — per-user review profile

The profile is a file outside every repository. Launchers and the review-chain runner refuse to start without the settings they need, so no reviewer or worker runs on a model, effort, or order the user did not confirm.

Run the helper beside this file: `python3 -I <this skill's directory>/scripts/review-profile.py <command>`. It is the only writer of the profile — never edit the JSON directly. Report a helper error to the user verbatim; do not retry with a guessed value.

Model identifiers come only from the user or from the engine CLI's own model listing; never supply one from memory.

## Inline setup

An entry skill's review profile preflight sends the user here when `check` exits 3. Run this in the same conversation, then return to that skill and continue the user's original request without asking them to invoke it again.

1. Run `check`. If it reports `"configured": false`, follow [First run](#first-run) instead; it ends in the same state.
2. Run `show`, `detect`, and `defaults`. For each key in step 1's `missing`, show the value you propose: its entry in step 1's `suggested` (a worker setting pre-filled from that engine's reviewer setting), otherwise the value from `defaults`. Ask only about the keys in `missing`; every stored value stays as confirmed.
3. Ask the user to accept the proposals or name changes. For an engine whose CLI `detect` reports not installed, suggest declaring it unavailable. Accept "I don't have this plan" or "I don't use this engine" as that declaration: `ENGINE.availability=unavailable`, which also removes that engine's missing keys. An order may not name an unavailable engine, so use `show`'s stored orders to propose every required removal and write the order and availability changes in the same command once the user agrees.
4. Write the answers with one `set` command holding only the missing keys and any availability or order change the user agreed to (for example `set claude.worker.model=opus claude.worker.effort=medium gemini.availability=unavailable order.deep=claude,codex`).
5. Run `show` and present the stored values.

Inline setup is complete when `check` exits 0 and the user has seen the stored values.

## First run

1. Run `show`. If it reports `"configured": true`, go to [Inline setup](#inline-setup) when it lists `missing` keys, otherwise to [Changing a setting](#changing-a-setting).
2. Run `detect` and `defaults`. Present one table — engine, CLI installed, recommended reviewer model and effort, recommended worker model and effort, note — then the recommended order for `lean` and `deep`.
3. Explain `inherit` where it appears: the engine CLI's own configured default model applies, so the model changes whenever that configuration does.
4. Ask the user to accept the recommendations or name changes, including declaring an engine unavailable (step 3 of [Inline setup](#inline-setup)). A worker setting the user leaves alone follows the reviewer setting they choose for that engine.
5. Write the profile: `init --accept-defaults`, followed on the same line by one assignment per change (for example `init --accept-defaults claude.effort=high gemini.availability=unavailable`). The helper fits the proposal to those choices: unassigned worker values follow the engine's reviewer values, and unavailable engines leave the proposed orders.
6. Run `show` and present the stored values.

Setup is complete when `show` reports `"configured": true` with an empty `missing` list and the user has seen the stored values.

## Changing a setting

Show the current values with `show` (add `--repo OWNER/REPO` for a repository-specific change), state the change, and write it once the user confirms.

| Request                                 | Command                                                             |
| --------------------------------------- | ------------------------------------------------------------------- |
| "use sonnet for Claude reviews"         | `set claude.model=sonnet`                                           |
| "run Codex reviews at max effort"       | `set codex.effort=max`                                              |
| "fall back to Sol at medium"            | `set codex.fallback.model=gpt-5.6-sol codex.fallback.effort=medium` |
| "disable the Codex fallback"            | `set codex.fallback=none`                                           |
| "agent-loop workers use sonnet"         | `set claude.worker.model=sonnet`                                    |
| "I don't have a Gemini plan"            | `set gemini.availability=unavailable order.deep=claude,codex`       |
| "deep reviews go Codex, then Claude"    | `set order.deep=codex,claude`                                       |
| "in this repo, add Gemini to lean runs" | `set --repo OWNER/REPO order.lean=claude,codex,gemini`              |
| "drop this repo's Codex model override" | `unset --repo OWNER/REPO codex.model`                               |

Availability is global; a repository override cannot change it. `unset --repo OWNER/REPO` with no keys removes that repository's whole override. The change is complete when `show` (with the same `--repo`) prints the new value.

Codex's optional reviewer fallback is a complete, explicit model-and-effort pair. No fallback is enabled by default. Set both values together; a repository override replaces the whole pair. The automatic runner switches once after a recognized capacity rejection only when code and review evidence are unchanged, then keeps that fallback for the remaining Codex passes. It preserves the failed attempt and round budget and reports the actual model and effort. Standalone launchers do not retry. Every engine's worker settings take the same kind of optional pair (`ENGINE.worker.fallback.model` with `ENGINE.worker.fallback.effort`, or `ENGINE.worker.fallback=none`).

## Newer recommendations

When `show` reports a `defaults_version` older than `current_defaults_version`, tell the user the recommendations changed and show where `defaults` now differs from their stored values. Change nothing unless they choose to.

## Scope

- An automatic review run already in progress keeps the settings it started with; a change applies to the next run.
- A run with `AGENT_LOOP_NONINTERACTIVE=1` set, or one a launcher or runner started (`AGENT_LOOP_REVIEW_ENGINE` set), never comes here; it uses its pinned values.
- This skill does not start reviews, resolve a review tier, install engine CLIs, or edit an engine CLI's own configuration.
