# herdr mechanics for the orchestration skills

Shared mechanics for `hreview`, `auto-glance`, `hgrill`, `horch` and `hpromote`. The skills hold the flows; this page holds how to drive herdr from any harness. Design: loomantix/activeloom#419 and loomantix/activeloom#422.

## Prerequisites

- herdr 0.9.0 or later, and this session running inside it: `test "${HERDR_ENV:-}" = 1`. Otherwise say so and stop.
- `gh` authenticated for the repository, `git`, `jq`, Python 3.
- The review surface the skill names (ledger helper, run controller, review profile) installed in the checkout.

## Configuration

The config helper ships with `hreview`: `python3 -I <skills root>/hreview/scripts/herd-config.py <command>`. Every command prints JSON; exit 1 is an invalid config, 2 a git or usage error, 3 a held lock. Treat 1 and 2 as blocks.

| Command                                             | Use                                                                                                                           |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `repo`                                              | `.activeloom-herd.json` from the integration branch at `origin`, with defaults applied. Never read the copy in a PR worktree. |
| `user`                                              | `~/.config/activeloom/herd.json` (or `$ACTIVELOOM_HERD_USER_CONFIG`) with defaults applied.                                   |
| `user-args --engine <e> [--read-only]`              | The user's extra launch argv for one engine. `--read-only` drops the permission-off arguments.                                |
| `gate-fix-paths --base <sha> --head <sha>`          | Classify a gate fix's changed paths against the repository's `gate_fix` globs.                                                |
| `paths --repo <owner/name> --skill <s> [--key <k>]` | Create and print the private state and scratch directories for this repository and skill.                                     |
| `lock` / `unlock` (same arguments as `paths`)       | Take or release an exclusive lock. `lock` exits 3 when another run holds it.                                                  |
| `validate [<file>]`                                 | Check a working-tree `.activeloom-herd.json` while authoring it.                                                              |

State files hold identifiers only: pane and tab IDs, PR and issue numbers, heads, outcomes, URLs, timestamps. No issue text, evidence, customer data or tokens. Scratch never goes in a worktree.

## Engines

Models and efforts come from the review profile: `python3 -I <skills root>/review-setup/scripts/review-profile.py launch-args --engine <e> --repo <owner/name>` prints the model on line 1 and the effort on line 2 (`--role worker` for builds). Append the user's `user-args` for that engine. Never add a flag that is already present: a flag passed twice makes the Codex CLI exit.

| Engine   | herdr `--kind` | Model and effort after `--`                | Read-only session        | Invoke a skill                                    | End the session |
| -------- | -------------- | ------------------------------------------ | ------------------------ | ------------------------------------------------- | --------------- |
| `claude` | `claude`       | `--model <m> --effort <e>`                 | `--permission-mode plan` | `/<skill> <args>`                                 | `/exit`         |
| `codex`  | `codex`        | `-m <m> -c 'model_reasoning_effort="<e>"'` | `-s read-only`           | `Use .codex/skills/<skill>/SKILL.md for <target>` | `ctrl+c ctrl+c` |
| `gemini` | `agy`          | `--model <m> --effort <e>`                 | `--mode plan`            | `/<skill> <args>`                                 | `/exit`         |

For a Claude pane, also set the pane environment when splitting it: `--env ANTHROPIC_MODEL=<m> --env CLAUDE_CODE_EFFORT_LEVEL=<e>`. A read-only session takes `user-args --read-only`.

## Tabs and panes

- Read every ID from JSON: `herdr tab create` returns `.result.root_pane.pane_id` and `.result.tab.tab_id`; `herdr pane split` returns `.result.pane.pane_id`. Never act on the UI-focused pane, and pass `--no-focus`.
- New tab: `herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd <dir> --label "<label>" --no-focus`.
- A row below this pane: `herdr pane split "$HERDR_PANE_ID" --direction down --cwd <dir> --no-focus`. More panes in the row: `herdr pane split <previous> --direction right --cwd <dir> --ratio <1/k> --no-focus`, where k is the number of panes still to fit (three panes: `0.3333`, then `0.5`).
- Titles: `herdr pane rename <id> "<title>"`, `herdr tab rename <tab> "<label>"`. Keep tab labels short (number and mark). Marks: `▶` running, `✓` done, `⚠` needs the user, `⏸` waiting on a human action.
- Work only in this session's workspace (`$HERDR_WORKSPACE_ID`). Read other workspaces for awareness; never prompt, split, rename or close anything in them.

## Starting a session

1. The pane must be at its shell prompt. A new tab's shell takes a few seconds to draw it, and `agent start` returns `agent_pane_busy` until then: retry up to 5 times, a few seconds apart. Before reusing a pane, end any earlier session in it and confirm with `herdr pane process-info --pane <id>` that the shell is in the foreground.
2. `herdr agent start <name> --kind <kind> --pane <id> --timeout 120000 -- <model and effort args> <user args>`. Names are unique per workspace; the skills give the pattern.
3. `agent_not_ready` means a startup dialog is up. Read it (`herdr agent read <name> --source visible --lines 60`) and answer:
   - **Folder trust:** trust only the primary checkout or a worktree this run created.
   - **Update banner or prompt:** dismiss it (`herdr agent send-keys <name> esc`). Never run an update from a chain.
   - **Anything else:** if the skill's contract does not decide it, it is a block.

   Then `herdr agent wait <name> --until idle --until done --timeout 120000`.

4. A launch that exits at once usually means a duplicated or unknown flag. Check `type <cli>` for a shell alias that adds flags, and the user's `user-args`. The user's config is the place for permission-off flags, not an alias.

## Prompting

- Always `herdr agent prompt <name> "<text>" --wait --timeout <ms>`. Without `--wait`, text can be dropped while an input box is still loading; `--timeout` is accepted only with `--wait`.
- `--wait` can time out while the session is already working. Before resending, count the prompt's first line in `herdr agent read <name> --source recent-unwrapped --lines 400`; resend only when it is absent.
- `agent_blocked` means the session is waiting on a question: read and answer it first.
- A greyed suggestion in an input box is the client's autosuggestion, not input. Never send `tab` or `right` to a session; they accept it.
- Free text goes through `agent prompt`; menu choices through `herdr agent send-keys <name> <keys>`.

## Status is a hint; the pane is the state

herdr's `idle`, `done`, `working` and `blocked` say when to look, not what happened. Before recording a session as finished, relaying its result or starting the next step, read its last lines (`herdr agent read <name> --source recent-unwrapped --lines 120`) and check:

- **Claude finished:** a `✻ <verb> for …` line after the last message, no spinner line, no tool call still `Running…`.
- **Codex finished:** the final summary, then a `Worked for …` line and an empty `›` prompt, with no `Working (…)` or `background terminal running` line after it. Codex settles at `idle`, never `done`; it also shows `idle` while waiting on its own background terminal and can flash `done` between steps of one turn.
- **Agy finished:** the input prompt is back and two reads ten seconds apart are identical.
- **Waiting on a question:** the last message asks something, or a dialog or menu is open. That is blocked, whatever herdr says.
- **Anything else:** still working. Wait again.

A status that disagrees with the pane is normal. Trust the pane, and for anything about a PR or issue, trust `gh`.

## Waiting

One rule on every harness: run the wait as a blocking command. If the harness moves it to the background, end the turn and resume when it completes. Never replace it with a loop of short sleeps.

- A session: `herdr agent wait <name> --until <state> [--until <state>] --timeout 600000`.
- After answering a blocked UI, wait with `--until working --until done --until idle`. A bare wait matches the stale `blocked` at once.
- State that only `gh` shows: one shell loop that checks the condition every 300 seconds and exits when it holds, run the same way.
- A tab another session drives: poll `herdr tab list --workspace "$HERDR_WORKSPACE_ID"` for its label in the same kind of loop.

## Stalls and crashes

- **Stall:** on each `timeout` while working, hash `herdr agent read <name> --source recent-unwrapped --lines 200`. Two consecutive timeouts with an unchanged hash mean a stall: send `esc`, then prompt `Continue from where you stopped.`
- **Crash:** `agent_not_found`, or the shell back in the foreground. Relaunch in the same pane with the same prompt plus the skill's recovery line. At most two relaunches per unit of work; a third failure is a block.
- **Stale blocked state:** herdr can keep reporting `blocked` after the question is answered. Read the pane; if it is working, wait with `--until done --until idle`.

## Ending a session

Send the engine's exit (table above), then confirm the shell is in the foreground with `herdr pane process-info --pane <id>`. Retitle the pane with its outcome.

## Notifications

- Routine (started, landed, relaunched): `herdr notification show "<title>" --body "<one line>"`.
- Needs the user (converged, exhausted, ready for a human action, any block): the same with `--sound request`, plus the harness's push notification when the user's config has `push_notifications` on. One line under 200 characters, leading with the action needed.
- The title is `<owner/name>#<n>` or `<owner/name> <activity>`. Never put issue text, findings or customer data in a notification.

## Blocks

A block stops one unit of work, never the others. Leave GitHub as it stands, keep locks and worktrees, title the pane with the reason, mark the tab `⚠`, notify as above, and tell the user what failed, the evidence (the command and its output line, or the `gh` field) and the decision that is theirs.
