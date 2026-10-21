---
name: hreview
description: Live review chain on herdr — this session herds the ordinary interactive review passes for one or more pull requests through observable herdr panes, full auto, until each is ready or exhausted.<<DESC_TRIGGER>>
<<FM_EXTRAS>>
---

# <<INVOKE>>hreview — live review chain on herdr

Usage: `<<INVOKE>>hreview <pr> [<pr> ...] [--author claude|codex|gemini] [--companion]`

- `--companion`: the PRs are companions; run their chains one after the other, in the order given.
- Without it, independent PRs run side by side, one row each.

This session is the **herder**. It drives panes; it never runs a review pass itself and never counts its own work as one. Design: loomantix/activeloom#419 and loomantix/activeloom#422.

Read [`herdr.md`](../../references/herdr.md) first: engine launch, startup dialogs, prompting, reading state from the pane, waiting, stall and crash recovery, notifications and blocks all live there.

## Hard stops

- never merge;
- never mark a PR ready outside `next-pass` → `verify-coverage` → `finish-run --outcome converged`, except through `auto-glance`'s clean-result and CI gate below, `dep-analysis`'s `verdict=clear` and CI gate on a `dependencyUpdate` PR, or for a herder-opened gate-fix PR (see [Pre-existing red gate](#pre-existing-red-gate));
- never start a new run after the cap to get more rounds;
- never override or dismiss a reviewer's finding.

Everything else is the herder's call. Once invoked, carry each chain to ready or exhausted without asking the user.

## Paths

Run from the repository the PRs belong to.

- `H` = `<<SKILLS_ROOT>>/critique/scripts/local-review-handoff.py` (run controller: `start-run`, `next-pass`, `finish-run`)
- `L` = `<<SKILLS_ROOT>>/critique/scripts/review-ledger.js` (`classify-changeset`, `read-roster`, `post-roster`, `verify-coverage`)
- `P` = `<<SKILLS_ROOT>>/review-setup/scripts/review-profile.py`
- `C` = `<<SKILLS_ROOT>>/hreview/scripts/herd-config.py`
- State and scratch: `python3 -I $C paths --repo $repo --skill hreview --key pr-<n>`.

If `H` or `L` is missing, stop: the checkout lacks the synced review surface.

Every engine, Gemini included, runs as an interactive session in its pane. These are the ordinary interactive reviews, not the headless launchers.

## Preflight (per PR)

1. `test "${HERDR_ENV:-}" = 1`, or say this session is not inside herdr and stop. Then the permissions preflight (herdr.md) for every engine in the review order.
2. Read `<<WORKFLOW_DOC>>` and `<<REFS_ROOT>>/local-review-ledger.md` if not already in context this session. They govern tier, rounds, roster, recovery and telemetry; this skill adds only the herding.
3. `repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)`; `gh pr view <n> --json headRefName,headRefOid,baseRefName,isDraft,state,author`. The PR must be open. Full SHAs only.
4. Config: `python3 -I $C repo` and `python3 -I $C user`. Either failing is a block.
5. Worktree: reuse the one already on the PR branch (`git worktree list --porcelain`); otherwise `git worktree add <worktree_root>/<name>-pr<n> <branch>`. Defer bootstrap until after step 6. Never switch a primary checkout. Require it clean with local HEAD = PR head = remote head.
6. Tier extensions first: if `.review/addendum.local.md` has a `## Review tier extensions` section and the diff touches a surface it lists as always-Deep, skip the glance gate and resolve Deep under trigger 6 (the standing request recorded there), naming the extension in the trigger. Otherwise apply the workflow's human-glance gate, unless the PR already carries a tier marker, a `local-review-run:v1` marker, or the invocation explicitly asked for this change to be reviewed anyway. Classify the current head with `node $L classify-changeset --base <merge-base sha> --head <head>`. On `dependencyUpdate`, whatever the tier extensions or markers: start no chain, unless the PR carries the `review: full` label or the invocation explicitly asked for this dependency update to be reviewed anyway (the workflow's dependency review override). Under the override, classify again with `--force-review` and continue as a mixed range whose dependency files the chain also reviews. Otherwise run `<<INVOKE>>dep-analysis <n>` yourself in this herder session, following that skill in full, report its verdict, and end this PR's path there. On a non-empty `dependencyPaths` without `dependencyUpdate`, continue for the code, and run `<<INVOKE>>dep-analysis <n>` once the chain converges. On `skip` or `smallChange`:
   - **`glance.auto` is `false`** in `herd-config.py repo`: print the workflow's line, notify, and stop that PR for a human glance.
   - **`glance.auto` is `true`**: resolve the author as in step 8 (`--author` wins over commit trailers; missing or ambiguous authorship blocks). Run `<<INVOKE>>auto-glance <n> --author <author>` yourself in this herder session, following that skill in full, and wait for its result. Do not wait for `horch` to route it. The auto-glance skill owns the reviewer pane, exact-head result and ready flip; it rechecks eligibility and configuration. Report its clean, findings, or blocked outcome, then end this PR's path here without creating a chain run or roster or falling through to step 7. Continue other PRs under the invocation's independent/companion ordering. A declined or blocked glance is not permission to start a chain.

   For a path continuing to the chain with non-empty `dependencyPaths`, first perform `<<INVOKE>>dep-analysis` step 2's provenance check at this head, before bootstrap, installation or reviewer execution. A provenance change or an incomplete check blocks this PR's path; report it for human acceptance. This preflight does not replace the full analysis after convergence. Once cleared, run each `worktree.bootstrap` command for a newly created worktree; dependency-only paths have already handed installation to `dep-analysis` and ended above.

7. Settings — reference them, do not restate them:
   - order: `python3 -I $P order --tier <tier> --repo $repo`
   - per engine model and effort: `python3 -I $P launch-args --engine <e> --repo $repo`
   - cap: the tier's round budget in the workflow; `start-run` records it as the run's `max_rounds`.

   `python3 -I $P check` exiting non-zero for anything other than worker-only keys is a block.

8. Author: `--author` if given; otherwise the engine named by the PR commits' `Co-Authored-By` trailers. If they name none or more than one engine, and no `--author` was given, notify and stop that PR: ask for `--author`.
9. Run — find the latest `local-review-run:v1` marker in the PR's issue comments.
   - **None:** resolve the tier and trigger per the workflow's "What sets the tier" (bounded-repair exception included). Write `<scratch>/authorization.txt`: one public-safe paragraph — repository, PR, tier, mode `live chain (hreview)`, authorized: local review passes in this run only; not authorized: merge, deployment, restart. No names, no quoted user words. Then
     `python3 -I $H start-run --repo $repo --pr <n> --head <head> --base <merge-base sha> --tier <tier> --trigger <trigger|none> --cycle <order> --authorization-file <file>`.
     A scope-checkpoint refusal (`--scope-decision` required) is a block.
   - **Active:** reuse it as-is. Do not call `start-run` again.
   - **Ended** (converged, exhausted or aborted): stop that PR and report it. A new run needs the user to name a restart.
10. Roster: `node $L read-roster --repo $repo --pr <n>`. Absent → `post-roster --head <head> --author <author> --reviewers <order minus author> --content-file <scratch file>`. Present but different → block.

## Layout

One tab: this herder pane, plus one row per PR and one pane per engine in the order.

- Add a PR row below this pane with the worktree as `--cwd`; that pane is the row's first engine. Add the remaining engines to its right.
- Title idle panes `waiting`, a running pass `<engine> r<round>`, a finished one `done@<head12>`. Label the tab with the PR numbers.

## The loop (per PR, strictly sequential)

Repeat until the PR reaches an end state.

1. `head=$(gh pr view <n> --json headRefOid -q .headRefOid)`; `python3 -I $H next-pass --repo $repo --pr <n> --head $head`.
   - `status: converged` → **Converged**.
   - `status: exhausted` → **Exhausted**.
   - `status: next` → take `engine` and `round`.
   - A non-zero exit is a block, with the controller's message.
2. **Start a fresh session** in the engine's pane, named `<n>-<engine>`, with the profile's model and effort and the user's `user-args` for that engine (herdr.md, Starting a session). End any earlier session in the pane first.
3. **Prompt.**
   - Skill: Lean uses `critique`; Deep uses `deepcritique`. On this engine's first pass in the PR at Lean, run `refactorpass` first; its ledger latch decides whether it runs. Address the skill the way the target engine expects (herdr.md, Engines).
   - Text: the skill line, then `Review pass for <repo> PR #<n>, run <run_id>, round <round>, pinned base <base>, exact head <head>. Post verified findings inline before edits; validate, commit, push, reply and resolve; publish the normal review result. This session owns exactly this one pass: do not launch another engine, start or finish the run, mark the PR ready, merge, or force-push.`
   - `herdr agent prompt <name> "<text>" --wait --timeout 600000`.
4. **Drive until the pass lands.** Loop on `herdr agent wait <name> --timeout 600000`:
   - `blocked`: read the pane and answer with `send-keys` or `prompt`. Choose the answer that continues the pass within its skill's contract. Accept a tier recommendation. Never pick an option that dismisses, downgrades or overrides a finding; if that is the only way forward, it is a block.
   - `done` or `idle`: read the pane (herdr.md, Status is a hint). If it finished, check the ledger (step 5). If the pass has not landed: answer a prose question with `agent prompt`; prompt `Finish and publish this pass's result per your skill.` if it stopped early; apply the workflow's "Recover a blocked pass" if it reports blocked.
   - `timeout` while working: stall check (herdr.md).
   - Crash: relaunch per herdr.md with the recovery line `An earlier session for this pass exited. Read the ledger first and finish the pass without re-posting existing findings.`
5. **Handoff.** The pass has landed only when both hold:
   - the pane shows the session finished;
   - `next-pass` at the new PR head lists a pass for this `engine` and `round`.

   Then end the session, title the pane `done@<head12>`, notify (`<repo>#<n> <engine> r<round>: <classification>`), and loop.

Never open a second engine on the same PR while a pass is running. Independent PRs' rows run concurrently. Before starting a pass, judge whether that engine is already busy on another row and hold the pass until it frees.

## Pre-existing red gate

A pass that returns blocked only because the local gate is red on code the PR does not touch (red at the base too, or caused by the host and not the diff) is the one case where the herder writes code. It is never a review pass.

1. If the repository config's `gate_fix` is not enabled, it is a block: notify with the failing check and its state on the base branch.
2. Reproduce the failure in the PR worktree and find the root cause.
3. Fix it in a **separate** PR: `git worktree add <worktree_root>/<name>-<topic> -b fix/<topic> origin/<PR base>`, bootstrap, fix, prove the failing check passes and that the new or changed test fails against the old code, format and lint, commit (conventional, closing keyword in the message), push, `gh pr create --draft --base <PR base>`. Never put the fix on the PR under review.
4. **Eligibility (fail closed).** Every condition must hold, or it is a block: open nothing, notify with the cause and the proposed fix, and stop that PR.
   - `python3 -I $C gate-fix-paths --base <fix base> --head <fix head>` reports `eligible: true`. The repository's globs are the only source of what a gate fix may touch.
   - `node $L classify-changeset` over the fix range reports `smallChange: true`.
   - Nothing the ActiveLoom sync delivers; fix that upstream instead.
   - The script's contract is unchanged: exit codes, refusals, assertions and outputs stay the same, and the PR body says so.
   - The gate is not weakened: never skip, delete, loosen or `.todo` a failing test, add an environment guard that skips it, or widen a timeout to get green.
   - Any change to tooling comes with a test that fails against the old code.
5. Mark the fix PR ready and notify with `--sound request`: `<repo>#<fix> gate fix (test/tooling only) for the red gate on #<n> — merge to resume`. Merging stays the user's.
6. Wait for `gh pr view <fix> --json state` to read `MERGED` (herdr.md, Waiting). Then merge `origin/<PR base>` into the PR branch in its worktree with a merge commit (never rebase or force-push), run each `gate_fix.extra_checks` command and the failing check, push, re-post the roster at the new head (`post-roster` derives `--supersedes`), and re-run the blocked pass from step 1 of the loop. Its prompt carries one extra line naming the earlier blocked result and the fix PR, and telling it to read the ledger first.

At most one gate fix per PR per run. A second red gate of this kind is a block.

## End states

**Converged** (`next-pass` says `converged`):

1. `node $L verify-coverage --repo $repo --pr <n> --head $head` must pass.
2. `python3 -I $H finish-run --repo $repo --pr <n> --head $head --outcome converged` must exit 0. A failure at either step is a block; the PR stays draft.
3. If `classify-changeset --base <merge-base sha> --head $head` reports a non-empty `dependencyPaths` (mixed range), run `<<INVOKE>>dep-analysis <n>` at `$head` now. Apply the workflow's Dependency updates evidence rule to select the authenticated GitHub actor's latest analysis comment. Require its `dependency-analysis:v1` comment at `head=$head` to record `verdict=clear`; missing or malformed evidence or any other verdict (`gaps`, `broken`, `blocked`) leaves the PR draft, notifies with the verdict, and stops before `gh pr ready`.
4. CI at the exact head: `gh run list --commit $head --json name,status,conclusion,databaseId,workflowName`. Wait until every run has completed; an absent run is not a passing one.
   - **All green:** re-read `headRefOid` immediately before readiness and require it to equal `$head`. If it moved, leave the PR draft, notify that the new head needs review and dependency analysis, and stop. Otherwise run `gh pr ready <n>`, then re-read the head; on a mismatch, restore draft status and notify instead of reporting success. Notify with `--sound request` only for the unchanged head: `<repo>#<n> converged, CI green — ready for final review and merge`.
   - **Any red:** leave the PR draft. Find the failing job (`gh run view <id> --json jobs`) and that workflow's latest result on the base branch (`gh run list --branch <base> --workflow <workflow> -L 1`). Notify with both. Do not fix it.

**Exhausted** (`next-pass` says `exhausted`): `python3 -I $H finish-run --repo $repo --pr <n> --head $head --outcome exhausted`. Leave the PR draft and notify.

**Block**: leave the run open and the PR draft, then follow herdr.md, Blocks. Continue any other independent rows.

For companion PRs, start the next PR's chain only after the previous one reaches an end state.

## Notifications

Routine and needs-the-user notifications as herdr.md describes.<<HERD_PUSH>>

## Metrics

Each pass emits its ordinary per-pass record; marking live passes waits on loomantix/activeloom#420.

## Final report

Per PR, under 200 words: end state, PR state (ready or draft), run ID, then an ordered list of engine, round, head and classification. For auto-glance, report its outcome, reviewer, exact head and CI result instead of a run ID or rounds; no chain was started. Also list relaunches, blocks, gate fixes and the CI result.
