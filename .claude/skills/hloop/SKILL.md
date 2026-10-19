---
name: hloop
description: Herdr batch loop — the orchestrator picks a batch of agent-ready issues, starts a build tab for each that ends at a draft PR with no review, then a fresh hreview chain (or auto-glance at the glance gate), then ships each converged PR to the integration branch through horch's gate. Issues with an open design go to the grill queue instead of a build.
disable-model-invocation: true
argument-hint: 'next <k> | <issue>... [--concurrency <c>]'
---

# /hloop — batch loop on herdr

Usage:

- `/hloop next <k>`: the top `k` ready agent issues.
- `/hloop <n> [<m> ...]`: the named issues.
- `--concurrency <c>`: builds running at once. Default 3.

This session is the **orchestrator** and follows the `horch` skill (`.claude/skills/horch/SKILL.md`) for everything this file does not change: preflight, scope, state file, starting sessions, watching, shipping, alerts and handoff. Read it first. This file adds the batch: picking, ordering, and the build → review → ship pipeline for each issue. Mechanics: [`herdr.md`](../../references/herdr.md).

## Hard stops

`horch`'s hard stops, plus:

- never close, reassign or unassign an issue; labels change only through the ship;
- never submit a prompt that a session's input box suggests; decide the next step and send it as your own prompt;
- product and scope calls go to the user, or to `hgrill` when the user has asked for grill chains;
- never pad a batch with issues that fail the screens below.

## 1. Pick

1. **Queue.** `python3 -I .claude/skills/issues/scripts/ready.py --agent --limit 30 --json` for `next <k>`; otherwise the named issues. A single bare number names one issue, never a batch size.
2. **Skip**, and say why, any issue that:
   - is assigned to someone else;
   - carries an `agent-bail:` label, or a label `ready.py` hard-excludes;
   - lacks `agent: refined` (offer `/backlog-refinement` for those);
   - already has a merged `Refs` PR, which means it waits on the user's check of the integration environment.
3. **Design screen.** Read each body and its comments. An issue that lists alternative designs ("options include…") with no recorded choice goes to the grill queue, not a build. A build started on one stops at its scope check with nothing to show.
4. **Overlap screen.** Compare the files each body names. Issues that touch the same file run one after the other: two branches editing one file make the second PR conflict once the first ships. The issue that changes behaviour goes first; one that only adds tests for that code waits for it.
5. **Show the plan before launching:** the waves and their issues, each skipped issue with what it needs, and the serialized pairs. A batch costs one build plus one review chain per issue, roughly 30–70 minutes of review wall time each; say so for a batch of 10 or more, and say when the queue holds fewer than `<k>` issues.

Done when the user has the plan and no hold stops it (`horch`, Holding).

## 2. Build

Start each build as `horch`'s **Build** describes: tab label `<n> build`, agent `i<n>-build`, worktree `<worktree_root>/<repo name>-<slug>`, where the slug is short, names no person, and ends with the issue number. Add to its prompt:

- read the issue body and every comment first; stay inside its scope;
- if the scope check fails or a design or product choice is open, stop **before** claiming and end the turn with `BUILD-QUESTION: <one line>`;
- before pushing, run the repository's generate, lint, typecheck and test steps for what changed (the full suite when shared logic or a dependency changed), and report the commands, results and skipped-test count in the PR body;
- never bypass commit hooks or signing.

Keep at most `--concurrency` builds running. Start the next queued issue when a build reports. Start a serialized issue only once its predecessor has **shipped**.

A `BUILD-QUESTION` moves the issue to the skipped list with that question. End the session and close its tab.

## 3. Review

On `BUILD-PR: <pr> <sha>`:

1. One read: `gh pr view <pr> --json isDraft,baseRefName,headRefOid,body`. The PR is a draft, the base is the integration branch, the head is the reported SHA, and the body names the issue. A mismatch is a block. Keep the PR in draft so `hreview` or `auto-glance` owns the ready flip on convergence.
2. Wait for the build session to finish its turn (read the pane), end it, and close the build tab. Closing a tab mid-turn loses the session's last push.
3. Start the review chain as `horch`'s **Review chain** describes, in a new tab labelled `<pr>`, and its watcher.

## 4. When a watcher stops

Read the herder's pane, then:

- **converged** → step 5;
- **stopped at the glance gate** → `horch`'s glance routing. When `auto-glance` declines because the change can never classify as small (a dependency or lockfile change, whatever its size), prompt `hreview <pr>` to review the change anyway (overriding the human-glance gate);
- **glance with findings** → send the findings to a fix session (the build's session if it is still open, otherwise a new one in the PR's tab), then glance again at the new head;
- **exhausted, non-converged or blocked** → `horch`'s Problems. Leave the PR and its tab as they are.

## 5. Ship

1. **Tests at the head.** CI on PRs to the integration branch may skip test jobs, so run, in the PR's worktree at its head, the specs the PR added or changed, and the full suite when the PR changes shared logic or a dependency. The repository's `AGENTS.md or CLAUDE.md` names the commands and any environment the database-backed tests need. Check the skipped count: tests that skip for a missing environment are not a pass. If the build session removed its worktree, use a detached one at the head under `worktree_root` and remove it afterwards. A failure is a block.
2. **Ship** through `horch`'s Shipping (if the converged PR is still in draft, mark it ready with `gh pr ready <pr>` first), with a summary of one or two plain sentences on what changed for users, written from the PR body and claiming no more than it does.
3. **Deferred findings.** A herder's final report can list findings the reviewers deferred. For the ones that are confirmed and user-visible, file one follow-up issue per PR, quoting each comment's file and line, with acceptance criteria. Label it `dev: agent` but **not** `agent: refined`, so the next batch skips it until it is refined. Test gaps go in the same issue. First search open issues for the PR number and each finding's file: the herder may already have filed it.
4. **Clean up.** Remove the build worktree and its local branch, skipping whatever the merge already removed. Leave the review tab as `horch` says.
5. Start any queued or serialized issue, unless a hold is set.

## 6. Report

At the end of the batch, and whenever the user asks:

- a table: issue, PR, outcome (shipped with the merge SHA; skipped, blocked or exhausted with the reason), tab;
- follow-up issues filed;
- decisions waiting on the user.

Update the handoff in the state directory when the batch ends.

## Traps

- A herder parked at the glance gate shows `done` with no run marker and says so in its last message. Read the pane; don't wait for a run end that will not come.
- `mergeable` reads `UNKNOWN` for 10–20 seconds after another PR merges. Re-read before treating it as a gate failure.
- Editing a PR body re-runs its body-triggered workflows at the same head. Wait for them before the gate reads CI.
- A PR that conflicts with its base can have zero checks queued. Check mergeability, not just checks.
- A dependency bump can change behaviour. Run the full suite and read every failure before calling it alignment only.
- Check claims in a review report (a version a catalog pins, which repository an issue link resolves in) against the file before repeating them to the user.
