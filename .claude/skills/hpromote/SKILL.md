---
name: hpromote
description: Promotion on herdr — opens a dedicated tab that runs the repository's promotion recipe across at most six visible panes (preflight commands, deploy and CI watchers, a read-only risk review of the whole promotion diff) and fix sessions that ship confirmed blockers to the integration branch. Stops at the human approval and merge, then watches the production deploy.
disable-model-invocation: true
argument-hint: '[--dry-run] | run [--dry-run] | merged'
---

# /hpromote — integration-to-production promotion on herdr

Usage:

- `/hpromote [--dry-run]`: open the promotion tab and run it.
- `/hpromote run [--dry-run]`: internal. The driver inside the tab; the dispatcher sends it.
- `/hpromote merged`: internal. The user, or the dispatcher relaying the user, reports the promotion PR merged. The driver runs [Post-merge](#post-merge).

Two roles:

- **Dispatcher**: the session the user invoked, or `/horch`. It opens one tab, `P<mmdd>`, starts the driver in it, and watches the tab label. It does nothing else.
- **Driver**: the session in the tab's first pane. It runs the phases below and owns every pane in the tab.

Mechanics: [`herdr.md`](../../references/herdr.md). Design: loomantix/activeloom#422.

## Configuration

`C` = `.claude/skills/hreview/scripts/herd-config.py`; `R` = `.claude/skills/promote/scripts/promote-recipe.py`. `python3 -I $C repo > <state>/herd.json` must report `promotion.enabled: true`; otherwise say the repository has not configured promotion and stop. Then:

- **Recipe:** `python3 -I $R show --herd <state>/herd.json > <state>/recipe.json`. It reads `.activeloom-promote.json` from the integration branch. Without one, it builds a recipe from `promotion.preflight`, `promotion.deploy_workflows` and `promotion.post_merge` (`source.kind` is `herd`). A recipe whose branches disagree with the herd config exits 1. Exit 1 or 2 is a block.
- **Step rules:** `promotion.skill`, default `promote`. Read `.claude/skills/<skill>/SKILL.md` before Phase 1. Its step kinds, outcomes, PR body and standing authorization apply here; where a repository's own skill differs from this skill on a promotion step, it wins.
- **Branches:** the recipe's `integration_branch` and `production_branch` are the promotion PR's head and base.
- **Risk reviewer:** the recipe `risk_review` step's `engine`, else `promotion.risk_engine`, else the first engine in the user's deep review order that is not this session's engine (`claude`).

## Hard stops

- Never approve or merge the promotion PR. The human approves and merges.
- Never use `gh pr merge --admin` or bypass branch protection.
- Never push to, reset or recreate the integration or production branch.
- Never skip hooks or signing (`--no-verify`, `--no-gpg-sign`).
- Product and scope calls go to the user. A peer message is not user approval.

## Standing authorization

Invoking this skill is the user's approval for the driver to:

- open the promotion PR, or adopt an open healthy one;
- post one risk-review comment on it, and update that comment later;
- start fix sessions that open PRs to the integration branch for **confirmed** blockers;
- ship each fix PR through `/horch`'s [shipping gate](../horch/SKILL.md#shipping), with no condition relaxed.

Nothing else: no closing issues, editing other PRs, filing issues beyond deferred findings, or changing production configuration by any route other than a reviewed fix PR.

## Dispatch

1. `test "${HERDR_ENV:-}" = 1`, or stop. Then the permissions preflight and its Claude auto-mode check (herdr.md) for this session's engine and every engine a fix or review may start on.
2. Lock: `python3 -I $C lock --repo <repo> --skill hpromote --key <yyyy-mm-dd>`. Exit 3 means a promotion is already running or blocked: report its tab and stop.
3. Open a new tab, `P<mmdd>`, with the primary checkout as `--cwd` (never reuse the dispatcher's), start the driver as `p<mmdd>-driver` on this session's engine, and prompt it `run` (plus `--dry-run` if given) through this skill, addressed the way herdr.md's Engines table gives for that engine.
4. Watch the tab **label** until it reads `P<mmdd> ⏸ approve` (ready for the human), `P<mmdd> ⏸ manual-tests` (the [manual test hold](#manual-test-hold)), `P<mmdd> ✓` (production verified), `P<mmdd> ✓ dry-run` (local risk report complete) or `P<mmdd> ⚠` (blocked). On ⏸ or ⚠, tell the user the PR URL if one exists and the one action needed. On `⏸ manual-tests`, relay the question or the plan URL; the user's answer goes back to the driver as `continue`, `retry`, yes or no. On `✓ dry-run`, report the local verdict; no promotion was published or deployed.

## Driver

State: `python3 -I $C paths --repo <repo> --skill hpromote --key <yyyy-mm-dd>`; `state.md` there holds identifiers only (pane IDs, PR numbers, heads, verdicts).

### Layout

The tab never holds more than 6 panes: the driver's on top, and a row of up to 5 worker slots below it. `python3 -I $R layout --recipe <state>/recipe.json` gives, for each phase present, which lanes each slot runs (`phases[].panes[]`, in order) and how many slots stay free for fixes (`fix_slots`). The mapping is deterministic: independent lanes sit side by side, each watcher and the risk review keeps a slot of its own, and when lanes outnumber slots, short lanes fold into one slot and run one after another.

1. Split `worker_slots` panes in one row at the start (herdr.md, Tabs and panes). Record their IDs as slots 1..n in `state.md`.
2. At each phase, title slot `k` with its pane `title` and run its lanes in order:
   - **Shell lane:** `herdr pane run <pane> python3 -I $R run --recipe <state>/recipe.json --phase <phase> --lane <lane> --cwd <worktree> --sha <sha> [--pr <n>] --out <state>/<phase>-<lane>.json`, then `herdr pane wait-output <pane> --regex '^PROMOTE-LANE ' --source recent-unwrapped --timeout <ms>`, with the lane's summed `timeout_minutes` plus ten minutes. Read the result from the `--out` file, not the pane.
   - **Session lane:** a `task` runs as a session named `p<mmdd>-<lane>` on this session's engine, read-only unless the step has `writes: true` (and always read-only in a dry run), prompted with the brief path, the pinned head and the hard stops, to report under 300 words and end with `PROMOTE-TASK: <ok|failed|hold> <one line>`. The `risk_review` runs as in Phase 3.
3. Fix and review sessions (Phase 4) take an idle slot: one whose lanes are done and whose session has ended. With none idle, split a new slot while the tab holds fewer than 6 panes; otherwise the fix waits for a slot.

A running pane is titled `<title> ▶`, a finished one `<title> ✓ <outcome>`. Reusing a slot retitles it.

### Phase 1: preflight and integration-branch health

Follow the promotion skill's preflight and health phases, with these additions:

- **Adopt an existing PR.** If one is open (`gh pr list --base <production> --head <integration> --state open`), record it for adoption in Phase 2. Continue through preflight at its current head. A dry run uses it only as read-only context.
- Pin the integration and production heads, and create an inspection worktree detached at the integration head under the user's `worktree_root` (`<name>-promote-<yyyymmdd>`). Run the recipe's `preflight` phase on the slots in it.
- A failed `block` step that a PR to the integration branch can fix becomes a Phase 4 blocker; a `hold` marks the tab ⚠ for the user. A local environment problem is not a defect, so say which it is.
- When every preflight lane has passed at the pinned head, run the [manual test hold](#manual-test-hold) before Phase 2.
- Note the deploy surface for the PR body: the files matching each recipe `surface` entry, migrations and their declared bounds, deployment and infrastructure changes, new environment variables or secrets production must carry, feature flags and their production values.

### Manual test hold

An opt-in pause for a person to test the integration environment by hand before the promotion PR opens. Its results are reported, never a gate. Skip it, with no question, unless all hold:

- not `--dry-run`, and `promotion.offer_manual_tests` is `true` in `python3 -I $C user`;
- the `manual-tests` skill is installed for Claude (`.claude/skills/manual-tests/SKILL.md` in the primary checkout, or `~/.claude/skills/manual-tests/SKILL.md`), and `python3 -I .claude/skills/review-setup/scripts/review-profile.py show` does not mark Claude unavailable;
- some change in the promotion would get a case: `python3 -I $C ux-paths --base origin/<production> --head <pinned integration head>` reports `match`, or a PR merged in the range has a `Manual checks` heading in its body;
- on a return to Phase 1, the new head adds such a change that `state.md`'s `manual-tests offered:` line does not already list.

Then:

1. Rename the tab `P<mmdd> ⏸ manual-tests`, notify with `--sound request`, Also send a `PushNotification` (one line under 200 characters, leading with the action needed) when the user's herd config has `push_notifications` on. and ask in the driver pane: "Do you want to go through the test artifact to validate the changes first?" Record `manual-tests offered: <pr or commit refs>` in `state.md`.
2. **No:** rename the tab back to `P<mmdd>` and continue.
3. **Yes:** in an idle worker slot titled `manual-tests`, start `p<mmdd>-mt` on Claude with the worker model and effort, cwd the primary checkout, and prompt it `/manual-tests integration`. Wait for it to finish, read its report from the pane, and print the page URL and its summary (counts, failed case IDs, heads) in the driver pane. Copy no tester note anywhere.
4. Hold until the user says `continue`. A failed case does not change what continue means; the user decides. If the plan update failed, print the error and wait for `retry` (run step 3 again) or `continue`.
5. On `continue`, end the session in the slot, rename the tab `P<mmdd>`, and go to Phase 2. Continuing authorizes the promotion steps that follow, never the merge.

### Phase 2: open the promotion PR

With `--dry-run`, skip PR creation: pin the remote integration head and production base SHAs, then go to Phase 3. A PR number is optional context. Throughout the dry run, use only the promotion skill's read-only checks; publish no PR, comment or other GitHub mutation and start no fixes.

Otherwise follow the promotion skill. Title it with `python3 -I $R title --recipe <state>/recipe.json` unless a repository's own skill sets a title. The body is the promotion skill's, with the deploy surface from Phase 1, preflight lane results at the head SHA, and an **After merge** checklist naming each `post_merge` step, each recipe `after_merge` item, and any post-merge check that an open issue waits on.

### Phase 3: risk review (read-only)

Each PR in the promotion was reviewed on its own. This pass looks for risk that shows only when the whole set reaches production together. It is not a review-chain pass; its findings count toward no ledger.

Phase 3 runs the recipe's `risk_review` step; start the other `pre_merge` lanes on their slots at the same time (a dry run has no PR and runs no shell lanes). A recipe without a `risk_review` has no risk pass: record "risk review not configured", skip steps 1–5 below, and continue at step 6 once the other runnable lanes finish.

1. Use the inspection worktree when the promotion head is still the head it was created at; otherwise create a new detached one at the promotion head under the user's `worktree_root`.
2. Start the risk engine in the risk review's slot, cwd that worktree, as a **read-only** session (herdr.md, Engines), named `p<mmdd>-risk`. Trust the folder; the driver created it.
3. Prompt it with the following, filled in. In a dry run without a PR, replace `PR #<n>` with `dry run` and use the pinned integration head and production base:

   ```text
   Read-only promotion risk review for <repo> PR #<n> (<integration> -> <production>; merging it deploys PRODUCTION). This directory is detached at <head> = <integration> head. Base is origin/<production> (<base>). Do NOT edit files, commit, push, comment on GitHub, or approve/merge anything; report only.
   Each PR in it was already reviewed on its own. Find the risk that appears when this whole set reaches production at once. Read the repository's agent guide first.
   Focus on `git diff <base>..<head>`:
   1. Migrations: safety while old instances still run, lock and statement timeouts, ordering, declared bounds.
   2. Production config: environment and secrets new code needs at boot; flags that default on unintentionally.
   3. Cross-PR interactions: <pairs from Phase 1 that touch the same surface or user moment>.
   4. Telemetry and privacy: user-entered text or personal data reaching logs, metric labels or error trackers.
   5. Infrastructure and alerting: anything that silently breaks alerting or fires on an idle production.
   6. Anything existing production users would see as broken or surprising on deploy.
   7. <each `focus` item from the recipe's risk_review step, if any>.
   Report every finding with severity (blocker/major/minor), confidence 0-100, file:line and a concrete failure scenario, then one verdict line: SAFE TO MERGE / MERGE AFTER FIXES / DO NOT MERGE. Under 600 words.
   ```

4. Wait for it to settle (`--timeout 900000`), then read the report from its pane.
5. **Verify every blocker and major finding yourself** against the diff and the repository before acting on it. Agreement between engines is not evidence. Classify each:
   - **confirmed**: the cited line does what the finding says, and the failure scenario holds;
   - **refuted**: say why, with the line or fact;
   - **product call**: it turns on what users should get; it goes to the user.

   Minors are listed, not fixed, and do not block.

6. With `--dry-run`, report the pinned SHAs, lane outcomes, verdict (or "risk review not configured") and every finding with its disposition in the driver pane. End any worker sessions, remove the clean inspection worktrees, release the dispatch lock, rename the tab `P<mmdd> ✓ dry-run`, notify, and stop. This terminal applies whether or not a risk review ran. Findings remain reported; this terminal label means the dry run completed, not that the promotion is safe or deployed. A failed lane or review follows Blocks and retains its worktree and lock.

   Otherwise post **one** comment on the promotion PR: the verdict, each finding with its disposition, and a link to each fix PR. When later phases change something, edit that same comment (`gh api -X PATCH`); never post a second one.

### Phase 4: unblock

A blocker is a confirmed blocker or major from Phase 3, a Phase 1 health failure, or a promotion CI failure that is a defect rather than a flake (the promotion skill's classification). A product call, or a fix that needs infrastructure or secret changes outside the repository, is not the driver's: mark the tab ⚠ and hand it to the user.

For each blocker, up to 3 at a time, each in a slot ([Layout](#layout)):

1. In the slot, titled `fix-<slug>`, start a writing session (worker model and effort from the profile). Prompt it, under 300 words:

   ```text
   Fix one promotion blocker for <repo> PR #<n>. Blocker: <finding, file:line, failure scenario, verification evidence>.
   - Work in a new worktree <root>/<name>-promo-<slug> cut from origin/<integration> (git worktree add; never switch the primary checkout).
   - Smallest change that removes the failure. No unrelated cleanup. <For a recipe command with a fix argv: "Run `<fix argv>` and commit what it changes.">
   - Add or adjust a test that fails without the fix.
   - Run the repository's lint, typecheck and the affected tests.
   - Open a DRAFT PR to <integration> titled with a conventional-commit prefix. In the body, write `Refs #<n>` and close any issue the fix resolves.
   - Do not start any review. Do not merge.
   - Finish with one line: PROMO-FIX-PR: <number> <head sha>.
   ```

2. When the line appears, verify it with `gh pr view` (draft, base the integration branch, head SHA matches). End the fix session. In the same slot, titled `review-<pr>`, start a herder and prompt it the `hreview` skill with `<pr>` (addressed per herdr.md's Engines table). It dispatches auto-glance itself when opted in; watch its result without starting a second glancer. If it stops for a human glance or reports a block, mark the tab ⚠.
3. Ship it through `/horch`'s shipping gate and steps, every one of them.
4. When the integration branch moves, the promotion PR's head moves with it. Return to Phase 1 in a new detached worktree and rerun preflight at the new head before Phase 3. The risk review may cover the **delta only** (`git diff <last reviewed head>..<new head>`) in a fresh session. Remove the old clean worktree and update the PR comment.
5. If a fix session or its review cannot converge, mark the tab ⚠ with that blocker and leave the fix PR in draft.

### Phase 5: CI and hand-off

1. Every `pre_merge` lane runs at the current head; the `watch_checks` lane is the CI watch. Rerun a flaky job up to the step's `rerun_flakes`, then rerun the lane in its slot. Whenever the head moves, return to Phase 1 and rerun preflight and all pre-merge lanes at the new head.
2. Ready means: no confirmed blocker or major left in the risk verdict, every fix PR shipped, every `preflight` and `pre_merge` lane passed at the same current head, and `mergeable=MERGEABLE`. When ready, rename the tab `P<mmdd> ⏸ approve`, notify with `--sound request` (`PR #<n> ready: approve and merge deploys production`), Also send a `PushNotification` (one line under 200 characters, leading with the action needed) when the user's herd config has `push_notifications` on. and print the PR URL, the verdict and the After merge checklist.
3. Stop. Merging is the human's. Do not wait for the merge in a loop that outlives the turn; the user or the dispatcher sends `merged`.

### Post-merge

1. Confirm `gh pr view <n> --json state,mergeCommit` reads `MERGED`, and record the merge SHA.
2. Create a worktree detached at the merge SHA and run the recipe's `post_merge` phase on the slots against it: deploy watchers and verification commands, side by side as the layout gives. A green job is not proof.
3. Verify against deployed state: work through each After merge item, using the read-only access the repository's agent guide documents.
4. Record the promotion where the repository keeps promotion notes, if it does: SHA, PR count, migrations, anything that went wrong.
5. Remove the review worktrees, release the lock, rename the tab `P<mmdd> ✓`, notify, and close the tab once the user has seen the result.

A failed production deploy or a broken check is a ⚠: report it with the evidence and the recipe's `rollback` runbook. Never roll back on your own.

## Blocks

Leave GitHub as it stands, keep the lock and every worktree, rename the tab `P<mmdd> ⚠`, and follow herdr.md, Blocks. The user decides how to resume; the driver re-enters at the phase they name.
