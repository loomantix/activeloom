---
name: hred-team
description: Red team on herdr — a non-author engine runs the red-team pass on a reviewed pull request in its own read-only pane; blocker and high findings are fixed on the PR by the author engine in a second pane, and lower-severity findings are filed as follow-up issues.
disable-model-invocation: true
argument-hint: '<pr> [--author claude|codex|gemini] [--engine claude|codex|gemini]'
---

# /hred-team — red team on herdr

Usage: `/hred-team <pr> [--author claude|codex|gemini] [--engine claude|codex|gemini]`

- `--author`: the engine that wrote the PR; it wins over the commits' `Co-Authored-By` trailers.
- `--engine`: the engine that runs the red team; it must not be the author.

The interactive `/red-team` stops for the user to route each finding. This skill routes by severity instead, so it runs to the end once invoked: **blocker** and **high** findings are fixed on the PR, **medium** and **low** become follow-up issues, and nothing is dropped.

This session is the **herder**. It drives two panes, the red team and the fixer; it never reads the diff as the red team or writes a fix itself. Read [`herdr.md`](../../references/herdr.md) first: engine launch, startup dialogs, prompting, reading state from the pane, waiting, stall and crash recovery, notifications and blocks all live there.

## Hard stops

- never merge, mark a PR ready, or force-push;
- never start, restart or finish a review run, or write a review-ledger marker;
- never drop, downgrade or dismiss a finding: each one ends fixed, filed, accepted, or reported privately.

## Paths

Run from the repository's primary checkout.

- `H` = `.claude/skills/critique/scripts/local-review-handoff.py`
- `P` = `.claude/skills/review-setup/scripts/review-profile.py`
- `C` = `.claude/skills/hreview/scripts/herd-config.py`
- State and scratch: `python3 -I $C paths --repo $repo --skill hred-team --key pr-<n>`.

If `H`, `P` or `C` is missing, or an engine pane does not know `red-team`, stop: classify it per herdr.md's [Missing or stale skills](../../references/herdr.md#missing-or-stale-skills) and put that report line in the block.

## Steps

1. **Preflight.** `test "${HERDR_ENV:-}" = 1`, or stop. `repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)` and `gh repo view --json visibility -q .visibility` for step 6; `gh pr view <n> --json state,isDraft,baseRefName,headRefName,headRefOid,comments`. The PR must be open. Full SHAs only. `python3 -I $C repo` and `python3 -I $C user`; either failing is a block. `python3 -I $C lock --repo $repo --skill hred-team --key pr-<n>`; exit 3 means another herder holds this PR: stop.
2. **Confirm it is time.** `python3 -I $H run-state --repo $repo --pr <n>`. An `active` run means a chain owns the PR: say to run this after it ends, unlock, and stop. A run whose `state` is `ended` with an `outcome` other than `converged`, or `none`, continues; say so in the final report. If a PR comment already carries `<!-- red-team:v1 … head=<head> -->` for the current head, this head has had its red team: unlock and stop.
3. **Pick the engines.** Author: `--author`, else the engine the PR commits' `Co-Authored-By` trailers name; none or more than one is a block asking for `--author`. Red team: `--engine`, else the first engine in `python3 -I $P order --tier deep --repo $repo` that is not the author; none available is a block. Models and efforts: `python3 -I $P launch-args --engine <e> --repo $repo`, with `--role worker` for the author. Run the [permissions preflight](../../references/herdr.md#permissions-preflight) and its [Claude auto-mode check](../../references/herdr.md#claude-auto-mode) with the red-team engine as read-only and the author as editing.
4. **Worktree.** Reuse the one on the PR branch (`git worktree list --porcelain`); otherwise `git worktree add <worktree_root>/<name>-pr<n> <branch>` and run each `worktree.bootstrap` command. Require it clean with local HEAD = PR head = remote head.
5. **Red team.** Add a row below this pane with the worktree as `--cwd` and start a **read-only** session named `pr<n>-redteam` (herdr.md, Engines and Starting a session); it reads PR text anyone may have written, so it gets no permission-off flags. Prompt it with the `red-team` skill addressed the way its engine expects, then:

   ```text
   Red-team pass for <repo> PR #<n>, base <merge-base sha>, exact head <head>.
   Follow the skill's steps 1 to 3 only. This session is fresh, so skip the
   fresh-session check. You are read-only: do not edit, commit, push, comment
   on GitHub, or start another session. Report every finding you believe is
   real; do not filter by severity or confidence. Do not ask the user to
   decide: the orchestrator routes each finding by severity.
   Reply with one JSON object, under 1200 words, between a line
   RED-TEAM-BEGIN and a line RED-TEAM-END:
   {"head": "<head>", "skipped_angles": "<one line>", "findings": [{"id": 1,
   "severity": "blocker|high|medium|low", "confidence": "high|medium|low",
   "recommendation": "fix|follow-up|accept", "security": false,
   "pre_existing": false, "title": "", "location": "file:line",
   "scenario": "", "evidence": "", "fix": "", "accept_condition": ""}]}
   ```

   Drive it to settled as `/hreview` drives a pass. Copy the text between the two lines from `herdr agent read pr<n>-redteam --source recent-unwrapped --lines 600` into `<scratch>/findings.json` and parse it; its `head` must equal the PR head. On a parse failure or a wrong head, ask once for the whole object again as a single fenced block; a second miss is a block. End the session and title the pane `done@<head12>`.

6. **Route.** Every finding goes to exactly one lane:

   | Finding                                         | Lane                                                                    |
   | ----------------------------------------------- | ----------------------------------------------------------------------- |
   | `blocker` or `high`, not `pre_existing`         | **Fix** on the PR                                                       |
   | `blocker` or `high`, `pre_existing`             | **Follow-up**, flagged in the notification: the change did not cause it |
   | `medium` or `low` with `recommendation: accept` | **Accept**: listed in the PR comment with its `accept_condition`        |
   | any other `medium` or `low`                     | **Follow-up**                                                           |
   | `security` in a public repository, not fixed    | **Private**: never an issue or detail in the PR comment; see step 9     |

   In a public repository a `security` finding takes the **Private** lane over **Follow-up** and **Accept**. With no finding in the fix lane, skip to step 8.

7. **Fix.** Re-read `headRefOid`; if it moved since step 5, block: the findings describe an older head. Add a pane to the right of the red team and start an **editing** session for the author engine named `pr<n>-fix`, with the user's `user-args`. Write the fix-lane findings to `<scratch>/fix.json` and prompt:

   ```text
   Fix the red-team findings in <scratch>/fix.json on <repo> PR #<n>, branch
   <branch>, starting at exact head <head>. For each finding: reproduce it
   first, with a test that goes red where one can; fix it; keep the test.
   One commit per finding, with a conventional message that describes a
   security fix without reproduction detail. Run the validation the
   repository's AGENTS.md or CLAUDE.md names, then push normally. Do not force-push,
   comment on GitHub, mark the PR ready, merge, or change anything a listed
   finding does not need. A finding you cannot reproduce or cannot fix stays
   unfixed: say why, with evidence, and commit nothing for it, not even its
   reproduction test.
   Reply with one JSON object, under 600 words, between a line FIX-BEGIN and
   a line FIX-END: {"head": "<new head>", "results": [{"id": 1,
   "outcome": "fixed|not-reproduced|not-fixed", "commit": "<sha or empty>",
   "evidence": ""}]}
   ```

   Drive it to settled as in step 5; crash recovery reuses the prompt with the line `An earlier session exited. Read git log and the branch first; do not redo a fix already committed.` Then check against external state: the new `headRefOid` equals the reported `head`, the old head is its ancestor (`git merge-base --is-ancestor`), and each `fixed` commit is in `git log <old head>..<new head>`. A failed check is a block. A `not-reproduced` or `not-fixed` result moves that finding to the follow-up lane, marked with the fixer's evidence, or to the private lane when step 6 says so, and its notification uses `--sound request`: a blocker or high finding is unfixed and the merge decision is the user's. End the session and title the pane `done@<head12>`.

8. **Follow-ups.** For each follow-up finding, search the open issues (`gh issue list --state open --search "<title> in:title"`) and link a match instead of filing a duplicate. Otherwise `gh issue create` with the title, then severity, confidence, scenario, evidence and suggested fix, a link to the PR and the red-team head, and `Pre-existing: not caused by #<n>.` where it applies. Never put credentials or private data in an issue.
9. **Private findings.** In a public repository, leave each security finding's details in `<scratch>/private-<id>.md`, which never goes in a worktree, even if it was fixed. For an unfixed private finding, tell the user in the final report and a `--sound request` notification that it needs the repository's `SECURITY.md` disclosure path. Take no further action on it.
10. **Report on the PR.** Post one comment with `gh pr comment <n> --body-file <file>`, under 400 words, starting `<!-- red-team:v1 engine=<red-team engine> head=<red-team head> -->`, then one line per non-security finding: severity, title, and its outcome (fixed in `<sha12>`, issue `#<m>`, accepted because …). In a public repository, report security findings only as aggregate counts of fixed and privately reported findings; never include their severity, title, or details.
11. **Restore review state.** If step 7 pushed, the review chain's attestations name an older head. If the PR is ready, `gh pr ready <n> --undo`. Notify with `--sound request`: `<repo>#<n> red team fixed <k> at <new head12>; review evidence is stale — run /hreview <n> --restart`. A restart is the user's to name; never run it. If nothing was pushed, leave the PR as it is and notify: `<repo>#<n> red team: <k> follow-ups, nothing to fix — merge is yours`.
12. **Finish.** `python3 -I $C unlock --repo $repo --skill hred-team --key pr-<n>`. Report under 200 words: red-team and fixer engines, red-team head and new head, the run state from step 2, counts per lane, each fixed commit and issue URL, private findings, relaunches and blocks.

## Blocks

Follow herdr.md, Blocks: leave GitHub as it stands and keep the lock and worktree. A block after step 7 pushed still runs step 11, so a ready PR never sits ready on unreviewed fixes.

## Notifications

Routine and needs-the-user notifications as herdr.md describes. Also send a `PushNotification` (one line under 200 characters, leading with the action needed) when the user's herd config has `push_notifications` on. Never put a finding's text in a notification.
