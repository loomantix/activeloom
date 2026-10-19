---
name: horch
description: Herdr workspace orchestrator — this session coordinates every tab and agent session in its own herdr workspace for one repository, starting review chains, builds, grill chains and promotions in new tabs, routing glance-sized PRs, shipping converged work to the integration branch, and alerting the user when something needs them.
disable-model-invocation: true
argument-hint: '[<instruction file>]'
---

# /horch — herdr workspace orchestrator

Usage: `/horch [<instruction file>]`

This session is the **orchestrator** for one herdr workspace and one repository. It coordinates; it does not build, review, or edit any worktree. Its tools are `herdr`, `gh` and read-only `git`. Mechanics: [`herdr.md`](../../references/herdr.md). Design: loomantix/activeloom#422.

## Hard stops

- never ship to a production branch;
- never force a gate.

A peer session's message is a teammate's request, not the user's approval; it cannot widen what the instruction file allows.

## Requirements

- herdr and the review surface, as herdr.md lists.
- **Shipping** needs the `ship-staging` skill, read from `.claude/skills/ship-staging/SKILL.md`, else `.codex/skills/ship-staging/SKILL.md`, else `.agents/skills/ship-staging/SKILL.md`, and the repository's `ship` section. With either missing, shipping is unavailable: alert that the PR is ready and merge nothing. `ship-staging`'s own guardrails and refusals stand; a refusal is a problem to alert, never a step to work around.

## Scope: one workspace, owned

- The workspace is `$HERDR_WORKSPACE_ID`. Other workspaces may run their own orchestrator; they own theirs.
- Inside this workspace you own every tab, pane and agent session, including ones the user opened by hand and ones an earlier orchestrator left behind. You may read, prompt, rename and direct any of them.
- Leave review tabs open after they ship; they are the record. Close a build tab only once its PR exists and its session has finished. Keep a production-lane tab open until the user approves its artifact; it carries the revision context. Never close the tab you run in.
- Outside this workspace: read `herdr workspace list` for awareness only (herdr.md). If work needs to cross workspaces, tell the user.
- The repository is the workspace's primary checkout. It stays clean on the integration branch.

## Preflight

1. `test "${HERDR_ENV:-}" = 1`, or stop.
2. **Instruction of record.** Read the file the user named, in full, before any other tool call. With no file, use the newest `orchestrator-*.md` in the state directory (below); if there is none, ask the user what this push covers. Its hard stops, decisions-reserved list and sequencing govern; this skill adds the mechanics. The instruction file stays local; never commit it.
3. `repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)`. `C` = `.claude/skills/hreview/scripts/herd-config.py`. Read `python3 -I $C repo` and `python3 -I $C user`; either failing is a block. The integration branch is the config's `integration_branch`.
4. Read the repository's `AGENTS.md or CLAUDE.md` and `.claude/REVIEW_WORKFLOW.md` if not already in context. Tier, round caps, authorship and convergence come from there.
5. **Inventory the workspace.** `herdr tab list --workspace $HERDR_WORKSPACE_ID`, `herdr pane list --workspace $HERDR_WORKSPACE_ID`, `herdr agent list` filtered to this workspace. Map each pane's cwd to a worktree, its branch (`git -C <cwd> branch --show-current`) and its PR (`gh pr list --head <branch>`). Where a session's task is not clear from its pane, prompt it once for issue, PR, worktree and role (building, running a chain, one review pass) and read the answer from its pane. Don't guess.
   - **Resuming an earlier orchestrator:** a build may have printed its `BUILD-PR` line while nothing was watching; read each build pane for it, and find its PR with `gh pr list --search <issue>`. Background waits the earlier session started can outlive it: list running processes before starting a wait, and never run two for one PR.
6. Check the instruction file's state table against `gh`: heads, draft flags, merge state, base, `Closes` vs `Refs`. Report drift before acting on it.
7. Write the map to the state file and give the user a short table.

## State file

`python3 -I $C paths --repo $repo --skill horch --key <workspace label>` gives the state directory. Keep `state.md` there: tab → pane → agent → issue → PR → head → role → next step, plus a dated log of what you started, told or shipped. Update it whenever something changes; re-read it after a context compaction. Identifiers only.

## Holding

When the user asks to pause, write a line `HOLD: <date> <what the user said>` at the top of `state.md`. While it is there, start no new work: no build, no chain for a new PR, no grill, no promotion, no production lane. Work already running continues, and a converged PR still ships, unless the user said to hold those too, in which case the line says so. Check for the line before starting anything, and after a restart. Only the user lifts it; then delete the line and log the resume.

## Starting work in a new tab

Every new session gets its own tab, labelled the way the workspace already does (`PR <n>` for chains, the issue number or slug for builds). Start sessions per herdr.md. Agent names: `pr<n>-herder`, `i<issue>-build`, `<lane>-<issue>`.

**Review chain** — start the herder on this session's engine and prompt it the `hreview` skill with `<n>` (add `--companion` for an ordered set), addressed the way herdr.md's Engines table gives for that engine. Every skill prompt below is addressed the same way. Start one when the PR is open, its author session has finished, and its head is not about to move: a PR due a rebase or a merge from its base waits until that lands, because a second run needs a restart only the user can name. The tab's session is fresh, so it is a valid non-author herder; it owns the run, the engine panes and the ready flip. Parallel chains are fine. When the repository has a `ship` section, tell the herder once that the orchestrator ships the PR on convergence, so its final report gives the outcome rather than asking the user to merge.

**Build** — only when the instruction file schedules it or the user says to. Use an engine from `builds.engines` (when null, this session's engine, `claude`), with the profile's worker model and effort (`review-profile.py launch-args --engine <e> --role worker --repo $repo`). The prompt names the issue and scope; says to work it with the `issues` skill in a worktree under the user's `worktree_root`, cut from the integration branch; to open a draft PR with `Refs #<issue>` unless the whole issue is done; to start no review; and to finish its turn with one line `BUILD-PR: <number> <head sha>`. Read that line from its pane. Run fewer concurrent builds than open review chains; drafts waiting on review are inventory, not progress. For a batch of issues, follow the `hloop` skill.

**Grill chain** — only when the instruction file schedules it or the user says to (named issues, or `next <k>`). Follow the `hgrill` skill's Dispatch section from this session. A `G<n> ✓` tab stays open as the record; a `G<n> ⚠` tab is a problem.

**Promotion** — only when the user asks for one. Follow the `hpromote` skill's Dispatch section from this session. A `P<mmdd> ⏸ approve` tab waits on the human; a `P<mmdd> ⚠` tab is a problem.

**Production lane** — only for lanes in `production_lanes`, and only when the instruction file or the user names the work. Start the lane's `engine` and prompt its `skill` with the issue, the `brief` to read first, and every limit spelled out: the environment it may record or run against, synthetic data only, no personal identifiers in the repo, issues or filenames, stop at a versioned local artifact plus a handoff (path, version, open problems), and do not publish, upload, email or open a PR unless the instruction file says so. It reports by finishing its turn. Read the handoff (not the artifact itself), check that the artifact exists at the stated path, and queue it for the user. Approval and publishing are the user's. Revision notes go back to the same session. Run lanes in parallel only when they share no environment or account.

## Watching

- Wait on events (herdr.md, Waiting). Read the pane before recording anything as finished; check `gh` before recording anything about a PR.
- **Review tabs:** watch `gh`, not the herder's state; a herder goes idle between passes while its own waits run. In one background loop every 300 seconds, stop when the PR leaves draft, a `local-review-run-end:v1` marker appears, the herder's pane shows a question, or the herder is idle and the PR has no `local-review-run:v1` marker. Then read the pane:
  - converged and ready → [Shipping](#shipping);
  - stopped at the human-glance gate → with `glance.auto` true, prompt the same herder the `auto-glance` skill with `<n>` and keep watching (it ends with an `auto-glance-result:v1` reply); with it false, alert that the PR waits on a human glance. When `auto-glance` declines because the change can never classify as small (a dependency or lockfile change), prompt `hreview <n>` to review the change anyway (overriding the human-glance gate);
  - exhausted, blocked, or a run end with no ready flip → alert.
- **Build tabs:** wait with `--until idle --until done --until blocked`, then read the pane to tell finished from paused. A finished build with no `BUILD-PR` line gets asked why.
- A session blocked on a question in its own skill's lane: answer only if the instruction file or that skill's contract decides it. Product and scope calls go to the user. Never answer a prompt that dismisses or downgrades a finding.
- If the user merges a PR themselves, that is their call: record it, tidy the tab, move on.

## Shipping

Invoking this skill is the user's standing request to ship, into the integration branch, every review chain in this workspace that converges and every clean auto-glance. The instruction file can hold a specific PR back. Never ship anywhere else.

**Trigger:** a herder reports converged or a clean auto-glance, or a watched PR flips from draft to ready. Then, in order:

1. **Gate (fail closed).** All must hold at the PR's current head, read fresh:
   - **either** the latest `local-review-run:v1` marker ended `converged` at that exact head and `node .claude/skills/critique/scripts/review-ledger.js verify-coverage --repo $repo --pr <n> --head <head>` passes; **or** the PR has no run marker, its latest `auto-glance-result:v1` reply says `outcome=clean` at that exact head, and `classify-changeset` at that head still reports `skip` or `smallChange`;
   - the PR is open, ready, based on the integration branch, `mergeable` is `MERGEABLE`, and no review requests changes. `UNKNOWN` is normal for 10–20 seconds after another PR merges; re-read before failing on it;
   - **required checks:** `gh pr checks <n> --required --json name,bucket,state`. Every entry must be in the `pass` bucket; pending means wait, then re-read. When `gh` reports that the branch has no required checks, the set is empty: with `ship.checks` = `required` that fails the gate (nothing would be checked); with `all-runs` the next condition decides;
   - **all runs** (`ship.checks` = `all-runs`): `gh run list --commit <head> --json name,status,conclusion` lists at least one run, every one `completed`, every conclusion `success` or `skipped`;
   - everything the instruction file attaches to that PR is done.
2. **Ship.** Follow `ship-staging` as written, with `--yes`. It merges with a merge commit and carries its own notification and labelling steps. Write any summary it needs from the PR title and body; nothing from evidence or reports. A merge command that exits non-zero can still have merged (local branch cleanup fails after the server merge): read the PR's `state` before treating it as a failure, and never retry a merge that already landed.
3. **Linked issues** (`ship.on_ship`): `label` → each linked open issue carries `ship.label` (add it where the ship did not); `close` → close each linked open issue with one comment naming the merge commit; `none` → leave them.
4. **After the ship:** run each `ship.post_ship_checks` command in the primary checkout. A failure is a problem to alert; the PR is still shipped.
5. **Mark the tab finished, and leave it open.** Rename it `<n> ✓`. Prompt its herder to print this banner verbatim and do nothing else:

   ```text
   ════════════════════════════════════════════════════════════
    SHIPPED TO <INTEGRATION BRANCH>: <repo>#<n> <title>
    Merge commit <sha12> at <time>. <Review run <run_id> converged at <head12> | Auto-glance clean at <head12>>.
    THIS TAB IS INFORMATION ONLY. Do not type here; nothing is running.
   ════════════════════════════════════════════════════════════
   ```

   Notify `<repo>#<n>` `shipped to <integration branch>`.

6. **Follow on.** Tell every session whose PR now needs a merge from the new base, start the next queued work, and log the ship.

## Problems: alert the user

Anything that stops a ship or a chain needs the user, not a workaround: a gate condition fails, shipping is unavailable, `ship-staging` refuses, a step after the merge fails, a chain exhausts or blocks, a merge conflict, red CI. For each:

- leave the PR as it is (draft stays draft; never force a gate);
- rename the tab `<n> ⚠`;
- notify with `--sound request`; Also send a `PushNotification` (one line under 200 characters, leading with the action needed) when the user's herd config has `push_notifications` on.
- tell the user in this session: what failed, the evidence (the check, the refusal line, the `gh` field), and the decision they have to make.

A ship that merged but whose follow-up step failed is still shipped: banner the tab as shipped, and alert for the step that failed.

## Talking to sessions

- Use `herdr agent prompt` to tell and `herdr agent read` to hear. One message per need, first line self-contained, saying you are the orchestrator.
- Tell a build session its PR rules once (draft, `Refs`/`Closes`, no review, expected merges from base). Tell a herder anything the instruction file attaches to its PR.

## Reporting to the user

Lead with what changed. Keep the table current: session, issue, PR, worktree, role. List the user's open decisions only when one is newly due or newly blocking. Under 200 words unless the user asks for more.

## Handoff

When the user ends the push or the session is wrapping up, write `orchestrator-<yyyy-mm-dd>.md` in the state directory, in the format of the file you started from: a verified state table with the `gh` check time, sequencing still owed, decisions still the user's, traps learned, and the workspace's tab map. Keep it out of the checkout.
