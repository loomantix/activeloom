---
name: hpromote
description: Promotion on herdr — opens a dedicated tab that runs the repository's integration-to-production promotion skill, a read-only risk review of the whole promotion diff in its own pane, and fix sessions that ship confirmed blockers to the integration branch. Stops at the human approval and merge, then watches the production deploy.
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

`C` = `.claude/skills/hreview/scripts/herd-config.py`. `python3 -I $C repo` must report `promotion.enabled: true`; otherwise say the repository has not configured promotion and stop. From it:

- `promotion.skill`: the repository's own promotion skill. It is the source for the promotion steps and their repository-specific rules; where it and this skill differ on a promotion step, it wins.
- `integration_branch` and `promotion.production_branch`: the promotion PR's head and base.
- `promotion.preflight`, `promotion.deploy_workflows`, `promotion.post_merge`: commands and workflow names the phases below run or watch.
- `promotion.risk_engine`: the risk reviewer. When null, the first engine in the user's deep review order that is not this session's engine (`claude`).

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

1. `test "${HERDR_ENV:-}" = 1`, or stop.
2. Lock: `python3 -I $C lock --repo <repo> --skill hpromote --key <yyyy-mm-dd>`. Exit 3 means a promotion is already running or blocked: report its tab and stop.
3. Open a new tab, `P<mmdd>`, with the primary checkout as `--cwd` (never reuse the dispatcher's), start the driver as `p<mmdd>-driver` on this session's engine, and prompt it `run` (plus `--dry-run` if given) through this skill, addressed the way herdr.md's Engines table gives for that engine.
4. Watch the tab **label** until it reads `P<mmdd> ⏸ approve` (ready for the human), `P<mmdd> ✓` (production verified), `P<mmdd> ✓ dry-run` (local risk report complete) or `P<mmdd> ⚠` (blocked). On ⏸ or ⚠, tell the user the PR URL if one exists and the one action needed. On `✓ dry-run`, report the local verdict; no promotion was published or deployed.

## Driver

State: `python3 -I $C paths --repo <repo> --skill hpromote --key <yyyy-mm-dd>`; `state.md` there holds identifiers only (pane IDs, PR numbers, heads, verdicts).

### Layout

The driver's pane on top, with a row below it: `risk` (Phase 3), one `fix-<slug>` per blocker fix (Phase 4, at most 3 at once), one `review-<pr>` per fix PR's chain or glance. A running pane is `<role> ▶`, a finished one `<role> ✓ <outcome>`.

### Phase 1: preflight and integration-branch health

Follow the promotion skill's preflight and health phases, with these additions:

- **Adopt an existing PR.** If one is open (`gh pr list --base <production> --head <integration> --state open`), a normal run adopts it and goes to Phase 3. A dry run uses it only as read-only context and continues through the health checks below and Phase 2's SHA pinning.
- Run each `promotion.preflight` command in the primary checkout at the integration head. A failure that a PR to the integration branch can fix becomes a Phase 4 blocker; a local environment problem is not a defect, so say which it is.
- The latest run of each `promotion.deploy_workflows` workflow on the integration head must be `success`. A `cancelled` run superseded by a later green one is normal.
- Note the deploy surface for the PR body: migrations and their declared bounds, deployment and infrastructure changes, new environment variables or secrets production must carry, feature flags and their production values.

### Phase 2: open the promotion PR

With `--dry-run`, skip PR creation: pin the remote integration head and production base SHAs, then go to Phase 3. A PR number is optional context. Throughout the dry run, use only the promotion skill's read-only checks; publish no PR, comment or other GitHub mutation and start no fixes.

Otherwise follow the promotion skill. Title it `Promote <integration> → <production> — <yyyy-mm-dd>` unless that skill sets a title. In the body: the categorized summary, the deploy surface from Phase 1, preflight results at the head SHA, and an **After merge** checklist naming each `promotion.post_merge` command and any post-merge check that an open issue waits on.

### Phase 3: risk review (read-only)

Each PR in the promotion was reviewed on its own. This pass looks for risk that shows only when the whole set reaches production together. It is not a review-chain pass; its findings count toward no ledger.

1. Create a detached worktree at the promotion head under the user's `worktree_root`: `git worktree add --detach <root>/<name>-promote-<yyyymmdd> <head>`.
2. Start the risk engine in the `risk` pane, cwd that worktree, as a **read-only** session (herdr.md, Engines), named `p<mmdd>-risk`. Trust the folder; the driver created it.
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
   Report every finding with severity (blocker/major/minor), confidence 0-100, file:line and a concrete failure scenario, then one verdict line: SAFE TO MERGE / MERGE AFTER FIXES / DO NOT MERGE. Under 600 words.
   ```

4. Wait for it to settle (`--timeout 900000`), then read the report from its pane.
5. **Verify every blocker and major finding yourself** against the diff and the repository before acting on it. Agreement between engines is not evidence. Classify each:
   - **confirmed**: the cited line does what the finding says, and the failure scenario holds;
   - **refuted**: say why, with the line or fact;
   - **product call**: it turns on what users should get; it goes to the user.

   Minors are listed, not fixed, and do not block.

6. With `--dry-run`, report the pinned SHAs, verdict and every finding with its disposition in the driver pane. End the risk session, remove its clean detached worktree, release the dispatch lock, rename the tab `P<mmdd> ✓ dry-run`, notify, and stop. Findings remain reported; this terminal label means the dry run completed, not that the promotion is safe or deployed. A failed review follows Blocks and retains its worktree and lock.

   Otherwise post **one** comment on the promotion PR: the verdict, each finding with its disposition, and a link to each fix PR. When later phases change something, edit that same comment (`gh api -X PATCH`); never post a second one.

### Phase 4: unblock

A blocker is a confirmed blocker or major from Phase 3, a Phase 1 health failure, or a promotion CI failure that is a defect rather than a flake (the promotion skill's classification). A product call, or a fix that needs infrastructure or secret changes outside the repository, is not the driver's: mark the tab ⚠ and hand it to the user.

For each blocker, up to 3 at a time:

1. In a new `fix-<slug>` pane, start a writing session (worker model and effort from the profile). Prompt it, under 300 words:

   ```text
   Fix one promotion blocker for <repo> PR #<n>. Blocker: <finding, file:line, failure scenario, verification evidence>.
   - Work in a new worktree <root>/<name>-promo-<slug> cut from origin/<integration> (git worktree add; never switch the primary checkout).
   - Smallest change that removes the failure. No unrelated cleanup.
   - Add or adjust a test that fails without the fix.
   - Run the repository's lint, typecheck and the affected tests.
   - Open a DRAFT PR to <integration> titled with a conventional-commit prefix. In the body, write `Refs #<n>` and close any issue the fix resolves.
   - Do not start any review. Do not merge.
   - Finish with one line: PROMO-FIX-PR: <number> <head sha>.
   ```

2. When the line appears, verify it with `gh pr view` (draft, base the integration branch, head SHA matches). End the fix session. In a `review-<pr>` pane, start a herder and prompt it the `hreview` skill with `<pr>` (addressed per herdr.md's Engines table); if it stops at the glance gate and the repository has `glance.auto` on, prompt it the `auto-glance` skill with `<pr>`; otherwise mark the tab ⚠ for a human glance.
3. Ship it through `/horch`'s shipping gate and steps, every one of them.
4. When the integration branch moves, the promotion PR's head moves with it. Re-run Phase 3 on the **delta only** (`git diff <last reviewed head>..<new head>`) in a fresh session and a new detached worktree, remove the old one, and update the PR comment.
5. If a fix session or its review cannot converge, mark the tab ⚠ with that blocker and leave the fix PR in draft.

### Phase 5: CI and hand-off

1. Watch the promotion PR's checks at the current head (the promotion skill's CI phase). Rerun a flake once.
2. Ready means: no confirmed blocker or major left in the risk verdict, every fix PR shipped, checks green at the head, and `mergeable=MERGEABLE`. When ready, rename the tab `P<mmdd> ⏸ approve`, notify with `--sound request` (`PR #<n> ready: approve and merge deploys production`), Also send a `PushNotification` (one line under 200 characters, leading with the action needed) when the user's herd config has `push_notifications` on. and print the PR URL, the verdict and the After merge checklist.
3. Stop. Merging is the human's. Do not wait for the merge in a loop that outlives the turn; the user or the dispatcher sends `merged`.

### Post-merge

1. Confirm `gh pr view <n> --json state,mergeCommit` reads `MERGED`, and record the merge SHA.
2. Watch each `promotion.deploy_workflows` run on that SHA, and anything the promotion skill names for after the merge. A green job is not proof.
3. Verify against deployed state: run each `promotion.post_merge` command and work through each After merge item, using the read-only access the repository's agent guide documents.
4. Record the promotion where the repository keeps promotion notes, if it does: SHA, PR count, migrations, anything that went wrong.
5. Remove the review worktrees, release the lock, rename the tab `P<mmdd> ✓`, notify, and close the tab once the user has seen the result.

A failed production deploy or a broken check is a ⚠: report it with the evidence and the repository's rollback runbook. Never roll back on your own.

## Blocks

Leave GitHub as it stands, keep the lock and every worktree, rename the tab `P<mmdd> ⚠`, and follow herdr.md, Blocks. The user decides how to resume; the driver re-enters at the phase they name.
