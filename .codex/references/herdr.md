# herdr mechanics for the orchestration skills

Shared mechanics for `hreview`, `auto-glance`, `hgrill`, `horch`, `hloop` and `hpromote`. The skills hold the flows; this page holds how to drive herdr from any harness. Design: loomantix/activeloom#419 and loomantix/activeloom#422.

## Prerequisites

- herdr 0.9.0 or later, and this session running inside it: `test "${HERDR_ENV:-}" = 1`. Otherwise say so and stop.
- `gh` authenticated for the repository, `git`, `jq`, Python 3.
- The review surface the skill names (ledger helper, run controller, review profile) installed in the checkout.
- The [permissions preflight](#permissions-preflight), including its [Claude auto-mode check](#claude-auto-mode), before the first engine launch.

## Configuration

The config helper ships with `hreview`: `python3 -I <skills root>/hreview/scripts/herd-config.py <command>`. Every command prints JSON; exit 1 is an invalid config, 2 a git or usage error, 3 a held lock. Treat 1 and 2 as blocks.

| Command                                                                                          | Use                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `repo`                                                                                           | `.activeloom-herd.json` from the integration branch at `origin`, with defaults applied. Never read the copy in a PR worktree.                                                               |
| `user`                                                                                           | `~/.config/activeloom/herd.json` (or `$ACTIVELOOM_HERD_USER_CONFIG`) with defaults applied.                                                                                                 |
| `user-args --engine <e> [--read-only]`                                                           | The user's extra launch argv for one engine. `--read-only` drops the permission-off arguments.                                                                                              |
| `gate-fix-paths --base <sha> --head <sha>`                                                       | Classify a gate fix's changed paths against the repository's `gate_fix` globs.                                                                                                              |
| `paths --repo <owner/name> --skill <s> [--key <k>]`                                              | Create and print the private state and scratch directories for this repository and skill.                                                                                                   |
| `lock` / `unlock` (same arguments as `paths`)                                                    | Take or release an exclusive lock. `lock` exits 3 when another run holds it.                                                                                                                |
| `validate [<file>]`                                                                              | Check a working-tree `.activeloom-herd.json` while authoring it.                                                                                                                            |
| `permissions --engine <e> [--engine <e>]`                                                        | Per engine: whether sessions that edit stop at approval prompts, which configured flags turn a check off, any such flag in `launch_args`, and what a shell alias for the engine's CLI adds. |
| `claude-settings --engine <e> [...] [--session-mode <m>] [--project-dir <dir>] [--draft <file>]` | Whether Claude auto mode is in use, and which Claude settings entries the herd needs then. Writes a draft settings file when some are missing; never writes Claude settings.                |

State files hold identifiers only: pane and tab IDs, PR and issue numbers, heads, outcomes, URLs, timestamps. No issue text, evidence, customer data or tokens. Scratch never goes in a worktree.

## Permissions preflight

Every herd skill runs this before its first engine launch. Run `permissions` with an `--engine` for every engine this run may start. A session the skill starts read-only takes `user-args --read-only`, which still passes `launch_args`: for an engine this run starts only read-only, act on `turned_off_in_read_only` and `unrecognized` and make no offer. Act on each engine's entry before launching anything.

The report reads `herd.json` and, for aliases, `type <cli>` in the user's shell, as its `scope` says. An engine's own config, such as Codex `~/.codex/config.toml` or Claude settings, can turn a check off without showing here, so never present an empty `turned_off` as proof that every check is on.

- **`unrecognized` is not empty.** Name those flags and say the helper cannot tell what they turn off. Make no offer for that engine: an added flag could repeat one of them under another spelling, and the Codex CLI exits on a repeated flag.
- **`stops_at_prompts` is true and nothing is unrecognized.** Its panes stop at every approval prompt, and the run answers them with `herdr agent send-keys`. This session's own harness may refuse to approve another agent's command it cannot inspect, which blocks the run partway through. Tell the user, and offer the tiers below, narrowest first, each with its line on what it removes. Link the guide on [running unattended safely](https://github.com/loomantix/activeloom/blob/main/docs/herd-orchestration.md#running-unattended-safely): these sessions read pull request and issue text that anyone may have written, and hold an authenticated `gh`.

  | Engine   | Narrower: prompts end, a check stays                                                                                                    | Full bypass                                                                                                                  |
  | -------- | --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
  | `claude` | `--permission-mode auto`: a classifier allows or refuses each action in place of the prompt; a refused action fails back to the session | `--dangerously-skip-permissions`: every permission check                                                                     |
  | `codex`  | `--approve-for-me`: the workspace-write sandbox stays; an automatic reviewer decides each request to leave it                           | `-a never -s danger-full-access`, the same as `--dangerously-bypass-approvals-and-sandbox`: approval prompts and the sandbox |
  | `gemini` | none: `--mode accept-edits` approves file edits only, so panes still stop at commands                                                   | `--dangerously-skip-permissions`: every permission check                                                                     |

  Codex `-a never` with the workspace-write sandbox kept is not a tier. Sessions that edit run `gh` and `git push`, which need the network, and a commit in a linked worktree writes into the primary checkout's `.git`, outside the worktree. The sandbox blocks both, and with `-a never` those commands fail instead of asking. A narrower tier can refuse the same commands; if a chain stalls on refusals, the full bypass is what is left, and it belongs in an isolated environment.

  Add only the flags the entry lacks, never one `launch_args` or `permission_off_args` already holds under either spelling. An entry that already holds `-s danger-full-access` is one flag short of the full bypass: adding `-a never` to it is the full-bypass tier, so offer it as that tier with that tier's line, never as a fix-up. Write the chosen tier to `herd.json` under that engine's `permission_off_args` only on the user's explicit choice, then run `permissions` again. If they decline, continue.

- **`turned_off` is not empty.** Tell the user in one line per engine what its sessions run without, for example `codex: -a never -s danger-full-access turns off approval prompts and the sandbox`. Do not ask again; the setting is theirs.
- **`turned_off_in_read_only` is not empty.** Those flags are in `launch_args`, so read-only sessions run without the check too. Tell the user and recommend moving them to `permission_off_args`.
- **`alias.repeats` is not empty.** A shell alias for that engine's CLI adds a flag the herd also passes, and the CLI exits on a repeated flag. Block that engine: show `alias.definition`, name the repeated flags, and tell the user to remove them from the alias and keep them in `herd.json`.
- **`alias.turned_off` is not empty.** The alias turns a check off for every session, read-only ones included. Tell the user and recommend moving the flag to `permission_off_args`.
- **`alias.kind` is `function`.** The helper cannot see what a shell function adds. Say so, and ask the user to confirm it adds no flag `herd.json` holds.
- **`alias.checked` is false.** The shell probe failed, timed out, or printed nothing about the CLI, so the empty `repeats` and `turned_off` prove nothing. Say the alias could not be checked, and ask the user to confirm that `type <cli>` adds no flag `herd.json` holds.

A session that another session started has no user to ask: it reports these in its first message and continues as configured. The session the user invoked asks. Never write `herd.json` without the user's choice. That is a rule the session follows, not one the helper enforces: the session edits the file itself.

### Claude auto mode

Claude Code's auto mode puts a classifier in front of each action. Without entries in the user's Claude settings it refuses ordinary herd actions: starting a Codex or Agy session with `herd.json`'s permission-off flags reads as creating an unsafe agent, and reading or answering a pane's startup dialog reads as bypassing auto mode. A chain that meets either refusal stops partway through, so check before the first launch.

Run `claude-settings` with an `--engine` for every engine this run may start, after `herd.json` is settled. When this session is Claude Code, add `--session-mode <its permission mode>` (`auto` while auto mode is active). Add `--project-dir <primary checkout>` so the checkout's `.claude/settings.json` and `.claude/settings.local.json` count. Pass `--draft <scratch dir>/claude-settings.json` once the skill has a scratch directory. The helper counts auto mode as in use when this session runs in it, when `herd.json` starts Claude sessions in it (those sessions drive panes too), or when a settings file sets `permissions.defaultMode` to `auto`.

- **`ready` is true.** Continue. When `auto_mode.in_use` is false, say nothing.
- **`ready` is false.** Block before any launch. Tell the user which reason in `auto_mode.because` applies, then list each `missing` entry with its `setting` and `why`, and each `conflicts` deny or ask rule that blocks unattended use of a herd command. When a draft exists, give the `draft.path` (the user's settings with only the missing entries added) and the `draft.copy_command`, which backs up the settings file, validates the draft with `jq -e`, then copies it into place. Never print the draft: it copies the whole settings file, secrets under `env` included, and the `missing` entries are exactly what it adds. Ask the user to read the draft and run the command themselves (in Claude Code, `! <command>`), then rerun `claude-settings`. Continue once it reports `ready`. Never run the copy command or edit Claude settings yourself. Rules in `conflicts` are the user's to change; the draft never removes them.
- **`defaults` is `replaced`.** The user's `autoMode.allow` list leaves out `$defaults`, so Claude's built-in rules are off. The draft keeps it that way. Mention it once.

The settings take effect without a restart. Build the draft only after `herd.json` is final: the launch entry names its permission-off flags, and a flag added later makes the check fail again. A session that another session started reports a `ready: false` result in its first message and stops; it has no user to ask.

In auto mode, run each herdr command as its own shell command. The classifier reads a compound command as one action, so a launch wrapped in status checks can be refused where each part alone passes.

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

4. A launch that exits at once usually means a duplicated or unknown flag. Run `permissions` again and read its `alias` entry for that engine, then the user's `user-args`. The user's config is the place for permission-off flags, not an alias.

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
